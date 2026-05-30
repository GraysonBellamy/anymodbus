"""ADU framing for Modbus ASCII (*Modbus over Serial Line v1.02 §2.5.2*).

ASCII framing wraps the same PDU layer as RTU (:mod:`anymodbus.pdu`, reused
verbatim); only the envelope differs:

    ``':'`` · ``ADDR``(2 hex) · ``FUNC..DATA``(2·N hex) · ``LRC``(2 hex) · ``CR LF``

Every byte of ``{addr || pdu || lrc}`` is two uppercase ASCII-hex characters on
transmit; receive accepts either case. Unlike RTU, receive is **delimiter**-based
(read to ``CRLF``), so there is no per-FC length table — FC 0x08 and any future
FC frame for free.

The low-level :func:`read_ascii_frame` returns the **raw** de-hexed frame and
does *not* judge the LRC — mirroring how RTU callers branch on
:func:`anymodbus.crc.verify_crc` (decision D2). The client framer raises
:class:`LRCError` for a bad frame addressed to us; the test :class:`MockSlave`
drops a bad request. :class:`AsciiFramer` is the ASCII implementation of the
:class:`anymodbus.framing.Framer` strategy.
"""

from __future__ import annotations

import logging
from typing import Final

import anyio
import anyio.abc

from anymodbus.exceptions import FrameError, LRCError
from anymodbus.lrc import lrc8_bytes, verify_lrc

_LOGGER = logging.getLogger("anymodbus.bus")

_COLON: Final = 0x3A
_CR: Final = 0x0D
_LF: Final = 0x0A
_HEX_DIGITS: Final[frozenset[int]] = frozenset(b"0123456789abcdefABCDEF")

_MAX_ADDRESS_BYTE = 0xFF

# Max raw ADU = addr(1) + PDU(<=253, *app §4.1*) + LRC(1) = 255 bytes -> 510 hex
# chars. Bounds the accumulator against a slave that never sends CRLF.
_MAX_ASCII_HEX_CHARS = 2 * (1 + 253 + 1)

# Minimum raw frame: addr(1) + fc(1) + lrc(1).
_MIN_RAW_FRAME_LEN = 3


def encode_ascii_adu(*, slave_address: int, pdu: bytes) -> bytes:
    r"""Wrap ``pdu`` into a full Modbus-ASCII transmission ADU.

    Returns ``b":" + hex(addr || pdu || lrc).upper() + b"\r\n"``. The LRC is
    computed over the binary ``{addr || pdu}`` bytes *before* hex encoding
    (*serial §6.2*).

    Args:
        slave_address: 0-255, on the wire as one byte. Range validation for
            unicast/broadcast happens at the Slave / broadcast call site.
        pdu: The PDU including the function-code byte. Must be non-empty.
    """
    if not (0 <= slave_address <= _MAX_ADDRESS_BYTE):
        msg = f"slave_address must be in [0, 0xFF] (got {slave_address!r})"
        raise ValueError(msg)
    if not pdu:
        msg = "pdu must not be empty"
        raise ValueError(msg)
    body = bytes((slave_address,)) + pdu
    frame = body + lrc8_bytes(body)
    return b":" + frame.hex().upper().encode("ascii") + b"\r\n"


async def _recv_one(stream: anyio.abc.ByteStream) -> int:
    """Receive exactly one byte; raise :class:`FrameError` on EOF mid-frame."""
    try:
        chunk = await stream.receive(1)
    except anyio.EndOfStream as e:
        msg = "stream closed mid ASCII frame"
        raise FrameError(msg) from e
    # AnyIO contract: receive() yields >=1 byte or raises; guard defensively.
    if not chunk:  # pragma: no cover — defensive
        msg = "stream returned empty receive mid ASCII frame"
        raise FrameError(msg)
    return chunk[0]


