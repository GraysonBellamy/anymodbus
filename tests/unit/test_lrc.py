"""LRC correctness tests for Modbus ASCII (*serial §6.2*).

The worked vector is the one from the servomex handoff appendix A.2/A.3 and
the implementation plan §17: ``{1E 04 00 00 00 01}`` → ``sum=0x23`` →
``lrc=0xDD``.
"""

from __future__ import annotations

import importlib

from hypothesis import given
from hypothesis import strategies as st

from anymodbus.lrc import lrc8, lrc8_bytes, verify_lrc


def test_worked_vector() -> None:
    payload = bytes.fromhex("1E0400000001")
    assert sum(payload) & 0xFF == 0x23
    assert lrc8(payload) == 0xDD
    assert lrc8_bytes(payload) == b"\xdd"


def test_verify_lrc_round_trip() -> None:
    body = bytes.fromhex("1E0400000001")
    frame = body + lrc8_bytes(body)
    assert verify_lrc(frame)


def test_verify_lrc_rejects_flipped_bit() -> None:
    body = bytes.fromhex("1E0400000001")
    frame = bytearray(body + lrc8_bytes(body))
    frame[-1] ^= 0x01
    assert not verify_lrc(bytes(frame))


def test_verify_lrc_rejects_too_short() -> None:
    assert not verify_lrc(b"")
    assert not verify_lrc(b"\x00")


def test_lrc_zero_payload() -> None:
    # sum(0,0) == 0 -> -0 & 0xFF == 0.
    assert lrc8(b"\x00\x00") == 0x00


def test_accepts_buffer_protocol_inputs() -> None:
    data = bytes.fromhex("1E0400000001")
    assert lrc8(data) == lrc8(bytearray(data))
    assert lrc8(data) == lrc8(memoryview(data))


def test_not_exported_at_top_level() -> None:
    """LRC primitives live at ``anymodbus.lrc``, mirroring ``anymodbus.crc`` (D3)."""
    anymodbus = importlib.import_module("anymodbus")
    for name in ("lrc8", "lrc8_bytes", "verify_lrc"):
        assert not hasattr(anymodbus, name), f"{name} must not be on the top-level surface"


@given(st.binary(max_size=300))
def test_lrc_in_range(data: bytes) -> None:
    assert 0 <= lrc8(data) <= 0xFF


@given(st.binary(min_size=1, max_size=300))
def test_round_trip_property(data: bytes) -> None:
    frame = data + lrc8_bytes(data)
    assert verify_lrc(frame)
