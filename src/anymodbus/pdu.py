"""PDU encode/decode — pure functions, no I/O.

Each function code gets one ``encode_*_request`` / ``decode_*_response``
pair. Encoders raise :class:`ConfigurationError` (a :class:`ValueError`) when
caller-supplied inputs are out of the spec's range — this is bad input from
caller code, not wire-level corruption. Decoders raise :class:`ProtocolError` when the bytes
from the wire are malformed (truncated, oversized, byte_count mismatch,
disagreeing function code byte).

For code that *answers* requests (test slaves, simulators) the module also
has the inverse pair: ``decode_*_request`` returns a small frozen dataclass
(:class:`ReadRequest`, :class:`WriteSingleRegisterRequest`, ...) and
``encode_*_response`` builds the reply, with :func:`encode_exception_response`
for exception replies. Request decoders raise :class:`ProtocolError` for a
malformed request or a quantity outside the spec's range, which a server
answers with exception code 0x03.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from anymodbus._types import FunctionCode
from anymodbus.exceptions import ConfigurationError, ProtocolError, UnexpectedResponseError

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
        raise ConfigurationError(msg)


def _check_register_value(value: int) -> None:
    if not (0 <= value <= _MAX_REGISTER_VALUE):
        msg = f"register value must be in [0, 0xFFFF] (got {value!r})"
        raise ConfigurationError(msg)


def _check_quantity(count: int, *, max_value: int, kind: str) -> None:
    if not (_MIN_QUANTITY <= count <= max_value):
        msg = f"{kind} quantity must be in [{_MIN_QUANTITY}, {max_value}] (got {count!r})"
        raise ConfigurationError(msg)


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


def _check_exact_length(
    pdu: bytes, expected_len: int, *, fc: FunctionCode, what: str = "response"
) -> None:
    if len(pdu) != expected_len:
        msg = f"FC {fc:#04x} {what}: expected PDU length {expected_len}, got {len(pdu)}"
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
        raise ConfigurationError(msg)
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
        raise UnexpectedResponseError(msg)
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
    ``expected_count``. A byte_count that does not fit ``expected_count``
    raises :class:`UnexpectedResponseError`.
    """
    return _decode_read_bits_response(pdu, FunctionCode.READ_COILS, expected_count, kind="FC 0x01")


def decode_read_discrete_inputs_response(pdu: bytes, *, expected_count: int) -> tuple[bool, ...]:
    """FC 0x02 — Read Discrete Inputs response."""
    return _decode_read_bits_response(
        pdu, FunctionCode.READ_DISCRETE_INPUTS, expected_count, kind="FC 0x02"
    )


def _decode_read_registers_response(
    pdu: bytes, fc: FunctionCode, expected_count: int | None
) -> tuple[int, ...]:
    _check_fc(pdu, fc)
    if expected_count is not None and not (_MIN_QUANTITY <= expected_count <= _MAX_READ_REGISTERS):
        msg = (
            f"FC {fc:#04x} expected_count must be in [1, {_MAX_READ_REGISTERS}] "
            f"(got {expected_count!r})"
        )
        raise ConfigurationError(msg)
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
    if expected_count is not None and register_count != expected_count:
        msg = (
            f"FC {fc:#04x} response carries {register_count} register(s); "
            f"the request asked for {expected_count}"
        )
        raise UnexpectedResponseError(msg)
    return struct.unpack(f">{register_count}H", pdu[2:])


def decode_read_holding_registers_response(
    pdu: bytes, *, expected_count: int | None = None
) -> tuple[int, ...]:
    """FC 0x03 — Read Holding Registers response.

    ``expected_count`` is the count from the original request. When given, a
    well-formed response carrying a different number of registers raises
    :class:`UnexpectedResponseError`; without it, any non-zero count is
    accepted.
    """
    return _decode_read_registers_response(pdu, FunctionCode.READ_HOLDING_REGISTERS, expected_count)


