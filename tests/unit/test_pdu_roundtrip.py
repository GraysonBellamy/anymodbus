"""Hypothesis property tests for PDU encode/decode pairs.

For each function code, this asserts:
1. The encoder accepts valid inputs across the spec's full range and produces
   a well-shaped PDU.
2. The decoder accepts a synthesized response (built by hand from the same
   inputs) and round-trips them back.
3. Encoders raise ``ValueError`` on out-of-range inputs.
4. Decoders raise :class:`ProtocolError` on truncated, oversized, or
   byte-count-inconsistent payloads.
"""

from __future__ import annotations

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anymodbus._types import FunctionCode
from anymodbus.exceptions import ProtocolError
from anymodbus.pdu import (
    decode_read_coils_response,
    decode_read_discrete_inputs_response,
    decode_read_holding_registers_response,
    decode_read_input_registers_response,
    decode_write_multiple_coils_response,
    decode_write_multiple_registers_response,
    decode_write_single_coil_response,
    decode_write_single_register_response,
    encode_read_coils_request,
    encode_read_discrete_inputs_request,
    encode_read_holding_registers_request,
    encode_read_input_registers_request,
    encode_write_multiple_coils_request,
    encode_write_multiple_registers_request,
    encode_write_single_coil_request,
    encode_write_single_register_request,
)

# Hypothesis strategies — kept narrow so tests run fast in CI.
addresses = st.integers(min_value=0, max_value=0xFFFF)
register_values = st.integers(min_value=0, max_value=0xFFFF)
read_bit_counts = st.integers(min_value=1, max_value=2000)
read_register_counts = st.integers(min_value=1, max_value=125)
write_coil_counts = st.integers(min_value=1, max_value=1968)
write_register_counts = st.integers(min_value=1, max_value=123)


def _pack_bits_lsb_first(bits: list[bool]) -> bytes:
    """Replicate the pdu module's coil packing for synthesizing fake responses.

    Coil 0 → bit 0 of byte 0; coil 7 → bit 7 of byte 0; coil 8 → bit 0 of
    byte 1; etc. Trailing bits in the final byte are zero.
    """
    nbytes = (len(bits) + 7) // 8
    out = bytearray(nbytes)
    for i, b in enumerate(bits):
        if b:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


# ---------------------------------------------------------------------------
# Read-bits: FC 0x01, FC 0x02
# ---------------------------------------------------------------------------


class TestReadCoilsRoundtrip:
    @given(address=addresses, count=read_bit_counts)
    def test_request_shape(self, address: int, count: int) -> None:
        pdu = encode_read_coils_request(address, count)
        fc, addr, qty = struct.unpack(">BHH", pdu)
        assert fc == FunctionCode.READ_COILS
        assert addr == address
        assert qty == count

    @given(count=read_bit_counts, data=st.data())
    def test_response_roundtrip(self, count: int, data: st.DataObject) -> None:
        bits = data.draw(st.lists(st.booleans(), min_size=count, max_size=count))
        packed = _pack_bits_lsb_first(bits)
        pdu = bytes([FunctionCode.READ_COILS, len(packed)]) + packed
        decoded = decode_read_coils_response(pdu, expected_count=count)
        assert decoded == tuple(bits)

    def test_count_too_low_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            encode_read_coils_request(0, 0)

    def test_count_too_high_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            encode_read_coils_request(0, 2001)

    def test_address_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="address"):
            encode_read_coils_request(0x10000, 1)

    def test_address_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="address"):
            encode_read_coils_request(-1, 1)

    def test_response_truncated_rejected(self) -> None:
        # byte_count says 3 but PDU has only 2 data bytes
        pdu = bytes([FunctionCode.READ_COILS, 0x03, 0xFF, 0xFF])
        with pytest.raises(ProtocolError):
            decode_read_coils_response(pdu, expected_count=24)

    def test_response_byte_count_mismatch_rejected(self) -> None:
        # expected_count=3 → expected byte_count=1, but slave reported 2
        pdu = bytes([FunctionCode.READ_COILS, 0x02, 0xFF, 0x00])
        with pytest.raises(ProtocolError):
            decode_read_coils_response(pdu, expected_count=3)

    def test_response_wrong_fc_rejected(self) -> None:
        # FC 0x02 in a 0x01 decoder → reject
        pdu = bytes([FunctionCode.READ_DISCRETE_INPUTS, 0x01, 0x00])
        with pytest.raises(ProtocolError, match="FC"):
            decode_read_coils_response(pdu, expected_count=1)


class TestReadDiscreteInputsRoundtrip:
    @given(address=addresses, count=read_bit_counts)
    def test_request_shape(self, address: int, count: int) -> None:
        pdu = encode_read_discrete_inputs_request(address, count)
        fc, addr, qty = struct.unpack(">BHH", pdu)
        assert fc == FunctionCode.READ_DISCRETE_INPUTS
        assert addr == address
        assert qty == count

    @given(count=read_bit_counts, data=st.data())
    def test_response_roundtrip(self, count: int, data: st.DataObject) -> None:
        bits = data.draw(st.lists(st.booleans(), min_size=count, max_size=count))
        packed = _pack_bits_lsb_first(bits)
        pdu = bytes([FunctionCode.READ_DISCRETE_INPUTS, len(packed)]) + packed
        decoded = decode_read_discrete_inputs_response(pdu, expected_count=count)
        assert decoded == tuple(bits)


