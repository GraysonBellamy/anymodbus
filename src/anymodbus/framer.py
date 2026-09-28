"""ADU framing for Modbus RTU.

The framer wraps a PDU with the slave-address byte and trailing CRC for
transmission, and reads one inbound ADU back into ``(slave_address, pdu)``
using a length-aware state machine. It is the RTU implementation of the
:class:`anymodbus.framing.Framer` strategy.

The state machine is the technical heart of the library. It uses a per-FC
response-length table to read exactly the right number of bytes for known
function codes, falling back to a t1.5-character idle-gap reader only for
function codes it has no length for (vendor-private FCs, and spec FCs this
client never sends).
This survives Linux/macOS scheduling jitter where response bytes arrive in
2-3 ms chunks; gap-only readers do not.

:meth:`RtuFramer.read_adu` reads and checksum-verifies **one raw frame**; the
framing-agnostic function-code interpretation (exception split, FC mismatch)
lives in :func:`anymodbus.framing.interpret_response_pdu`. The module-level
:func:`read_response_adu` is a back-compat wrapper composing the two.

See :doc:`DESIGN.md` §6.3 for the full rationale.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import anyio
import anyio.abc

from anymodbus._types import FunctionCode
from anymodbus.crc import crc16_modbus_bytes, verify_crc
from anymodbus.exceptions import (
    ConfigurationError,
    CRCError,
    FrameError,
    ProtocolError,
)

# _FC_ZERO_MSG is shared so the RTU framer (fc==0 unframeable guard) and the
# interpreter raise the identical ProtocolError message — single source of truth.
from anymodbus.framing import (
    _FC_ZERO_MSG,  # pyright: ignore[reportPrivateUsage]
    interpret_response_pdu,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger("anymodbus.bus")

# Per *Modbus over Serial Line v1.02 §2.2*, slave addresses on the wire are
# 0-247 (1-247 unicast, 0 broadcast). 248-255 are reserved. The framer
# accepts the full 8-bit range here because the validation lives at the
# call-site (Slave construction, Bus.broadcast_*); the framer's job is just
# to put the byte on the wire.
_MAX_ADDRESS_BYTE = 0xFF

# ---------------------------------------------------------------------------
# Per-FC response-length tables — single source of truth for the rx framer.
#
# Bytes-after-header are counted EXCLUDING the 2-byte (slave + fc) header and
# INCLUDING the 2-byte trailing CRC. See *app §6.x* for each FC's response
# format.
# ---------------------------------------------------------------------------

_FIXED_TAIL: Final[Mapping[int, int]] = {
    # FC 0x05 / 0x06 / 0x0F / 0x10 echo: addr(2) + value-or-quantity(2) + crc(2).
    FunctionCode.WRITE_SINGLE_COIL: 6,
    FunctionCode.WRITE_SINGLE_REGISTER: 6,
    FunctionCode.WRITE_MULTIPLE_COILS: 6,
    FunctionCode.WRITE_MULTIPLE_REGISTERS: 6,
    # FC 0x16 (Mask Write Register): ref_addr(2) + and_mask(2) + or_mask(2) + crc(2).
    # Eight, NOT six — separate entry from the writes above. Lumping it in
    # would mis-frame the next response on the bus.
    FunctionCode.MASK_WRITE_REGISTER: 8,
    # FC 0x08 Diagnostics: subfunction(2) + data(2) + crc(2). The fixed tail of
    # 6 is valid for sub-function 0x0000 (Return Query Data) ONLY. Other FC08
    # sub-functions are variable-length and some return NO response at all
    # (notably sub 0x04 Force Listen Only Mode — see DESIGN.md §2 / serial §6).
    # This is safe because encode_diagnostic_loopback_request() can ONLY emit
    # sub-0, so no non-sub-0 response can ever arrive on this client. Do not
    # widen FC08 support without revisiting this entry.
    FunctionCode.DIAGNOSTICS: 6,
}

# FCs whose response carries a 1-byte byte_count immediately after the FC byte.
# After byte_count we read ``byte_count + 2`` more bytes (data + CRC).
_BYTE_COUNT_1B: Final[frozenset[int]] = frozenset(
    {
        FunctionCode.READ_COILS,
        FunctionCode.READ_DISCRETE_INPUTS,
        FunctionCode.READ_HOLDING_REGISTERS,
        FunctionCode.READ_INPUT_REGISTERS,
        FunctionCode.READ_WRITE_MULTIPLE_REGISTERS,
    }
)

# Per-FC *request* lengths, for servers reading requests. Bytes after the 2-byte
# (slave + fc) header, INCLUDING the 2-byte trailing CRC. See *app §6.x*.
_REQUEST_FIXED_TAIL: Final[Mapping[int, int]] = {
    # FC 0x01-0x04: address(2) + quantity(2) + crc(2).
    FunctionCode.READ_COILS: 6,
    FunctionCode.READ_DISCRETE_INPUTS: 6,
    FunctionCode.READ_HOLDING_REGISTERS: 6,
    FunctionCode.READ_INPUT_REGISTERS: 6,
    # FC 0x05 / 0x06: address(2) + value(2) + crc(2).
    FunctionCode.WRITE_SINGLE_COIL: 6,
    FunctionCode.WRITE_SINGLE_REGISTER: 6,
    # FC 0x08: sub-function(2) + data(2) + crc(2), the shape of sub-function
    # 0x0000 as this client sends it.
    FunctionCode.DIAGNOSTICS: 6,
    # FC 0x16: address(2) + and_mask(2) + or_mask(2) + crc(2).
    FunctionCode.MASK_WRITE_REGISTER: 8,
}

# FCs whose request carries a 1-byte byte_count after a fixed prefix. The value
# is the prefix length after the FC byte, excluding the byte_count itself; the
# byte_count is followed by that many data bytes and the CRC.
_REQUEST_BYTE_COUNT_PREFIX: Final[Mapping[int, int]] = {
    # address(2) + quantity(2)
    FunctionCode.WRITE_MULTIPLE_COILS: 4,
    FunctionCode.WRITE_MULTIPLE_REGISTERS: 4,
    # read address(2) + read quantity(2) + write address(2) + write quantity(2)
    FunctionCode.READ_WRITE_MULTIPLE_REGISTERS: 8,
}

# *app §4.1*: the PDU is at most 253 bytes. A 1-byte-byte_count read response
# is FC(1) + bc(1) + data(<=250) + crc(2) = 254 bytes on the wire after the
# slave address. Reject byte_count > 250 immediately to defend against a
# malformed slave inducing oversized speculative reads.
_MAX_BYTE_COUNT = 250

# Two-byte trailing CRC (*serial §2.5.1.2*).
_CRC_LEN = 2

# *app §4.1* + *serial §2.5.1.2*: full ADU = slave(1) + PDU(<=253) + CRC(2)
# = 256 bytes. The framer always reads the 2-byte head separately, so the
# remainder of any frame is <=254 bytes. The gap-based reader caps at this
# value to bound memory under a misbehaving slave that drips bytes inside
# the inter-character gap window.
_MAX_TAIL_BYTES = 254


def encode_adu(*, slave_address: int, pdu: bytes) -> bytes:
    """Wrap ``pdu`` with the slave address byte and append the CRC.

    Returns the full ADU ready for transmission. The CRC is appended in
    little-endian byte order — *Modbus over Serial Line v1.02 §2.5.1.2*:
    "low-order byte of the field is appended first, followed by the
    high-order byte." Note this is **opposite** to the big-endian convention
    used for data fields.

    Args:
        slave_address: 0-255, on the wire as a single byte. Address-range
            validation happens at the Slave / broadcast call site.
        pdu: The PDU including the function-code byte. Must be non-empty.

    Returns:
        ``slave_address | pdu | CRC-low | CRC-high``.
    """
    if not (0 <= slave_address <= _MAX_ADDRESS_BYTE):
        msg = f"slave_address must be in [0, 0xFF] (got {slave_address!r})"
        raise ConfigurationError(msg)
    if len(pdu) == 0:
        msg = "pdu must not be empty"
        raise ConfigurationError(msg)
    head = bytes((slave_address,)) + pdu
    return head + crc16_modbus_bytes(head)


# ---------------------------------------------------------------------------
# Stream read helpers. These exist as private functions because the state
# machine in :func:`read_response_adu` calls them at half a dozen sites.
# ---------------------------------------------------------------------------


async def _read_exact(stream: anyio.abc.ByteStream, n: int) -> bytes:
    """Read exactly ``n`` bytes, looping over short receives.

    AnyIO's ``ByteStream.receive(max_bytes)`` returns *up to* ``max_bytes``
    bytes — fewer when the kernel hasn't buffered enough yet. We loop,
    bounding each request by the remaining count so we never over-read past
    the frame boundary into the next response.

    Args:
        stream: The byte stream to read from.
        n: Exact number of bytes to read.

    Raises:
        FrameError: The stream returned EOF before ``n`` bytes were read,
            i.e. the frame was truncated.
    """
    buf = bytearray()
    while len(buf) < n:
        remaining = n - len(buf)
        try:
            chunk = await stream.receive(remaining)
        except anyio.EndOfStream as e:
            msg = f"stream closed after {len(buf)}/{n} bytes"
            raise FrameError(msg) from e
        if not chunk:
            # AnyIO contract: receive() returns at least 1 byte or raises
            # EndOfStream. Defensive guard against streams that violate this.
            msg = f"stream returned empty receive after {len(buf)}/{n} bytes"
            raise FrameError(msg)
        buf.extend(chunk)
    return bytes(buf)


async def _read_until_idle(
    stream: anyio.abc.ByteStream, *, gap: float, max_bytes: int = _MAX_TAIL_BYTES
) -> bytes:
    """Read bytes until ``gap`` seconds elapse with no new data.

    Used in two places:

    1. The unknown-FC fallback — for vendor-private function codes we have no
       length table for, the t1.5-character idle gap is the only way to know
       the frame ended.
    2. The unexpected-slave-drain branch — when a stray frame addressed to a
       different slave shows up, we drain the rest of it before continuing
       to wait for our slave's reply.

    The first ``receive()`` is unbounded on the wire side: cancellation is
    governed by the caller's enclosing scope (e.g. ``Bus._one_txn`` wraps
    the whole transaction in ``anyio.fail_after(request_timeout)``).
    Subsequent reads stop after ``gap`` seconds of silence.

    Args:
        stream: The byte stream to read from.
        gap: Seconds of inter-read silence that signals end-of-frame.
        max_bytes: Hard cap on bytes returned. Defends against a misbehaving
            slave that drips bytes inside the gap window forever; the gap
            can never close so we'd otherwise grow unbounded. Default is the
            spec-derived maximum tail length for one Modbus RTU ADU.

    Returns:
        All bytes read up until the idle gap (or EOF, or ``max_bytes``) hit.
    """
    buf = bytearray()
    try:
        chunk = await stream.receive(max_bytes)
    except anyio.EndOfStream:
        return bytes(buf)
    buf.extend(chunk)
    if len(buf) >= max_bytes:
        return bytes(buf[:max_bytes])
    while True:
        with anyio.move_on_after(gap) as scope:
            try:
                chunk = await stream.receive(max_bytes - len(buf))
            except anyio.EndOfStream:
                return bytes(buf)
            buf.extend(chunk)
        if scope.cancelled_caught:
            return bytes(buf)
        if len(buf) >= max_bytes:
            return bytes(buf[:max_bytes])


async def _read_raw_adu(
    stream: anyio.abc.ByteStream,
    *,
    expected_slave_address: int,
    inter_char_idle: float,
) -> tuple[int, bytes]:
    """Read one **raw** response ADU using the length-aware state machine.

    Returns ``(slave_address, pdu)`` where ``pdu`` is the function-code byte
    plus the response body, sans the trailing CRC (verified before the PDU is
    handed back). The ``pdu`` MAY carry the exception bit (``fc & 0x80``) —
    interpreting that, and any FC mismatch, is
    :func:`anymodbus.framing.interpret_response_pdu`'s job, not the framer's.

    The state machine implements *DESIGN.md §6.3*: read 2-byte header, drain
    stray frames (per *serial §2.4.1*), then dispatch on the **received** FC to
    one of the length-aware branches (exception / fixed tail / 1-byte
    byte_count) or, for any other FC, the t1.5 idle-gap reader. The received
    FC alone determines the response length, so ``expected_function_code`` is
    not needed here (decision D1).

    The caller is expected to wrap this in ``anyio.fail_after(request_timeout)``
    to bound the overall transaction; this function does not enforce a
    deadline of its own.

    Raises:
        FrameError: Frame was truncated (EOF before all expected bytes
            arrived) or a 1-byte byte_count exceeded the spec maximum.
        CRCError: Frame was complete but the trailing CRC did not verify.
        ProtocolError: Slave returned function code 0 (invalid per *app §4.1*).
    """
    while True:
        head = await _read_exact(stream, 2)
        slave = head[0]
        if slave != expected_slave_address:
            # *serial §2.4.1*: a reply addressed to a different slave does NOT
            # abort the transaction. Drain the stray frame using a t1.5 idle
            # gap and keep waiting under the same enclosing deadline.
            await _read_until_idle(stream, gap=inter_char_idle)
            _LOGGER.info(
                "Discarded stray frame from slave 0x%02x (expecting 0x%02x)",
                slave,
                expected_slave_address,
            )
            continue
        break

    fc = head[1]

    if fc == 0:
        # *app §4.1*: function code 0 is unframeable (no length table).
        raise ProtocolError(_FC_ZERO_MSG)

    if fc & 0x80:
        # Exception response: ec(1) + crc(2). The exception-bit FC is not in
        # any length table; its tail is always 3 bytes.
        tail = await _read_exact(stream, 3)
    elif fc in _BYTE_COUNT_1B:
        bc_byte = await _read_exact(stream, 1)
        bc = bc_byte[0]
        if bc > _MAX_BYTE_COUNT:
            # Defend against a malformed slave forcing a ~257-byte speculative
            # read. *app §4.1* caps the PDU at 253 bytes.
            msg = f"byte_count={bc} exceeds spec max of {_MAX_BYTE_COUNT}"
            raise FrameError(msg)
        data_and_crc = await _read_exact(stream, bc + 2)
        tail = bc_byte + data_and_crc
    elif fc in _FIXED_TAIL:
        tail = await _read_exact(stream, _FIXED_TAIL[fc])
    else:
        # Any other FC: vendor-private codes, and spec codes this client never
        # sends (0x07, 0x0B, 0x0C, 0x11, 0x14, 0x15, 0x18, 0x2B, ...). A reply
        # can only carry one of the latter through line damage (06 -> 07,
        # 03 -> 0B, 10 -> 11 are one bit apart) or a confused slave. Read to
        # the t1.5 idle gap, which also drains the rest of a damaged frame,
        # and let the CRC decide: damage fails it (CRCError, retryable); a
        # checksum-valid frame reaches interpret_response_pdu, which raises
        # UnexpectedResponseError for the function-code mismatch.
        tail = await _read_until_idle(stream, gap=inter_char_idle)
        if len(tail) < _CRC_LEN:
            msg = (
                f"FC {fc:#04x} response truncated: only {len(tail)} byte(s) "
                f"received after the FC byte (need at least the 2-byte CRC)"
            )
            raise FrameError(msg)

    if not verify_crc(head + tail):
        # CRC check BEFORE we trust any byte of the payload (incl. an exception
        # code). *DESIGN.md §6.3* — a bad CRC is retryable; a trusted exception
        # code would mislead callers.
        msg = f"CRC mismatch on FC {fc:#04x} response"
        raise CRCError(msg)

    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("rx %s", (head + tail).hex())

    # Strip the trailing CRC from the tail; PDU is FC + body.
    pdu = bytes((fc,)) + tail[:-_CRC_LEN]
    return slave, pdu


async def _drain_until_idle(stream: anyio.abc.ByteStream, *, gap: float) -> None:
    """Discard bytes until ``gap`` seconds pass with none arriving (or the stream ends).

    Unlike :func:`_read_until_idle`, the first read is bounded by ``gap`` too,
    so a line that is already quiet returns at once.
    """
    while True:
        with anyio.move_on_after(gap) as scope:
            try:
                await stream.receive(_MAX_TAIL_BYTES)
            except anyio.EndOfStream:
                return
        if scope.cancelled_caught:
            return


async def _read_raw_request_adu(
    stream: anyio.abc.ByteStream, *, inter_char_idle: float
) -> tuple[int, bytes]:
    """Read one **request** ADU, for any slave address; return ``(slave_address, pdu)``.

    The server-side counterpart of :func:`_read_raw_adu`: frames by the
    *request* length of the received FC, falling back to the t1.5 idle gap
    for FCs without a length. Does not filter by address, so one reader can
    serve several simulated slaves on one line.

    Raises:
        anyio.EndOfStream: The stream closed between frames.
        FrameError: The frame was truncated or its byte_count is impossible.
        CRCError: The frame was complete but its CRC did not verify. The rest
            of the line is drained to the next t1.5 idle gap first, so the next
            read starts at a frame boundary.
    """
    # A close before the first byte is a clean end between frames, and
    # propagates as EndOfStream; after that, a close truncates the frame.
    head = await stream.receive(2)
    if len(head) < 2:  # noqa: PLR2004 — the 2-byte (slave + fc) header
        head += await _read_exact(stream, 2 - len(head))
    slave, fc = head[0], head[1]
    if fc in _REQUEST_FIXED_TAIL:
        tail = await _read_exact(stream, _REQUEST_FIXED_TAIL[fc])
    elif fc in _REQUEST_BYTE_COUNT_PREFIX:
        prefix = await _read_exact(stream, _REQUEST_BYTE_COUNT_PREFIX[fc] + 1)
        byte_count = prefix[-1]
        if byte_count > _MAX_BYTE_COUNT:
            await _drain_until_idle(stream, gap=inter_char_idle)
            msg = f"FC {fc:#04x} request: byte_count={byte_count} exceeds spec max"
            raise FrameError(msg)
        tail = prefix + await _read_exact(stream, byte_count + _CRC_LEN)
    else:
        tail = await _read_until_idle(stream, gap=inter_char_idle)
        if len(tail) < _CRC_LEN:
            msg = f"FC {fc:#04x} request truncated: only {len(tail)} byte(s) after the FC byte"
            raise FrameError(msg)
    if not verify_crc(head + tail):
        await _drain_until_idle(stream, gap=inter_char_idle)
        msg = f"CRC mismatch on FC {fc:#04x} request to slave 0x{slave:02x}"
        raise CRCError(msg)
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("rx (request) %s", (head + tail).hex())
    return slave, bytes((fc,)) + tail[:-_CRC_LEN]


class RtuFramer:
    """RTU implementation of the :class:`anymodbus.framing.Framer` strategy.

    Stateless; a shared :data:`RTU_FRAMER` singleton is used throughout.
    """

    def encode_adu(self, *, slave_address: int, pdu: bytes) -> bytes:
        """Wrap ``pdu`` with the slave-address byte and append the CRC."""
        return encode_adu(slave_address=slave_address, pdu=pdu)

    async def read_adu(
        self,
        stream: anyio.abc.ByteStream,
        *,
        expected_slave_address: int,
        inter_char_idle: float,
    ) -> tuple[int, bytes]:
        """Read one raw frame addressed to ``expected_slave_address``.

        See :func:`_read_raw_adu`. ``inter_char_idle`` is the RTU rx-timing gap
        used for the stray drain and the unknown-FC fallback.
        """
        return await _read_raw_adu(
            stream,
            expected_slave_address=expected_slave_address,
            inter_char_idle=inter_char_idle,
        )

    async def read_request_adu(
        self,
        stream: anyio.abc.ByteStream,
        *,
        inter_char_idle: float,
    ) -> tuple[int, bytes]:
        """Read one request frame for any slave address; return ``(slave, pdu)``.

        See :func:`_read_raw_request_adu`. For servers and test slaves.
        """
        return await _read_raw_request_adu(stream, inter_char_idle=inter_char_idle)


#: Shared stateless RTU framer singleton (returned by ``framing.get_framer``).
RTU_FRAMER: Final[RtuFramer] = RtuFramer()


async def read_response_adu(
    stream: anyio.abc.ByteStream,
    *,
    expected_slave_address: int,
    expected_function_code: FunctionCode,
    inter_char_idle: float,
) -> tuple[int, bytes]:
    """Back-compat wrapper: read one RTU ADU and interpret its FC semantics.

    Composes :meth:`RtuFramer.read_adu` (raw frame) with
    :func:`anymodbus.framing.interpret_response_pdu` (FC semantics). New code
    in :class:`anymodbus.Bus` uses those two directly; this preserves the
    0.1.x signature and behaviour for existing callers and tests.

    Returns ``(slave_address, pdu)`` for a normal, matching response; raises
    CRCError, FrameError, ProtocolError, UnexpectedResponseError, or a
    ModbusExceptionResponse subclass.
    """
    slave, pdu = await RTU_FRAMER.read_adu(
        stream,
        expected_slave_address=expected_slave_address,
        inter_char_idle=inter_char_idle,
    )
    return interpret_response_pdu(
        slave_address=slave,
        pdu=pdu,
        expected_function_code=expected_function_code,
    )


__all__ = ["RTU_FRAMER", "RtuFramer", "encode_adu", "read_response_adu"]
