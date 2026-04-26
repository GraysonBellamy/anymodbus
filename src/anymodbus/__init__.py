"""Async-native Modbus RTU client for Python, built on AnyIO and anyserial.

This module re-exports the package's public surface.
"""

from __future__ import annotations

from anymodbus._types import (
    ByteOrder,
    Capability,
    ExceptionCode,
    FunctionCode,
    WordOrder,
    is_idempotent_function,
    is_read_function,
    is_write_function,
)
from anymodbus._version import __version__
from anymodbus.bus import Bus
from anymodbus.capabilities import SlaveCapabilities
from anymodbus.config import BusConfig, RetryPolicy, TimingConfig
from anymodbus.exceptions import (
    AcknowledgeError,
    BusClosedError,
    ConfigurationError,
    ConnectionLostError,
    CRCError,
    FrameError,
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
    UnexpectedResponseError,
)
from anymodbus.slave import Slave
from anymodbus.stream import open_modbus_rtu

__all__ = [
    "AcknowledgeError",
    "Bus",
    "BusClosedError",
    "BusConfig",
    "ByteOrder",
    "CRCError",
    "Capability",
    "ConfigurationError",
    "ConnectionLostError",
    "ExceptionCode",
    "FrameError",
    "FrameTimeoutError",
    "FunctionCode",
    "GatewayPathUnavailableError",
    "GatewayTargetFailedToRespondError",
    "IllegalDataAddressError",
    "IllegalDataValueError",
    "IllegalFunctionError",
    "MemoryParityError",
    "ModbusError",
    "ModbusExceptionResponse",
    "ModbusUnknownExceptionError",
    "ModbusUnsupportedFunctionError",
    "ProtocolError",
    "RetryPolicy",
    "Slave",
    "SlaveCapabilities",
    "SlaveDeviceBusyError",
    "SlaveDeviceFailureError",
    "TimingConfig",
    "UnexpectedResponseError",
    "WordOrder",
    "__version__",
    "is_idempotent_function",
    "is_read_function",
    "is_write_function",
    "open_modbus_rtu",
]