# ---------------------------------------------------------------------------
# Read-registers: FC 0x03, FC 0x04
# ---------------------------------------------------------------------------


class TestReadHoldingRegistersRoundtrip:
    @given(address=addresses, count=read_register_counts)
    def test_request_shape(self, address: int, count: int) -> None:
        pdu = encode_read_holding_registers_request(address, count)
        fc, addr, qty = struct.unpack(">BHH", pdu)
        assert fc == FunctionCode.READ_HOLDING_REGISTERS
        assert addr == address
        assert qty == count

    @given(count=read_register_counts, data=st.data())
    def test_response_roundtrip(self, count: int, data: st.DataObject) -> None:
        regs = data.draw(st.lists(register_values, min_size=count, max_size=count).map(tuple))
        body = struct.pack(f">{count}H", *regs)
        pdu = bytes([FunctionCode.READ_HOLDING_REGISTERS, 2 * count]) + body
        decoded = decode_read_holding_registers_response(pdu)
        assert decoded == regs

    def test_count_too_high_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            encode_read_holding_registers_request(0, 126)

    def test_count_too_low_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            encode_read_holding_registers_request(0, 0)

    def test_response_odd_byte_count_rejected(self) -> None:
        pdu = bytes([FunctionCode.READ_HOLDING_REGISTERS, 0x03, 0x00, 0x00, 0x00])
        with pytest.raises(ProtocolError, match="even"):
            decode_read_holding_registers_response(pdu)

    def test_response_zero_byte_count_rejected(self) -> None:
        pdu = bytes([FunctionCode.READ_HOLDING_REGISTERS, 0x00])
        with pytest.raises(ProtocolError):
            decode_read_holding_registers_response(pdu)

    def test_response_truncated_rejected(self) -> None:
        # byte_count says 4 but only 2 follow
        pdu = bytes([FunctionCode.READ_HOLDING_REGISTERS, 0x04, 0x00, 0x01])
        with pytest.raises(ProtocolError):
            decode_read_holding_registers_response(pdu)

    def test_response_oversize_rejected(self) -> None:
        # byte_count says 2 but 4 follow
        pdu = bytes([FunctionCode.READ_HOLDING_REGISTERS, 0x02, 0x00, 0x01, 0x00, 0x02])
        with pytest.raises(ProtocolError):
            decode_read_holding_registers_response(pdu)


class TestReadInputRegistersRoundtrip:
    @given(address=addresses, count=read_register_counts)
    def test_request_shape(self, address: int, count: int) -> None:
        pdu = encode_read_input_registers_request(address, count)
        fc, addr, qty = struct.unpack(">BHH", pdu)
        assert fc == FunctionCode.READ_INPUT_REGISTERS
        assert addr == address
        assert qty == count

    @given(count=read_register_counts, data=st.data())
    def test_response_roundtrip(self, count: int, data: st.DataObject) -> None:
        regs = data.draw(st.lists(register_values, min_size=count, max_size=count).map(tuple))
        body = struct.pack(f">{count}H", *regs)
        pdu = bytes([FunctionCode.READ_INPUT_REGISTERS, 2 * count]) + body
        decoded = decode_read_input_registers_response(pdu)
        assert decoded == regs


# ---------------------------------------------------------------------------
# Write-single: FC 0x05, FC 0x06 (response echoes the request bytes verbatim)
# ---------------------------------------------------------------------------


class TestWriteSingleCoilRoundtrip:
    @given(address=addresses, on=st.booleans())
    def test_request_then_decode_echo(self, address: int, on: bool) -> None:
        pdu = encode_write_single_coil_request(address, on=on)
        # Slave echoes the request as the response.
        decoded_addr, decoded_on = decode_write_single_coil_response(pdu)
        assert decoded_addr == address
        assert decoded_on is on

    def test_request_uses_canonical_wire_values(self) -> None:
        # Per app §6.5: ON = 0xFF00, OFF = 0x0000 (and nothing else).
        on_pdu = encode_write_single_coil_request(0x0000, on=True)
        off_pdu = encode_write_single_coil_request(0x0000, on=False)
        assert struct.unpack(">BHH", on_pdu) == (FunctionCode.WRITE_SINGLE_COIL, 0, 0xFF00)
        assert struct.unpack(">BHH", off_pdu) == (FunctionCode.WRITE_SINGLE_COIL, 0, 0x0000)

    def test_response_with_invalid_value_rejected(self) -> None:
        # Per app §6.5: only 0xFF00 / 0x0000 are legal — strict equality.
        bad = struct.pack(">BHH", FunctionCode.WRITE_SINGLE_COIL, 0x00AC, 0x0001)
        with pytest.raises(ProtocolError, match="0xFF00"):
            decode_write_single_coil_response(bad)
        bad2 = struct.pack(">BHH", FunctionCode.WRITE_SINGLE_COIL, 0x00AC, 0xFF01)
        with pytest.raises(ProtocolError):
            decode_write_single_coil_response(bad2)

    def test_response_too_short_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            decode_write_single_coil_response(b"\x05\x00\x00\xff")


