"""CRC-16/Modbus correctness tests.

Vectors taken from the Modbus Application Protocol spec (v1.1b3) appendix
"CRC Generation" worked example, plus standard reference values for short
inputs and the canonical empty-frame edge case.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anymodbus.crc import crc16_modbus, crc16_modbus_bytes, verify_crc

# (input bytes, expected CRC as int)
KNOWN_VECTORS: list[tuple[bytes, int]] = [
    # Modbus spec worked example: slave 0x02, FC 0x07.
    (bytes([0x02, 0x07]), 0x1241),
    # Universally cited CRC-16/Modbus reference vector.
    (b"123456789", 0x4B37),
    # Single byte 0xFF — by hand calc against the standard table.
    (b"\xff", 0x00FF),
    # Empty input — initial value, unchanged.
    (b"", 0xFFFF),
]


@pytest.mark.parametrize(("data", "expected"), KNOWN_VECTORS)
def test_known_vectors(data: bytes, expected: int) -> None:
    assert crc16_modbus(data) == expected


def test_crc_bytes_is_little_endian() -> None:
    crc = crc16_modbus(b"\x02\x07")
    assert crc == 0x1241
    assert crc16_modbus_bytes(b"\x02\x07") == bytes([0x41, 0x12])


def test_verify_crc_round_trip() -> None:
    body = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A])
    frame = body + crc16_modbus_bytes(body)
    assert verify_crc(frame)


def test_verify_crc_rejects_corruption() -> None:
    body = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A])
    frame = bytearray(body + crc16_modbus_bytes(body))
    frame[-1] ^= 0x01  # flip a bit in the high CRC byte
    assert not verify_crc(bytes(frame))


def test_verify_crc_rejects_too_short() -> None:
    assert not verify_crc(b"")
    assert not verify_crc(b"\x00")
    assert not verify_crc(b"\x00\x00")


def test_accepts_buffer_protocol_inputs() -> None:
    data = b"\x02\x07"
    assert crc16_modbus(data) == crc16_modbus(bytearray(data))
    assert crc16_modbus(data) == crc16_modbus(memoryview(data))


@given(st.binary(max_size=300))
def test_crc_in_range(data: bytes) -> None:
    crc = crc16_modbus(data)
    assert 0 <= crc <= 0xFFFF


@given(st.binary(min_size=1, max_size=300))
def test_round_trip_property(data: bytes) -> None:
    """Any frame with its own CRC appended must verify."""
    frame = data + crc16_modbus_bytes(data)
    assert verify_crc(frame)