async def read_ascii_frame(stream: anyio.abc.ByteStream) -> bytes:
    """Read one ``':'``..``CRLF`` frame; return the raw de-hexed ``{addr||pdu||lrc}``.

    Does **not** verify the LRC — callers decide (the framer raises
    :class:`LRCError`; the mock drops), mirroring how RTU callers branch on
    :func:`verify_crc`. Reads one byte at a time so it never over-reads past
    ``CRLF`` into a following reader's bytes (shared-port discipline). Bounded
    by the caller's enclosing ``fail_after`` deadline.

    Raises:
        anyio.EndOfStream: the stream closed cleanly **between** frames (before
            any ``':'`` was seen). Callers distinguish this from a mid-frame
            failure: the test slave stops serving; the client framer's
            enclosing ``fail_after`` normally fires first on a silent line.
        FrameError: cannot produce a frame once one has started — EOF
            mid-frame, CR not followed by LF, oversized frame with no CRLF, or
            a non-hex / odd-length body.
    """
    # 1) Discard until ':' (leading idle / inter-frame garbage). A clean EOF
    #    here (no frame in progress) propagates as EndOfStream so a serve loop
    #    stops instead of treating the close as a malformed frame.
    while True:
        chunk = await stream.receive(1)  # raises EndOfStream on a clean close
        if chunk and chunk[0] == _COLON:
            break

    # 2) Accumulate hex chars until CR; reject non-hex deterministically.
    buf = bytearray()
    while True:
        b = await _recv_one(stream)
        if b == _CR:
            if await _recv_one(stream) != _LF:
                msg = "ASCII frame: CR not followed by LF"
                raise FrameError(msg)
            break
        if b not in _HEX_DIGITS:
            msg = f"ASCII frame: non-hex byte {b:#04x}"
            raise FrameError(msg)
        buf.append(b)
        if len(buf) > _MAX_ASCII_HEX_CHARS:
            msg = "ASCII frame exceeds maximum length without CRLF"
            raise FrameError(msg)

    # 3) De-hex. Odd length -> ValueError; wrap as FrameError.
    try:
        raw = bytes.fromhex(buf.decode("ascii"))
    except ValueError as e:
        msg = f"ASCII frame: un-de-hexable body ({e})"
        raise FrameError(msg) from e
    if len(raw) < _MIN_RAW_FRAME_LEN:
        msg = f"ASCII frame too short: {len(raw)} raw byte(s)"
        raise FrameError(msg)
    return raw


class AsciiFramer:
    """ASCII implementation of the :class:`anymodbus.framing.Framer` strategy.

    Stateless; a shared :data:`ASCII_FRAMER` singleton is used throughout.
    """

    def encode_adu(self, *, slave_address: int, pdu: bytes) -> bytes:
        """Wrap ``pdu`` into a ``':'``..LRC..``CRLF`` ASCII ADU."""
        return encode_ascii_adu(slave_address=slave_address, pdu=pdu)

    async def read_adu(
        self,
        stream: anyio.abc.ByteStream,
        *,
        expected_slave_address: int,
        inter_char_idle: float,  # RTU rx-timing; ASCII is delimiter-framed, so unused (D5)
    ) -> tuple[int, bytes]:
        """Read one ASCII frame addressed to us; return ``(slave_address, pdu)``.

        Stray frames (addressed to another slave) are skipped **before** the
        LRC is judged — matching the RTU stray-drain — so a corrupt reply meant
        for someone else does not fail our transaction. A frame addressed to us
        whose LRC fails raises :class:`LRCError` immediately, so the retry
        policy can re-fire instead of blocking until ``request_timeout``.
        """
        while True:
            raw = await read_ascii_frame(stream)
            if raw[0] != expected_slave_address:
                _LOGGER.info(
                    "Discarded stray ASCII frame from slave 0x%02x (expecting 0x%02x)",
                    raw[0],
                    expected_slave_address,
                )
                continue
            if not verify_lrc(raw):
                msg = f"LRC mismatch on ASCII frame from slave 0x{raw[0]:02x}"
                raise LRCError(msg)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("rx (ascii) %s", raw.hex())
            return raw[0], raw[1:-1]  # strip the trailing LRC byte


#: Shared stateless ASCII framer singleton (returned by ``framing.get_framer``).
ASCII_FRAMER: Final[AsciiFramer] = AsciiFramer()


__all__ = ["ASCII_FRAMER", "AsciiFramer", "encode_ascii_adu", "read_ascii_frame"]
