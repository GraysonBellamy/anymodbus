"""Async-native Modbus RTU client for Python, built on AnyIO and anyserial.

This module re-exports the package's public surface.
"""

from __future__ import annotations

from anymodbus._types import (
    ByteOrder,
    Capability,
    ExceptionCode,
    Framing,
    FunctionCode,
    RegisterSource,
    RegisterType,
    WordOrder,
    is_idempotent_function,
    is_read_function,
    is_write_function,
)
from anymodbus._version import __version__
from anymodbus.bus import Bus
from anymodbus.capabilities import SlaveCapabilities
from anymodbus.config import (
    BusConfig,
    RetryPolicy,
    TimingConfig,
    estimate_late_reply_window,
)
from anymodbus.decoders import decode, encode, register_count_for
from anymodbus.exceptions import (
    AcknowledgeError,
    BusClosedError,
    ChecksumError,
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
    LRCError,
    MemoryParityError,
    ModbusError,
    ModbusExceptionResponse,
    ModbusUnknownExceptionError,
    ModbusUnsupportedFunctionError,
    ProtocolError,
    SlaveDeviceBusyError,
    SlaveDeviceFailureError,
    TransportError,
    UnexpectedResponseError,
)
from anymodbus.slave import Slave
from anymodbus.stream import open_modbus_ascii, open_modbus_rtu
from anymodbus.transaction import TransactionInfo, TransactionObserver, TransactionOutcome

#: Short alias for :class:`FunctionCode`. The class docstring promised it; we
#: deliver. Use ``anymodbus.FC.READ_HOLDING_REGISTERS`` if the long name is
#: noisy at the call site.
FC = FunctionCode

__all__ = [
    "FC",
    "AcknowledgeError",
    "Bus",
    "BusClosedError",
    "BusConfig",
    "ByteOrder",
    "CRCError",
    "Capability",
    "ChecksumError",
    "ConfigurationError",
    "ConnectionLostError",
    "ExceptionCode",
    "FrameError",
    "FrameTimeoutError",
    "Framing",
    "FunctionCode",
    "GatewayPathUnavailableError",
    "GatewayTargetFailedToRespondError",
    "IllegalDataAddressError",
    "IllegalDataValueError",
    "IllegalFunctionError",
    "LRCError",
    "MemoryParityError",
    "ModbusError",
    "ModbusExceptionResponse",
    "ModbusUnknownExceptionError",
    "ModbusUnsupportedFunctionError",
    "ProtocolError",
    "RegisterSource",
    "RegisterType",
    "RetryPolicy",
    "Slave",
    "SlaveCapabilities",
    "SlaveDeviceBusyError",
    "SlaveDeviceFailureError",
    "TimingConfig",
    "TransactionInfo",
    "TransactionObserver",
    "TransactionOutcome",
    "TransportError",
    "UnexpectedResponseError",
    "WordOrder",
    "__version__",
    "decode",
    "encode",
    "estimate_late_reply_window",
    "is_idempotent_function",
    "is_read_function",
    "is_write_function",
    "open_modbus_ascii",
    "open_modbus_rtu",
    "register_count_for",
]
