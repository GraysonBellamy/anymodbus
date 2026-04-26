"""Smoke test: the public API imports and the version is set."""

from __future__ import annotations

import anymodbus


def test_version_is_set() -> None:
    assert isinstance(anymodbus.__version__, str)
    assert anymodbus.__version__


def test_public_classes_importable() -> None:
    assert anymodbus.Bus is not None
    assert anymodbus.Slave is not None
    assert anymodbus.BusConfig is not None
    assert anymodbus.RetryPolicy is not None
    assert anymodbus.TimingConfig is not None


def test_exception_hierarchy_root() -> None:
    assert issubclass(anymodbus.ProtocolError, anymodbus.ModbusError)
    assert issubclass(anymodbus.CRCError, anymodbus.ProtocolError)
    assert issubclass(anymodbus.FrameTimeoutError, TimeoutError)
    assert issubclass(anymodbus.IllegalFunctionError, anymodbus.ModbusExceptionResponse)


def test_function_code_helpers() -> None:
    assert anymodbus.is_read_function(anymodbus.FunctionCode.READ_HOLDING_REGISTERS)
    assert anymodbus.is_write_function(anymodbus.FunctionCode.WRITE_SINGLE_REGISTER)
    assert not anymodbus.is_read_function(anymodbus.FunctionCode.WRITE_SINGLE_COIL)
