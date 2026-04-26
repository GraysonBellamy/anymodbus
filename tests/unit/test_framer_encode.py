"""Tests for :func:`anymodbus.framer.encode_adu`.

The ADU writer is trivial — slave-address byte, then the PDU, then the
CRC-16/Modbus appended in **little-endian** byte order (low byte first;
*Modbus over Serial Line v1.02 §2.5.1.2*). These tests pin both halves of
that contract against known wire fixtures.
"""

from __future__ import annotations

import pytest

from anymodbus.crc import crc16_modbus_bytes, verify_crc
from anymodbus.framer import encode_adu


class TestEncodeAduShape:
    def test_empty_pdu_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            encode_adu(slave_address=1, pdu=b"")

    def test_negative_address_rejected(self) -> None:
        with pytest.raises(ValueError, match="slave_address"):
            encode_adu(slave_address=-1, pdu=b"\x03\x00\x00\x00\x01")

    def test_address_too_large_rejected(self) -> None:
        with pytest.raises(ValueError, match="slave_address"):
            encode_adu(slave_address=256, pdu=b"\x03\x00\x00\x00\x01")

    def test_address_zero_accepted_for_broadcast(self) -> None:
        # Address 0 is the broadcast slave address; Bus.broadcast_* call sites
        # validate use, but the framer itself must put it on the wire.
        adu = encode_adu(slave_address=0, pdu=b"\x06\x00\x01\x00\x03")
        assert adu[0] == 0x00

    @pytest.mark.parametrize("addr", [1, 100, 247, 248, 255])
    def test_address_at_boundaries(self, addr: int) -> None:
        adu = encode_adu(slave_address=addr, pdu=b"\x03\x00\x00\x00\x01")
        assert adu[0] == addr


class TestKnownWireFixtures:
    """End-to-end ADU bytes for canonical requests."""

    def test_fc03_read_holding_request(self) -> None:
        # Request: slave 0x01, FC 0x03, start 0x006B, count 3.
        # The CRC for this exact request is well-known on Modbus debuggers.
        pdu = bytes([0x03, 0x00, 0x6B, 0x00, 0x03])
        adu = encode_adu(slave_address=0x01, pdu=pdu)
        assert adu[:6] == bytes([0x01, 0x03, 0x00, 0x6B, 0x00, 0x03])
        # CRC is appended low byte first.
        expected_crc = crc16_modbus_bytes(bytes([0x01]) + pdu)
        assert adu[6:] == expected_crc
        assert len(adu) == 8

    def test_fc06_write_single_register(self) -> None:
        # Slave 0x11, FC 0x06, addr 0x0001, value 0x0003. Spec §6.6 example.
        pdu = bytes([0x06, 0x00, 0x01, 0x00, 0x03])
        adu = encode_adu(slave_address=0x11, pdu=pdu)
        assert adu[:6] == bytes([0x11, 0x06, 0x00, 0x01, 0x00, 0x03])
        assert verify_crc(adu)

    def test_fc05_write_single_coil(self) -> None:
        pdu = bytes([0x05, 0x00, 0xAC, 0xFF, 0x00])
        adu = encode_adu(slave_address=0x01, pdu=pdu)
        assert adu[:6] == bytes([0x01, 0x05, 0x00, 0xAC, 0xFF, 0x00])
        assert verify_crc(adu)

    def test_crc_appended_low_byte_first(self) -> None:
        # *serial §2.5.1.2*: low-order byte first, then high-order byte.
        # For input 0x02 0x07 the well-known CRC is 0x1241; on the wire it's
        # appended as 0x41, 0x12.
        adu = encode_adu(slave_address=0x02, pdu=b"\x07")
        assert adu[-2:] == bytes([0x41, 0x12])

    def test_full_frame_self_verifies(self) -> None:
        # crc16_modbus(frame) == 0 is a property of CRC-16; the framer's
        # output should always round-trip through verify_crc.
        for pdu in [
            b"\x03\x00\x00\x00\x01",
            b"\x06\x00\x01\x00\x03",
            b"\x10\x00\x01\x00\x02\x04\x00\x0a\x01\x02",
        ]:
            adu = encode_adu(slave_address=42, pdu=pdu)
            assert verify_crc(adu)
