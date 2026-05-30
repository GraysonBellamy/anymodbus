"""FC 0x08 sub-0x0000 (Return Query Data / loopback) PDU codec tests."""

from __future__ import annotations

import pytest

from anymodbus.exceptions import ProtocolError
from anymodbus.pdu import (
    decode_diagnostic_loopback_response,
    encode_diagnostic_loopback_request,
)


def test_encode_default_zero_word() -> None:
    assert encode_diagnostic_loopback_request() == bytes([0x08, 0x00, 0x00, 0x00, 0x00])


def test_encode_known_data() -> None:
    assert encode_diagnostic_loopback_request(b"\xab\xcd") == bytes([0x08, 0x00, 0x00, 0xAB, 0xCD])


@pytest.mark.parametrize("data", [b"", b"\x00", b"\x00\x00\x00"])
def test_encode_rejects_non_2_byte_data(data: bytes) -> None:
    with pytest.raises(ValueError, match="exactly 2 bytes"):
        encode_diagnostic_loopback_request(data)


def test_decode_round_trip() -> None:
    pdu = encode_diagnostic_loopback_request(b"\xab\xcd")
    assert decode_diagnostic_loopback_response(pdu) == b"\xab\xcd"


def test_decode_rejects_wrong_fc() -> None:
    with pytest.raises(ProtocolError):
        decode_diagnostic_loopback_response(bytes([0x03, 0x00, 0x00, 0xAB, 0xCD]))


def test_decode_rejects_wrong_length() -> None:
    with pytest.raises(ProtocolError, match="5-byte"):
        decode_diagnostic_loopback_response(bytes([0x08, 0x00, 0x00, 0xAB]))


def test_decode_rejects_non_sub0() -> None:
    with pytest.raises(ProtocolError, match="sub-function"):
        decode_diagnostic_loopback_response(bytes([0x08, 0x00, 0x04, 0xAB, 0xCD]))