class TestWriteSingleRegisterRoundtrip:
    @given(address=addresses, value=register_values)
    def test_request_then_decode_echo(self, address: int, value: int) -> None:
        pdu = encode_write_single_register_request(address, value)
        decoded_addr, decoded_value = decode_write_single_register_response(pdu)
        assert decoded_addr == address
        assert decoded_value == value

    def test_value_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="register value"):
            encode_write_single_register_request(0, 0x10000)

    def test_value_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="register value"):
            encode_write_single_register_request(0, -1)


# ---------------------------------------------------------------------------
# Write-multiple: FC 0x0F, FC 0x10 (response carries only address + count)
# ---------------------------------------------------------------------------


class TestWriteMultipleCoilsRoundtrip:
    @given(address=addresses, count=write_coil_counts, data=st.data())
    def test_request_shape(self, address: int, count: int, data: st.DataObject) -> None:
        bits = data.draw(st.lists(st.booleans(), min_size=count, max_size=count))
        pdu = encode_write_multiple_coils_request(address, bits)
        fc, addr, qty, byte_count = struct.unpack(">BHHB", pdu[:6])
        assert fc == FunctionCode.WRITE_MULTIPLE_COILS
        assert addr == address
        assert qty == count
        assert byte_count == (count + 7) // 8
        assert len(pdu) == 6 + byte_count

    @given(address=addresses, count=write_coil_counts)
    def test_synthesized_response_decodes(self, address: int, count: int) -> None:
        # Slave's echo response carries only (FC, addr, count).
        response = struct.pack(">BHH", FunctionCode.WRITE_MULTIPLE_COILS, address, count)
        decoded_addr, decoded_count = decode_write_multiple_coils_response(response)
        assert decoded_addr == address
        assert decoded_count == count

    def test_count_too_high_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            encode_write_multiple_coils_request(0, [True] * 1969)

    def test_count_too_low_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            encode_write_multiple_coils_request(0, [])

    def test_response_count_out_of_range_rejected(self) -> None:
        # Slave says count=2000 (> 1968 max for FC 0x0F) — clip-to-spec sanity check.
        bad = struct.pack(">BHH", FunctionCode.WRITE_MULTIPLE_COILS, 0, 2000)
        with pytest.raises(ProtocolError):
            decode_write_multiple_coils_response(bad)


class TestWriteMultipleRegistersRoundtrip:
    @given(address=addresses, count=write_register_counts, data=st.data())
    def test_request_shape(self, address: int, count: int, data: st.DataObject) -> None:
        regs = data.draw(st.lists(register_values, min_size=count, max_size=count))
        pdu = encode_write_multiple_registers_request(address, regs)
        fc, addr, qty, byte_count = struct.unpack(">BHHB", pdu[:6])
        assert fc == FunctionCode.WRITE_MULTIPLE_REGISTERS
        assert addr == address
        assert qty == count
        assert byte_count == 2 * count
        # Body should be the registers in big-endian.
        assert struct.unpack(f">{count}H", pdu[6:]) == tuple(regs)

    @given(address=addresses, count=write_register_counts)
    def test_synthesized_response_decodes(self, address: int, count: int) -> None:
        response = struct.pack(">BHH", FunctionCode.WRITE_MULTIPLE_REGISTERS, address, count)
        decoded_addr, decoded_count = decode_write_multiple_registers_response(response)
        assert decoded_addr == address
        assert decoded_count == count

    def test_count_too_high_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            encode_write_multiple_registers_request(0, [0] * 124)

    def test_register_value_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="register value"):
            encode_write_multiple_registers_request(0, [0xFFFF, 0x10000])

    def test_response_count_out_of_range_rejected(self) -> None:
        bad = struct.pack(">BHH", FunctionCode.WRITE_MULTIPLE_REGISTERS, 0, 124)
        with pytest.raises(ProtocolError):
            decode_write_multiple_registers_response(bad)


# ---------------------------------------------------------------------------
# Cross-FC: every decoder must reject empty PDUs and wrong FC bytes.
# ---------------------------------------------------------------------------


def _coils_decoder(pdu: bytes) -> object:
    return decode_read_coils_response(pdu, expected_count=1)


def _discrete_inputs_decoder(pdu: bytes) -> object:
    return decode_read_discrete_inputs_response(pdu, expected_count=1)


class TestDecoderInputDefenses:
    @pytest.mark.parametrize(
        "decoder",
        [
            _coils_decoder,
            _discrete_inputs_decoder,
            decode_read_holding_registers_response,
            decode_read_input_registers_response,
            decode_write_single_coil_response,
            decode_write_single_register_response,
            decode_write_multiple_coils_response,
            decode_write_multiple_registers_response,
        ],
    )
    def test_empty_pdu_rejected(self, decoder: object) -> None:
        with pytest.raises(ProtocolError):
            decoder(b"")  # type: ignore[operator]