def decode_read_input_registers_response(
    pdu: bytes, *, expected_count: int | None = None
) -> tuple[int, ...]:
    """FC 0x04 — Read Input Registers response.

    See :func:`decode_read_holding_registers_response` for ``expected_count``.
    """
    return _decode_read_registers_response(pdu, FunctionCode.READ_INPUT_REGISTERS, expected_count)


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


# ---------------------------------------------------------------------------
# FC 0x08 Diagnostics — sub-function 0x0000 (Return Query Data / loopback).
#
# Only sub-0 is modelled: it echoes a 2-byte data word, mutating nothing, which
# is exactly what the AUTO-probe / link-health use case needs. Other FC08
# sub-functions are variable-length and some return no response at all; keeping
# this client to sub-0 only is what makes the RTU framer's fixed 6-byte tail
# safe (see framer.py).
# ---------------------------------------------------------------------------

_DIAG_SUBFN_RETURN_QUERY_DATA = 0x0000
_DIAG_DATA_LEN = 2
_DIAG_RESPONSE_PDU_LEN = 5  # fc(1) + subfn(2) + data(2)


def encode_diagnostic_loopback_request(data: bytes = b"\x00\x00") -> bytes:
    """FC 0x08 sub 0x0000 (Return Query Data). ``data`` must be exactly 2 bytes."""
    if len(data) != _DIAG_DATA_LEN:
        msg = f"diagnostic loopback data must be exactly 2 bytes (got {len(data)})"
        raise ConfigurationError(msg)
    return struct.pack(">BH", FunctionCode.DIAGNOSTICS, _DIAG_SUBFN_RETURN_QUERY_DATA) + data


def decode_diagnostic_loopback_response(pdu: bytes) -> bytes:
    """Return the echoed 2-byte data word from an FC 0x08 sub-0 response.

    Raises :class:`ProtocolError` on a malformed or non-sub-0 response.
    """
    _check_fc(pdu, FunctionCode.DIAGNOSTICS)
    if len(pdu) != _DIAG_RESPONSE_PDU_LEN:
        msg = f"FC 0x08 response: expected {_DIAG_RESPONSE_PDU_LEN}-byte PDU, got {len(pdu)}"
        raise ProtocolError(msg)
    (subfn,) = struct.unpack(">H", pdu[1:3])
    if subfn != _DIAG_SUBFN_RETURN_QUERY_DATA:
        msg = f"FC 0x08 response: expected sub-function 0x0000, got {subfn:#06x}"
        raise ProtocolError(msg)
    return pdu[3:5]


# ---------------------------------------------------------------------------
# Server side — request decoders and response encoders.
#
# The inverse of the client pair above, for code that answers requests: test
# slaves, simulators, gateways. Request decoders raise ProtocolError when the
# request is malformed or its quantity is outside the spec's range; a server
# answers those with exception code 0x03 (ILLEGAL DATA VALUE), per the request
# state diagrams in *app §6.x*. Response encoders raise ConfigurationError
# for out-of-range arguments, like the request encoders.
# ---------------------------------------------------------------------------

