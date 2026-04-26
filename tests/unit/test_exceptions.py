"""Tests for the exception hierarchy and ``code_to_exception`` translator."""

from __future__ import annotations

import anyio
import pytest

from anymodbus.exceptions import (
    AcknowledgeError,
    BusClosedError,
    ConfigurationError,
    ConnectionLostError,
    CRCError,
    FrameTimeoutError,
    GatewayPathUnavailableError,
    GatewayTargetFailedToRespondError,
    IllegalDataAddressError,
    IllegalDataValueError,
    IllegalFunctionError,
    MemoryParityError,
    ModbusError,
    ModbusExceptionResponse,
    ModbusUnknownExceptionError,
    ModbusUnsupportedFunctionError,
    ProtocolError,
    SlaveDeviceBusyError,
    SlaveDeviceFailureError,
    code_to_exception,
    is_exception_response,
    strip_exception_bit,
)


class TestMultiInheritance:
    """Each exception is also catchable via stdlib / AnyIO bases."""

    def test_protocol_error_is_value_error(self) -> None:
        with pytest.raises(ValueError):
            raise ProtocolError("bad")

    def test_crc_error_is_protocol_error(self) -> None:
        with pytest.raises(ProtocolError):
            raise CRCError("bad CRC")

    def test_frame_timeout_is_timeout_error(self) -> None:
        with pytest.raises(TimeoutError):
            raise FrameTimeoutError("timed out")

    def test_connection_lost_is_anyio_broken(self) -> None:
        with pytest.raises(anyio.BrokenResourceError):
            raise ConnectionLostError("lost")

    def test_bus_closed_is_anyio_closed(self) -> None:
        with pytest.raises(anyio.ClosedResourceError):
            raise BusClosedError("closed")

    def test_configuration_error_is_value_error(self) -> None:
        with pytest.raises(ValueError):
            raise ConfigurationError("bad")

    def test_configuration_error_is_modbus_error(self) -> None:
        with pytest.raises(ModbusError):
            raise ConfigurationError("bad")

    def test_unsupported_function_is_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            raise ModbusUnsupportedFunctionError("FC 0x07 not implemented in v0.1")

    def test_unsupported_function_is_modbus_error(self) -> None:
        with pytest.raises(ModbusError):
            raise ModbusUnsupportedFunctionError("FC 0x07 not implemented in v0.1")


class TestCodeToException:
    @pytest.mark.parametrize(
        ("code", "cls"),
        [
            (0x01, IllegalFunctionError),
            (0x02, IllegalDataAddressError),
            (0x03, IllegalDataValueError),
            (0x04, SlaveDeviceFailureError),
            (0x05, AcknowledgeError),
            (0x06, SlaveDeviceBusyError),
            (0x08, MemoryParityError),
            (0x0A, GatewayPathUnavailableError),
            (0x0B, GatewayTargetFailedToRespondError),
        ],
    )
    def test_known_codes_dispatch_to_subclass(
        self, code: int, cls: type[ModbusExceptionResponse]
    ) -> None:
        exc = code_to_exception(function_code=0x03, exception_code=code)
        assert isinstance(exc, cls)
        assert exc.exception_code == code
        assert exc.function_code == 0x03

    @pytest.mark.parametrize("code", [0x07, 0x09, 0x0C, 0x42, 0xFE, 0xFF])
    def test_unassigned_codes_surface_as_unknown(self, code: int) -> None:
        # Per app §7, codes outside {1-6, 8, 10, 11} are unassigned in v1.1b3.
        # Notably 0x07 — pre-v1.1 Modicon "Negative Acknowledge" — surfaces
        # here, not as a dedicated class.
        exc = code_to_exception(function_code=0x03, exception_code=code)
        assert isinstance(exc, ModbusUnknownExceptionError)
        assert exc.code == code
        assert exc.function_code == 0x03

    def test_unknown_exception_is_modbus_error_not_protocol_error(self) -> None:
        exc = code_to_exception(function_code=0x03, exception_code=0x07)
        assert isinstance(exc, ModbusError)
        # Frame was well-formed; the slave returned a code we don't recognise.
        # That's not a wire-level protocol violation.
        assert not isinstance(exc, ProtocolError)

    def test_unknown_exception_is_not_a_modbus_exception_response(self) -> None:
        # The standalone class doesn't pretend to be one of the named §7 codes.
        exc = code_to_exception(function_code=0x03, exception_code=0x07)
        assert not isinstance(exc, ModbusExceptionResponse)

    def test_all_exception_responses_are_modbus_errors(self) -> None:
        exc = code_to_exception(function_code=0x03, exception_code=0x02)
        assert isinstance(exc, ModbusError)
        # Notably NOT a ProtocolError — slave-side semantic outcomes are
        # distinct from wire-level protocol violations.
        assert not isinstance(exc, ProtocolError)

    def test_message_is_descriptive(self) -> None:
        exc = code_to_exception(function_code=0x10, exception_code=0x02)
        assert "0x02" in str(exc)
        assert "0x10" in str(exc)

    def test_unknown_message_includes_raw_code(self) -> None:
        exc = code_to_exception(function_code=0x10, exception_code=0x07)
        assert "0x07" in str(exc)
        assert "0x10" in str(exc)


class TestExceptionBitHelpers:
    def test_is_exception_response_true_for_high_bit(self) -> None:
        assert is_exception_response(0x83)
        assert is_exception_response(0x90)
        assert is_exception_response(0xFF)

    def test_is_exception_response_false_for_normal(self) -> None:
        assert not is_exception_response(0x03)
        assert not is_exception_response(0x10)
        assert not is_exception_response(0x00)

    def test_strip_exception_bit(self) -> None:
        assert strip_exception_bit(0x83) == 0x03
        assert strip_exception_bit(0x90) == 0x10
        assert strip_exception_bit(0x86) == 0x06
