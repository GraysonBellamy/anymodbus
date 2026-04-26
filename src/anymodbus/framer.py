"""ADU framing for Modbus RTU.

The framer wraps a PDU with the slave-address byte and trailing CRC for
transmission, and parses an inbound ADU back into ``(slave_address, pdu)``
using a length-aware state machine.

The state machine is the technical heart of the library. It uses a per-FC
response-length table to read exactly the right number of bytes for known
function codes, falling back to a t1.5-character idle-gap reader only for
truly unknown function codes (vendor-private FCs in the user-defined ranges).
This survives Linux/macOS scheduling jitter where response bytes arrive in
2-3 ms chunks; gap-only readers do not.

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
    CRCError,
    FrameError,
    ModbusUnsupportedFunctionError,
    ProtocolError,
    UnexpectedResponseError,
    code_to_exception,
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

# FCs defined by the spec but not implemented by this version. Recognised so
# the framer raises a precise error instead of mis-framing a response on the
# wire. Note FC 0x18 (Read FIFO Queue) actually carries a 2-byte byte_count
# when implemented; that's a future-work consideration.
_KNOWN_UNSUPPORTED: Final[frozenset[int]] = frozenset(
    {
        0x07,  # Read Exception Status (serial line only)
        0x08,  # Diagnostics
        0x0B,  # Get Comm Event Counter
        0x0C,  # Get Comm Event Log
        0x11,  # Report Server ID
        0x14,  # Read File Record
        0x15,  # Write File Record
        0x18,  # Read FIFO Queue
        FunctionCode.ENCAPSULATED_INTERFACE_TRANSPORT,  # 0x2B (MEI 0x0E planned for v0.2)
    }
)

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
        raise ValueError(msg)
    if len(pdu) == 0:
        msg = "pdu must not be empty"
        raise ValueError(msg)
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


async def read_response_adu(  # noqa: PLR0912, PLR0915 — state machine; splitting hurts readability
    stream: anyio.abc.ByteStream,
    *,
    expected_slave_address: int,
    expected_function_code: FunctionCode,
    inter_char_timeout: float,
) -> tuple[int, bytes]:
    """Read one response ADU from ``stream`` using the length-aware state machine.

    Returns ``(slave_address, pdu)``. The PDU is the function-code byte plus
    the response body, sans the trailing CRC (which has been verified before
    we hand the PDU back). Exception responses (FC | 0x80) are converted into
    the matching :class:`ModbusExceptionResponse` subclass and raised, so a
    successful return always carries a normal response.

    The state machine implements *DESIGN.md §6.3*: read 2-byte header, drain
    stray frames (per *serial §2.4.1*), then dispatch on FC to one of the
    length-aware branches (fixed tail / 1-byte byte_count / known-unsupported
    / truly unknown).

    The caller is expected to wrap this in ``anyio.fail_after(request_timeout)``
    to bound the overall transaction; this function does not enforce a
    deadline of its own.

    Args:
        stream: AnyIO byte stream connected to the bus.
        expected_slave_address: The slave we sent the request to. Used for
            the resync check; replies addressed to other slaves are drained
            and we keep waiting (per *serial §2.4.1*).
        expected_function_code: The FC we sent. Used to disambiguate
            exception responses (FC | 0x80) from normal responses, to pick
            the correct length-aware branch, and to surface mismatches via
            :class:`UnexpectedResponseError`.
        inter_char_timeout: Seconds of idle time on the rx side that signals
            end-of-frame for the unknown-FC fallback path and the
            unexpected-slave drain.

    Raises:
        FrameError: Frame was truncated (EOF before all expected bytes
            arrived) or a 1-byte byte_count exceeded the spec maximum.
        CRCError: Frame was complete but the trailing CRC did not verify.
        ProtocolError: Slave returned function code 0 (invalid per *app §4.1*).
        UnexpectedResponseError: Slave address or function code echoed back
            did not match the request.
        ModbusUnsupportedFunctionError: Slave responded with a function code
            that this version of ``anymodbus`` recognises but does not yet
            implement.
        ModbusExceptionResponse: Slave returned an exception response (FC |
            0x80); the specific subclass depends on the exception code.
    """
    while True:
        head = await _read_exact(stream, 2)
        slave = head[0]
        if slave != expected_slave_address:
            # *serial §2.4.1*: a reply addressed to a different slave does NOT
            # abort the transaction. Drain the stray frame using a t1.5 idle
            # gap and keep waiting under the same enclosing deadline.
            await _read_until_idle(stream, gap=inter_char_timeout)
            _LOGGER.info(
                "Discarded stray frame from slave 0x%02x (expecting 0x%02x)",
                slave,
                expected_slave_address,
            )
            continue
        break

    fc = head[1]
    expected_fc = int(expected_function_code)

    if fc == 0:
        # *app §4.1*: "Function code '0' is not valid."
        msg = "slave returned function code 0 (invalid per app §4.1)"
        raise ProtocolError(msg)

    if fc & 0x80:
        # Exception response: total ADU = slave(1) + fc(1) + ec(1) + crc(2) = 5.
        tail = await _read_exact(stream, 3)
        if not verify_crc(head + tail):
            # CRC check BEFORE we trust any byte in the exception payload.
            msg = f"CRC mismatch on exception response (fc={fc:#04x})"
            raise CRCError(msg)
        base_fc = fc & 0x7F
        if base_fc != expected_fc:
            msg = f"exception response echoes fc {base_fc:#04x}, expected {expected_fc:#04x}"
            raise UnexpectedResponseError(msg)
        _LOGGER.info(
            "Slave 0x%02x returned exception code 0x%02x for fc 0x%02x",
            slave,
            tail[0],
            base_fc,
        )
        raise code_to_exception(function_code=base_fc, exception_code=tail[0])

    if fc != expected_fc:
        msg = f"slave returned fc {fc:#04x}, expected {expected_fc:#04x}"
        raise UnexpectedResponseError(msg)

    if fc in _BYTE_COUNT_1B:
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
    elif fc in _KNOWN_UNSUPPORTED:
        msg = (
            f"FC {fc:#04x} is defined by the Modbus spec but not implemented "
            f"by this version of anymodbus"
        )
        raise ModbusUnsupportedFunctionError(msg)
    else:
        # Truly unknown FC (user-defined ranges 65-72 / 100-110, vendor
        # private). Fall back to the t1.5 gap-based reader; imprecise but the
        # only option without per-FC length knowledge.
        tail = await _read_until_idle(stream, gap=inter_char_timeout)
        if len(tail) < _CRC_LEN:
            msg = (
                f"FC {fc:#04x} response truncated: only {len(tail)} byte(s) "
                f"received after the FC byte (need at least the 2-byte CRC)"
            )
            raise FrameError(msg)

    if not verify_crc(head + tail):
        msg = f"CRC mismatch on FC {fc:#04x} response"
        raise CRCError(msg)

    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug("rx %s", (head + tail).hex())

    # Strip the trailing CRC from the tail; PDU is FC + body.
    pdu = bytes((fc,)) + tail[:-_CRC_LEN]
    return slave, pdu


__all__ = ["encode_adu", "read_response_adu"]