# Length of the FC 0x0F / 0x10 request PDU before its data: fc(1) +
# address(2) + quantity(2) + byte_count(1).
_WRITE_MULTIPLE_REQUEST_PREFIX_LEN = 6


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadRequest:
    """A decoded FC 0x01-0x04 request: read ``count`` items starting at ``address``."""

    address: int
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteSingleCoilRequest:
    """A decoded FC 0x05 request: set the coil at ``address`` on or off."""

    address: int
    on: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteSingleRegisterRequest:
    """A decoded FC 0x06 request: write ``value`` to the register at ``address``."""

    address: int
    value: int


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteMultipleCoilsRequest:
    """A decoded FC 0x0F request: write ``values`` to consecutive coils from ``address``."""

    address: int
    values: tuple[bool, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteMultipleRegistersRequest:
    """A decoded FC 0x10 request: write ``values`` to consecutive registers from ``address``."""

    address: int
    values: tuple[int, ...]


def _decode_read_request(pdu: bytes, fc: FunctionCode, max_count: int) -> ReadRequest:
    _check_exact_length(pdu, 5, fc=fc, what="request")
    _check_fc(pdu, fc)
    _, address, count = struct.unpack(">BHH", pdu)
    if not (_MIN_QUANTITY <= count <= max_count):
        msg = f"FC {fc:#04x} request: quantity {count} outside spec range [1, {max_count}]"
        raise ProtocolError(msg)
    return ReadRequest(address=address, count=count)


def decode_read_coils_request(pdu: bytes) -> ReadRequest:
    """FC 0x01 — Read Coils request. Inverse of :func:`encode_read_coils_request`."""
    return _decode_read_request(pdu, FunctionCode.READ_COILS, _MAX_READ_BITS)


def decode_read_discrete_inputs_request(pdu: bytes) -> ReadRequest:
    """FC 0x02 — Read Discrete Inputs request."""
    return _decode_read_request(pdu, FunctionCode.READ_DISCRETE_INPUTS, _MAX_READ_BITS)


def decode_read_holding_registers_request(pdu: bytes) -> ReadRequest:
    """FC 0x03 — Read Holding Registers request."""
    return _decode_read_request(pdu, FunctionCode.READ_HOLDING_REGISTERS, _MAX_READ_REGISTERS)


def decode_read_input_registers_request(pdu: bytes) -> ReadRequest:
    """FC 0x04 — Read Input Registers request."""
    return _decode_read_request(pdu, FunctionCode.READ_INPUT_REGISTERS, _MAX_READ_REGISTERS)


def decode_write_single_coil_request(pdu: bytes) -> WriteSingleCoilRequest:
    """FC 0x05 — Write Single Coil request.

    The wire value must be ``0xFF00`` or ``0x0000`` (*app §6.5*); any other
    value raises :class:`ProtocolError`.
    """
    _check_exact_length(pdu, 5, fc=FunctionCode.WRITE_SINGLE_COIL, what="request")
    _check_fc(pdu, FunctionCode.WRITE_SINGLE_COIL)
    _, address, value = struct.unpack(">BHH", pdu)
    if value not in (_COIL_ON, _COIL_OFF):
        msg = f"FC 0x05 request: value must be 0xFF00 or 0x0000 per app §6.5 (got {value:#06x})"
        raise ProtocolError(msg)
    return WriteSingleCoilRequest(address=address, on=value == _COIL_ON)


def decode_write_single_register_request(pdu: bytes) -> WriteSingleRegisterRequest:
    """FC 0x06 — Write Single Register request."""
    _check_exact_length(pdu, 5, fc=FunctionCode.WRITE_SINGLE_REGISTER, what="request")
    _check_fc(pdu, FunctionCode.WRITE_SINGLE_REGISTER)
    _, address, value = struct.unpack(">BHH", pdu)
    return WriteSingleRegisterRequest(address=address, value=value)


def _decode_write_multiple_request(
    pdu: bytes, fc: FunctionCode, *, max_count: int, bits_per_item: int
) -> tuple[int, int, bytes]:
    """Validate an FC 0x0F / 0x10 request; return ``(address, count, data)``.

    ``bits_per_item`` is 1 for coils and 16 for registers; the byte_count
    must be exactly the bytes ``count`` items need.
    """
    _check_fc(pdu, fc)
    if len(pdu) < _WRITE_MULTIPLE_REQUEST_PREFIX_LEN:
        msg = (
            f"FC {fc:#04x} request: PDU too short "
            f"(need at least {_WRITE_MULTIPLE_REQUEST_PREFIX_LEN} bytes, got {len(pdu)})"
        )
        raise ProtocolError(msg)
    _, address, count, byte_count = struct.unpack(">BHHB", pdu[:_WRITE_MULTIPLE_REQUEST_PREFIX_LEN])
    data = pdu[_WRITE_MULTIPLE_REQUEST_PREFIX_LEN:]
    if not (_MIN_QUANTITY <= count <= max_count):
        msg = f"FC {fc:#04x} request: quantity {count} outside spec range [1, {max_count}]"
        raise ProtocolError(msg)
    expected_byte_count = (count * bits_per_item + 7) // 8
    if byte_count != expected_byte_count or len(data) != byte_count:
        msg = (
            f"FC {fc:#04x} request: byte_count={byte_count} with {len(data)} data byte(s) "
            f"disagrees with quantity {count} (expected byte_count={expected_byte_count})"
        )
        raise ProtocolError(msg)
    return address, count, data


def decode_write_multiple_coils_request(pdu: bytes) -> WriteMultipleCoilsRequest:
    """FC 0x0F — Write Multiple Coils request."""
    address, count, data = _decode_write_multiple_request(
        pdu, FunctionCode.WRITE_MULTIPLE_COILS, max_count=_MAX_WRITE_COILS, bits_per_item=1
    )
    return WriteMultipleCoilsRequest(address=address, values=_unpack_bits(data, count))


def decode_write_multiple_registers_request(pdu: bytes) -> WriteMultipleRegistersRequest:
    """FC 0x10 — Write Multiple Registers request."""
    address, count, data = _decode_write_multiple_request(
        pdu,
        FunctionCode.WRITE_MULTIPLE_REGISTERS,
        max_count=_MAX_WRITE_REGISTERS,
        bits_per_item=16,
    )
    return WriteMultipleRegistersRequest(address=address, values=struct.unpack(f">{count}H", data))


def decode_diagnostic_loopback_request(pdu: bytes) -> bytes:
    """FC 0x08 sub 0x0000 request; return its 2-byte data word.

    Raises :class:`ProtocolError` for another sub-function or a malformed PDU.
    """
    _check_exact_length(pdu, _DIAG_RESPONSE_PDU_LEN, fc=FunctionCode.DIAGNOSTICS, what="request")
    _check_fc(pdu, FunctionCode.DIAGNOSTICS)
    (subfn,) = struct.unpack(">H", pdu[1:3])
    if subfn != _DIAG_SUBFN_RETURN_QUERY_DATA:
        msg = f"FC 0x08 request: expected sub-function 0x0000, got {subfn:#06x}"
        raise ProtocolError(msg)
    return pdu[3:5]


def _encode_read_bits_response(fc: FunctionCode, values: Sequence[bool]) -> bytes:
    _check_quantity(len(values), max_value=_MAX_READ_BITS, kind=f"FC {fc:#04x}")
    packed = _pack_bits(values)
    return bytes((fc, len(packed))) + packed


def encode_read_coils_response(values: Sequence[bool]) -> bytes:
    """FC 0x01 — Read Coils response carrying ``values``."""
    return _encode_read_bits_response(FunctionCode.READ_COILS, values)


def encode_read_discrete_inputs_response(values: Sequence[bool]) -> bytes:
    """FC 0x02 — Read Discrete Inputs response carrying ``values``."""
    return _encode_read_bits_response(FunctionCode.READ_DISCRETE_INPUTS, values)


def _encode_read_registers_response(fc: FunctionCode, values: Sequence[int]) -> bytes:
    count = len(values)
    _check_quantity(count, max_value=_MAX_READ_REGISTERS, kind=f"FC {fc:#04x}")
    for v in values:
        _check_register_value(v)
    return bytes((fc, 2 * count)) + struct.pack(f">{count}H", *values)


def encode_read_holding_registers_response(values: Sequence[int]) -> bytes:
    """FC 0x03 — Read Holding Registers response carrying ``values``."""
    return _encode_read_registers_response(FunctionCode.READ_HOLDING_REGISTERS, values)


def encode_read_input_registers_response(values: Sequence[int]) -> bytes:
    """FC 0x04 — Read Input Registers response carrying ``values``."""
    return _encode_read_registers_response(FunctionCode.READ_INPUT_REGISTERS, values)


def encode_write_single_coil_response(address: int, *, on: bool) -> bytes:
    """FC 0x05 — Write Single Coil response: an echo of the request (*app §6.5*)."""
    return encode_write_single_coil_request(address, on=on)


def encode_write_single_register_response(address: int, value: int) -> bytes:
    """FC 0x06 — Write Single Register response: an echo of the request (*app §6.6*)."""
    return encode_write_single_register_request(address, value)


def encode_write_multiple_coils_response(address: int, count: int) -> bytes:
    """FC 0x0F — Write Multiple Coils response echoing ``address`` and ``count``."""
    _check_address(address)
    _check_quantity(count, max_value=_MAX_WRITE_COILS, kind="FC 0x0f")
    return struct.pack(">BHH", FunctionCode.WRITE_MULTIPLE_COILS, address, count)


def encode_write_multiple_registers_response(address: int, count: int) -> bytes:
    """FC 0x10 — Write Multiple Registers response echoing ``address`` and ``count``."""
    _check_address(address)
    _check_quantity(count, max_value=_MAX_WRITE_REGISTERS, kind="FC 0x10")
    return struct.pack(">BHH", FunctionCode.WRITE_MULTIPLE_REGISTERS, address, count)


def encode_diagnostic_loopback_response(data: bytes = b"\x00\x00") -> bytes:
    """FC 0x08 sub 0x0000 response: an echo of the request carrying ``data``."""
    return encode_diagnostic_loopback_request(data)


_MAX_FUNCTION_CODE = 0x7F
_MAX_EXCEPTION_CODE = 0xFF


def encode_exception_response(function_code: int, exception_code: int) -> bytes:
    """Exception response PDU: ``function_code | 0x80`` then ``exception_code`` (*app §7*).

    Args:
        function_code: The request's function code, 1-127.
        exception_code: The exception code, e.g. an :class:`ExceptionCode`.
    """
    if not (1 <= function_code <= _MAX_FUNCTION_CODE):
        msg = f"function_code must be in [1, 0x7F] (got {function_code!r})"
        raise ConfigurationError(msg)
    if not (0 <= exception_code <= _MAX_EXCEPTION_CODE):
        msg = f"exception_code must be in [0, 0xFF] (got {exception_code!r})"
        raise ConfigurationError(msg)
    return bytes((function_code | 0x80, exception_code))


__all__ = [
    "ReadRequest",
    "WriteMultipleCoilsRequest",
    "WriteMultipleRegistersRequest",
    "WriteSingleCoilRequest",
    "WriteSingleRegisterRequest",
    "decode_diagnostic_loopback_request",
    "decode_diagnostic_loopback_response",
    "decode_read_coils_request",
    "decode_read_coils_response",
    "decode_read_discrete_inputs_request",
    "decode_read_discrete_inputs_response",
    "decode_read_holding_registers_request",
    "decode_read_holding_registers_response",
    "decode_read_input_registers_request",
    "decode_read_input_registers_response",
    "decode_write_multiple_coils_request",
    "decode_write_multiple_coils_response",
    "decode_write_multiple_registers_request",
    "decode_write_multiple_registers_response",
    "decode_write_single_coil_request",
    "decode_write_single_coil_response",
    "decode_write_single_register_request",
    "decode_write_single_register_response",
    "encode_diagnostic_loopback_request",
    "encode_diagnostic_loopback_response",
    "encode_exception_response",
    "encode_read_coils_request",
    "encode_read_coils_response",
    "encode_read_discrete_inputs_request",
    "encode_read_discrete_inputs_response",
    "encode_read_holding_registers_request",
    "encode_read_holding_registers_response",
    "encode_read_input_registers_request",
    "encode_read_input_registers_response",
    "encode_write_multiple_coils_request",
    "encode_write_multiple_coils_response",
    "encode_write_multiple_registers_request",
    "encode_write_multiple_registers_response",
    "encode_write_single_coil_request",
    "encode_write_single_coil_response",
    "encode_write_single_register_request",
    "encode_write_single_register_response",
]
