"""Hand-rolled PDU byte-string fixtures from *Modbus Application Protocol v1.1b3*.

These pin the encoders/decoders against the spec's worked examples — if a
future refactor changes the wire format silently, these tests fail loudly.
Section references (``app §X.Y``) point to the spec; bytes are quoted
verbatim from the spec's "Request" / "Response" tables.
"""

from __future__ import annotations

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


class TestSpecVectors:
    """Worked examples from *Modbus Application Protocol v1.1b3* §6.x."""

    def test_fc01_read_coils_request_app_6_1(self) -> None:
        # app §6.1: Read 19 coils (0x0013) starting at 0x0013.
        assert encode_read_coils_request(0x0013, 19) == bytes([0x01, 0x00, 0x13, 0x00, 0x13])

    def test_fc01_read_coils_response_app_6_1(self) -> None:
        # app §6.1: 19 coils packed into 3 bytes 0xCD, 0x6B, 0x05.
        # Coils 27..20 = 0xCD = 1100 1101 → bits 0-7 LSB-first:
        #   coil[0..7] = 1,0,1,1,0,0,1,1
        # Coils 35..28 = 0x6B = 0110 1011 → coil[8..15] = 1,1,0,1,0,1,1,0
        # Coils (xxxx)38..36 = 0x05 = 0000 0101 → coil[16..18] = 1,0,1
        expected = (
            True, False, True, True, False, False, True, True,
            True, True, False, True, False, True, True, False,
            True, False, True,
        )  # fmt: skip
        pdu = bytes([0x01, 0x03, 0xCD, 0x6B, 0x05])
        assert decode_read_coils_response(pdu, expected_count=19) == expected

    def test_fc02_read_discrete_inputs_request_app_6_2(self) -> None:
        # app §6.2: Read 22 inputs starting at 0x00C4.
        assert encode_read_discrete_inputs_request(0x00C4, 22) == bytes(
            [0x02, 0x00, 0xC4, 0x00, 0x16]
        )

    def test_fc02_read_discrete_inputs_response_app_6_2(self) -> None:
        # app §6.2: response bytes 0xAC 0xDB 0x35.
        # 0xAC = 1010 1100 → input[0..7] = 0,0,1,1,0,1,0,1
        # 0xDB = 1101 1011 → input[8..15] = 1,1,0,1,1,0,1,1
        # 0x35 = 0011 0101 → input[16..21] = 1,0,1,0,1,1
        expected = (
            False, False, True, True, False, True, False, True,
            True, True, False, True, True, False, True, True,
            True, False, True, False, True, True,
        )  # fmt: skip
        pdu = bytes([0x02, 0x03, 0xAC, 0xDB, 0x35])
        assert decode_read_discrete_inputs_response(pdu, expected_count=22) == expected

    def test_fc03_read_holding_registers_request_app_6_3(self) -> None:
        # app §6.3: Read 3 registers at start address 0x006B (decimal 107).
        assert encode_read_holding_registers_request(0x006B, 3) == bytes(
            [0x03, 0x00, 0x6B, 0x00, 0x03]
        )

    def test_fc03_read_holding_registers_response_app_6_3(self) -> None:
        # app §6.3: 3 registers = 0x022B, 0x0000, 0x0064.
        pdu = bytes([0x03, 0x06, 0x02, 0x2B, 0x00, 0x00, 0x00, 0x64])
        assert decode_read_holding_registers_response(pdu) == (0x022B, 0x0000, 0x0064)

    def test_fc04_read_input_registers_request_app_6_4(self) -> None:
        # app §6.4: Read 1 input register at 0x0008.
        assert encode_read_input_registers_request(0x0008, 1) == bytes(
            [0x04, 0x00, 0x08, 0x00, 0x01]
        )

    def test_fc04_read_input_registers_response_app_6_4(self) -> None:
        # app §6.4: 1 register = 0x000A.
        pdu = bytes([0x04, 0x02, 0x00, 0x0A])
        assert decode_read_input_registers_response(pdu) == (0x000A,)

    def test_fc05_write_single_coil_on_request_app_6_5(self) -> None:
        # app §6.5: Write coil at 0x00AC ON → 0xFF00.
        assert encode_write_single_coil_request(0x00AC, on=True) == bytes(
            [0x05, 0x00, 0xAC, 0xFF, 0x00]
        )

    def test_fc05_write_single_coil_off_request(self) -> None:
        # app §6.5: OFF → 0x0000.
        assert encode_write_single_coil_request(0x00AC, on=False) == bytes(
            [0x05, 0x00, 0xAC, 0x00, 0x00]
        )

    def test_fc05_write_single_coil_response_app_6_5(self) -> None:
        # app §6.5: response echoes request.
        pdu = bytes([0x05, 0x00, 0xAC, 0xFF, 0x00])
        assert decode_write_single_coil_response(pdu) == (0x00AC, True)

    def test_fc06_write_single_register_request_app_6_6(self) -> None:
        # app §6.6: Write 0x0003 to register 0x0001.
        assert encode_write_single_register_request(0x0001, 0x0003) == bytes(
            [0x06, 0x00, 0x01, 0x00, 0x03]
        )

    def test_fc06_write_single_register_response_app_6_6(self) -> None:
        # app §6.6: response echoes request.
        pdu = bytes([0x06, 0x00, 0x01, 0x00, 0x03])
        assert decode_write_single_register_response(pdu) == (0x0001, 0x0003)

    def test_fc0f_write_multiple_coils_request_app_6_11(self) -> None:
        # app §6.11: Write 10 coils starting at 0x0013, value bytes 0xCD 0x01.
        # 0xCD = 1100 1101 → bit positions 0..7 LSB-first:
        #   coil[0..7] = 1,0,1,1,0,0,1,1
        # 0x01 = 0000 0001, only first 2 bits used → coil[8..9] = 1,0
        coils = [
            True, False, True, True, False, False, True, True,
            True, False,
        ]  # fmt: skip
        assert encode_write_multiple_coils_request(0x0013, coils) == bytes(
            [0x0F, 0x00, 0x13, 0x00, 0x0A, 0x02, 0xCD, 0x01]
        )

    def test_fc0f_write_multiple_coils_response_app_6_11(self) -> None:
        pdu = bytes([0x0F, 0x00, 0x13, 0x00, 0x0A])
        assert decode_write_multiple_coils_response(pdu) == (0x0013, 10)

    def test_fc10_write_multiple_registers_request_app_6_12(self) -> None:
        # app §6.12: Write 2 registers (0x000A, 0x0102) starting at 0x0001.
        assert encode_write_multiple_registers_request(0x0001, [0x000A, 0x0102]) == bytes(
            [0x10, 0x00, 0x01, 0x00, 0x02, 0x04, 0x00, 0x0A, 0x01, 0x02]
        )

    def test_fc10_write_multiple_registers_response_app_6_12(self) -> None:
        pdu = bytes([0x10, 0x00, 0x01, 0x00, 0x02])
        assert decode_write_multiple_registers_response(pdu) == (0x0001, 2)
