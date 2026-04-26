"""PDU encode/decode — pure functions, no I/O.

Each function code gets one ``encode_*_request`` / ``decode_*_response``
pair. Encoders raise :class:`ValueError` (plain) when caller-supplied inputs
are out of the spec's range — this is bad input from caller code, not
wire-level corruption. Decoders raise :class:`ProtocolError` when the bytes
from the wire are malformed (truncated, oversized, byte_count mismatch,
disagreeing function code byte).

These functions operate on the **PDU** — function-code byte plus body — and
do not handle the slave-address byte or the trailing CRC. That's the
:mod:`anymodbus.framer` layer's job.

Per-FC quantity ranges (from *Modbus Application Protocol v1.1b3*):

==========  =================================  =========
FC          Operation                          Quantity
==========  =================================  =========
0x01        Read Coils                         1-2000
0x02        Read Discrete Inputs               1-2000
0x03        Read Holding Registers             1-125
0x04        Read Input Registers               1-125
0x0F        Write Multiple Coils               1-1968
0x10        Write Multiple Registers           1-123
==========  =================================  =========

All 16-bit fields are big-endian on the wire (*app §4.2*).
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from anymodbus._types import FunctionCode
from anymodbus.exceptions import ProtocolError

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Spec constants
# ---------------------------------------------------------------------------

_MAX_ADDRESS = 0xFFFF
"""Modbus PDU address field is 16-bit (*app §4.4*)."""

_MAX_REGISTER_VALUE = 0xFFFF
"""A holding register or input register is a 16-bit unsigned value."""

# Per-FC quantity bounds — straight from *app §6.x*.
_MIN_QUANTITY = 1
_MAX_READ_BITS = 2000  # FC 0x01, 0x02 (app §6.1, §6.2)
_MAX_READ_REGISTERS = 125  # FC 0x03, 0x04 (app §6.3, §6.4)
_MAX_WRITE_COILS = 1968  # FC 0x0F (app §6.11)
_MAX_WRITE_REGISTERS = 123  # FC 0x10 (app §6.12)

# Wire values for FC 0x05 Write Single Coil — only these two are legal
# per *app §6.5*; any other value is a protocol violation.
_COIL_ON = 0xFF00
_COIL_OFF = 0x0000

# Two-byte header (FC + byte_count) prepended to read-response payloads.
_RESPONSE_HEADER_LEN = 2


# ---------------------------------------------------------------------------
# Internal helpers — bounds checking, bit packing, FC byte verification.
# ---------------------------------------------------------------------------


def _check_address(address: int) -> None:
    if not (0 <= address <= _MAX_ADDRESS):
        msg = f"address must be in [0, 0xFFFF] (got {address!r})"
        raise ValueError(msg)


def _check_register_value(value: int) -> None:
    if not (0 <= value <= _MAX_REGISTER_VALUE):
        msg = f"register value must be in [0, 0xFFFF] (got {value!r})"
        raise ValueError(msg)


def _check_quantity(count: int, *, max_value: int, kind: str) -> None:
    if not (_MIN_QUANTITY <= count <= max_value):
        msg = f"{kind} quantity must be in [{_MIN_QUANTITY}, {max_value}] (got {count!r})"
        raise ValueError(msg)


def _pack_bits(bits: Sequence[bool]) -> bytes:
    """Pack ``bits`` LSB-first per coil response/request (*app §6.1, §6.11*).

    Coil 0 lives in bit 0 of byte 0; coil 7 in bit 7 of byte 0; coil 8 in bit
    0 of byte 1; etc. Trailing bits in the final byte are padded with zero.
    """
    nbytes = (len(bits) + 7) // 8
    out = bytearray(nbytes)
    for i, bit in enumerate(bits):
        if bit:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def _unpack_bits(payload: bytes, count: int) -> tuple[bool, ...]:
    """Inverse of :func:`_pack_bits`. ``payload`` must have at least ceil(count/8) bytes."""
    expected_bytes = (count + 7) // 8
    if len(payload) < expected_bytes:
        msg = f"need {expected_bytes} bytes for {count} coils, have {len(payload)}"
        raise ProtocolError(msg)
    return tuple(bool(payload[i >> 3] & (1 << (i & 7))) for i in range(count))


def _check_fc(pdu: bytes, expected: FunctionCode) -> None:
    """Validate that ``pdu`` starts with the expected FC byte and is non-empty."""
    if len(pdu) == 0:
        msg = f"PDU is empty; expected FC {expected:#04x}"
        raise ProtocolError(msg)
    if pdu[0] != expected:
        msg = f"PDU starts with FC {pdu[0]:#04x}; expected {expected:#04x}"
        raise ProtocolError(msg)


def _check_exact_length(pdu: bytes, expected_len: int, *, fc: FunctionCode) -> None:
    if len(pdu) != expected_len:
        msg = f"FC {fc:#04x} response: expected PDU length {expected_len}, got {len(pdu)}"
        raise ProtocolError(msg)


# ---------------------------------------------------------------------------
# Request encoders
# ---------------------------------------------------------------------------


def _encode_read_request(fc: FunctionCode, address: int, count: int, max_count: int) -> bytes:
    _check_address(address)
    _check_quantity(count, max_value=max_count, kind=f"FC {fc:#04x}")
    return struct.pack(">BHH", fc, address, count)


def encode_read_coils_request(address: int, count: int) -> bytes:
    """FC 0x01 — Read Coils request PDU."""
    return _encode_read_request(FunctionCode.READ_COILS, address, count, _MAX_READ_BITS)


def encode_read_discrete_inputs_request(address: int, count: int) -> bytes:
    """FC 0x02 — Read Discrete Inputs request PDU."""
    return _encode_read_request(FunctionCode.READ_DISCRETE_INPUTS, address, count, _MAX_READ_BITS)


def encode_read_holding_registers_request(address: int, count: int) -> bytes:
    """FC 0x03 — Read Holding Registers request PDU."""
    return _encode_read_request(
        FunctionCode.READ_HOLDING_REGISTERS, address, count, _MAX_READ_REGISTERS
    )


def encode_read_input_registers_request(address: int, count: int) -> bytes:
    """FC 0x04 — Read Input Registers request PDU."""
    return _encode_read_request(
        FunctionCode.READ_INPUT_REGISTERS, address, count, _MAX_READ_REGISTERS
    )


def encode_write_single_coil_request(address: int, *, on: bool) -> bytes:
    """FC 0x05 — Write Single Coil request PDU.

    Per *app §6.5*, the wire value is exactly ``0xFF00`` (ON) or ``0x0000``
    (OFF); any other value is a protocol violation. The high-level API takes
    a Python ``bool`` and the encoder produces the correct word.
    """
    _check_address(address)
    value = _COIL_ON if on else _COIL_OFF
    return struct.pack(">BHH", FunctionCode.WRITE_SINGLE_COIL, address, value)


def encode_write_single_register_request(address: int, value: int) -> bytes:
    """FC 0x06 — Write Single Register request PDU."""
    _check_address(address)
    _check_register_value(value)
    return struct.pack(">BHH", FunctionCode.WRITE_SINGLE_REGISTER, address, value)


def encode_write_multiple_coils_request(address: int, values: Sequence[bool]) -> bytes:
    """FC 0x0F — Write Multiple Coils request PDU."""
    _check_address(address)
    count = len(values)
    _check_quantity(count, max_value=_MAX_WRITE_COILS, kind="FC 0x0f")
    packed = _pack_bits(values)
    header = struct.pack(">BHHB", FunctionCode.WRITE_MULTIPLE_COILS, address, count, len(packed))
    return header + packed


def encode_write_multiple_registers_request(address: int, values: Sequence[int]) -> bytes:
    """FC 0x10 — Write Multiple Registers request PDU."""
    _check_address(address)
    count = len(values)
    _check_quantity(count, max_value=_MAX_WRITE_REGISTERS, kind="FC 0x10")
    for v in values:
        _check_register_value(v)
    byte_count = 2 * count
    header = struct.pack(">BHHB", FunctionCode.WRITE_MULTIPLE_REGISTERS, address, count, byte_count)
    return header + struct.pack(f">{count}H", *values)


# ---------------------------------------------------------------------------
# Response decoders — accept the full PDU (FC byte + body), return the
# domain payload as immutable types.
# ---------------------------------------------------------------------------


def _decode_read_bits_response(
    pdu: bytes, fc: FunctionCode, expected_count: int, *, kind: str
) -> tuple[bool, ...]:
    _check_fc(pdu, fc)
    if not (_MIN_QUANTITY <= expected_count <= _MAX_READ_BITS):
        msg = f"{kind} expected_count must be in [1, {_MAX_READ_BITS}] (got {expected_count!r})"
        raise ValueError(msg)
    if len(pdu) < _RESPONSE_HEADER_LEN:
        msg = (
            f"FC {fc:#04x} response: PDU too short "
            f"(need at least {_RESPONSE_HEADER_LEN} bytes, got {len(pdu)})"
        )
        raise ProtocolError(msg)
    byte_count = pdu[1]
    expected_byte_count = (expected_count + 7) // 8
    if byte_count != expected_byte_count:
        msg = (
            f"FC {fc:#04x} response: byte_count={byte_count} disagrees with "
            f"expected_count={expected_count} (expected byte_count={expected_byte_count})"
        )
        raise ProtocolError(msg)
    if len(pdu) != 2 + byte_count:
        msg = (
            f"FC {fc:#04x} response: PDU length {len(pdu)} disagrees with "
            f"byte_count={byte_count} (expected length {2 + byte_count})"
        )
        raise ProtocolError(msg)
    return _unpack_bits(pdu[2:], expected_count)


def decode_read_coils_response(pdu: bytes, *, expected_count: int) -> tuple[bool, ...]:
    """FC 0x01 — Read Coils response.

    ``expected_count`` is the count from the original request (the response
    only carries a byte_count). Returns a tuple of bools of length
    ``expected_count``.
    """
    return _decode_read_bits_response(pdu, FunctionCode.READ_COILS, expected_count, kind="FC 0x01")


def decode_read_discrete_inputs_response(pdu: bytes, *, expected_count: int) -> tuple[bool, ...]:
    """FC 0x02 — Read Discrete Inputs response."""
    return _decode_read_bits_response(
        pdu, FunctionCode.READ_DISCRETE_INPUTS, expected_count, kind="FC 0x02"
    )


def _decode_read_registers_response(pdu: bytes, fc: FunctionCode) -> tuple[int, ...]:
    _check_fc(pdu, fc)
    if len(pdu) < _RESPONSE_HEADER_LEN:
        msg = (
            f"FC {fc:#04x} response: PDU too short "
            f"(need at least {_RESPONSE_HEADER_LEN} bytes, got {len(pdu)})"
        )
        raise ProtocolError(msg)
    byte_count = pdu[1]
    if byte_count == 0 or byte_count % 2 != 0:
        msg = f"FC {fc:#04x} response: byte_count must be a non-zero even value (got {byte_count})"
        raise ProtocolError(msg)
    if len(pdu) != 2 + byte_count:
        msg = (
            f"FC {fc:#04x} response: PDU length {len(pdu)} disagrees with "
            f"byte_count={byte_count} (expected length {2 + byte_count})"
        )
        raise ProtocolError(msg)
    register_count = byte_count // 2
    return struct.unpack(f">{register_count}H", pdu[2:])


def decode_read_holding_registers_response(pdu: bytes) -> tuple[int, ...]:
    """FC 0x03 — Read Holding Registers response."""
    return _decode_read_registers_response(pdu, FunctionCode.READ_HOLDING_REGISTERS)


def decode_read_input_registers_response(pdu: bytes) -> tuple[int, ...]:
    """FC 0x04 — Read Input Registers response."""
    return _decode_read_registers_response(pdu, FunctionCode.READ_INPUT_REGISTERS)


def decode_write_single_coil_response(pdu: bytes) -> tuple[int, bool]:
    """FC 0x05 — Write Single Coil response. Returns ``(address, on)``.

    Per *app §6.5*, the wire value must be exactly ``0xFF00`` or ``0x0000``;
    any other value raises :class:`ProtocolError`.
    """
    _check_exact_length(pdu, 5, fc=FunctionCode.WRITE_SINGLE_COIL)
    _check_fc(pdu, FunctionCode.WRITE_SINGLE_COIL)
    _, address, value = struct.unpack(">BHH", pdu)
    if value == _COIL_ON:
        on = True
    elif value == _COIL_OFF:
        on = False
    else:
        msg = f"FC 0x05 response: value must be 0xFF00 or 0x0000 per app §6.5 (got {value:#06x})"
        raise ProtocolError(msg)
    return address, on


def decode_write_single_register_response(pdu: bytes) -> tuple[int, int]:
    """FC 0x06 — Write Single Register response. Returns ``(address, value)``."""
    _check_exact_length(pdu, 5, fc=FunctionCode.WRITE_SINGLE_REGISTER)
    _check_fc(pdu, FunctionCode.WRITE_SINGLE_REGISTER)
    _, address, value = struct.unpack(">BHH", pdu)
    return address, value


def _decode_write_multiple_response(
    pdu: bytes, fc: FunctionCode, *, max_count: int
) -> tuple[int, int]:
    _check_exact_length(pdu, 5, fc=fc)
    _check_fc(pdu, fc)
    _, address, count = struct.unpack(">BHH", pdu)
    if not (_MIN_QUANTITY <= count <= max_count):
        msg = f"FC {fc:#04x} response: count {count} outside spec range [1, {max_count}]"
        raise ProtocolError(msg)
    return address, count


def decode_write_multiple_coils_response(pdu: bytes) -> tuple[int, int]:
    """FC 0x0F — Write Multiple Coils response. Returns ``(address, count)``."""
    return _decode_write_multiple_response(
        pdu, FunctionCode.WRITE_MULTIPLE_COILS, max_count=_MAX_WRITE_COILS
    )


def decode_write_multiple_registers_response(pdu: bytes) -> tuple[int, int]:
    """FC 0x10 — Write Multiple Registers response. Returns ``(address, count)``."""
    return _decode_write_multiple_response(
        pdu, FunctionCode.WRITE_MULTIPLE_REGISTERS, max_count=_MAX_WRITE_REGISTERS
    )


__all__ = [
    "decode_read_coils_response",
    "decode_read_discrete_inputs_response",
    "decode_read_holding_registers_response",
    "decode_read_input_registers_response",
    "decode_write_multiple_coils_response",
    "decode_write_multiple_registers_response",
    "decode_write_single_coil_response",
    "decode_write_single_register_response",
    "encode_read_coils_request",
    "encode_read_discrete_inputs_request",
    "encode_read_holding_registers_request",
    "encode_read_input_registers_request",
    "encode_write_multiple_coils_request",
    "encode_write_multiple_registers_request",
    "encode_write_single_coil_request",
    "encode_write_single_register_request",
]
