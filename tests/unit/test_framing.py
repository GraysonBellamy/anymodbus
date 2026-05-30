"""Tests for the shared framing seam: ``interpret_response_pdu`` + ``get_framer``.

``interpret_response_pdu`` is the single source of truth for framing-agnostic
function-code semantics (decision D1), so it is unit-tested standalone here; the
RTU and ASCII framers both route response interpretation through it.
"""

from __future__ import annotations

import pytest

from anymodbus._types import Framing, FunctionCode
from anymodbus.exceptions import (
    IllegalDataAddressError,
    IllegalFunctionError,
    ModbusUnknownExceptionError,
    ProtocolError,
    UnexpectedResponseError,
)
from anymodbus.framer import RTU_FRAMER
from anymodbus.framing import get_framer, interpret_response_pdu


class TestInterpretResponsePdu:
    def test_normal_response_passes_through(self) -> None:
        pdu = bytes([0x03, 0x02, 0x00, 0x2A])
        slave, out = interpret_response_pdu(
            slave_address=0x11,
            pdu=pdu,
            expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
        )
        assert slave == 0x11
        assert out == pdu

    def test_fc_zero_rejected(self) -> None:
        with pytest.raises(ProtocolError, match="function code 0"):
            interpret_response_pdu(
                slave_address=0x01,
                pdu=bytes([0x00, 0x00]),
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            )

    def test_exception_bit_dispatches_to_subclass(self) -> None:
        with pytest.raises(IllegalFunctionError) as ei:
            interpret_response_pdu(
                slave_address=0x01,
                pdu=bytes([0x03 | 0x80, 0x01]),
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            )
        assert ei.value.function_code == 0x03
        assert ei.value.exception_code == 0x01

    def test_exception_data_address(self) -> None:
        with pytest.raises(IllegalDataAddressError):
            interpret_response_pdu(
                slave_address=0x01,
                pdu=bytes([0x04 | 0x80, 0x02]),
                expected_function_code=FunctionCode.READ_INPUT_REGISTERS,
            )

    def test_unassigned_exception_code_is_unknown(self) -> None:
        with pytest.raises(ModbusUnknownExceptionError) as ei:
            interpret_response_pdu(
                slave_address=0x01,
                pdu=bytes([0x03 | 0x80, 0x07]),
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            )
        assert ei.value.exception_code == 0x07

    def test_exception_base_fc_mismatch(self) -> None:
        # Sent FC3, exception echoes FC6.
        with pytest.raises(UnexpectedResponseError, match="exception"):
            interpret_response_pdu(
                slave_address=0x01,
                pdu=bytes([0x06 | 0x80, 0x02]),
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            )

    def test_normal_fc_mismatch(self) -> None:
        with pytest.raises(UnexpectedResponseError, match="fc 0x04"):
            interpret_response_pdu(
                slave_address=0x01,
                pdu=bytes([0x04, 0x02, 0x00, 0x00]),
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            )


class TestGetFramer:
    def test_rtu_returns_singleton(self) -> None:
        assert get_framer(Framing.RTU) is RTU_FRAMER

    def test_ascii_returns_singleton(self) -> None:
        from anymodbus.framer_ascii import ASCII_FRAMER  # noqa: PLC0415

        assert get_framer(Framing.ASCII) is ASCII_FRAMER

    def test_each_call_returns_same_instance(self) -> None:
        assert get_framer(Framing.RTU) is get_framer(Framing.RTU)
        assert get_framer(Framing.ASCII) is get_framer(Framing.ASCII)
