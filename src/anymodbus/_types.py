"""Core enums and small data types used across the public API.

These have no runtime dependencies on any other ``anymodbus`` module and are
safe to import from anywhere in the package.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class FunctionCode(IntEnum):
    """Modbus function codes implemented (or planned) by anymodbus.

    Values match the wire encoding. Aliased as ``FC`` at the top-level package
    surface.
    """

    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_COILS = 0x0F
    WRITE_MULTIPLE_REGISTERS = 0x10
    MASK_WRITE_REGISTER = 0x16
    READ_WRITE_MULTIPLE_REGISTERS = 0x17
    ENCAPSULATED_INTERFACE_TRANSPORT = 0x2B


_READ_FUNCTION_CODES = frozenset(
    {
        FunctionCode.READ_COILS,
        FunctionCode.READ_DISCRETE_INPUTS,
        FunctionCode.READ_HOLDING_REGISTERS,
        FunctionCode.READ_INPUT_REGISTERS,
    }
)
_WRITE_FUNCTION_CODES = frozenset(
    {
        FunctionCode.WRITE_SINGLE_COIL,
        FunctionCode.WRITE_SINGLE_REGISTER,
        FunctionCode.WRITE_MULTIPLE_COILS,
        FunctionCode.WRITE_MULTIPLE_REGISTERS,
        FunctionCode.MASK_WRITE_REGISTER,
    }
)
# Function codes safe to retry by default on transient transport errors. The
# concept is "no observable side effect on the slave if we re-fire": pure
# reads only. FC 23 is excluded because it has a write half; FC 22 is
# excluded because it depends on the slave's current value.
_IDEMPOTENT_FUNCTION_CODES = _READ_FUNCTION_CODES


def is_read_function(fc: FunctionCode | int) -> bool:
    """Return True if ``fc`` is one of the read-only function codes (1-4)."""
    try:
        code = FunctionCode(fc)
    except ValueError:
        return False
    return code in _READ_FUNCTION_CODES


def is_write_function(fc: FunctionCode | int) -> bool:
    """Return True if ``fc`` is one of the write function codes."""
    try:
        code = FunctionCode(fc)
    except ValueError:
        return False
    return code in _WRITE_FUNCTION_CODES


def is_idempotent_function(fc: FunctionCode | int) -> bool:
    """Return True if ``fc`` is safe to silently retry on transport error.

    Used by :class:`RetryPolicy` when ``retry_idempotent_only=True``: only
    function codes that have no side effect on the slave qualify, so a lost
    response can be re-driven without risk of double-firing a write. Today
    that's exactly FC 1-4. FC 23 (Read/Write Multiple) is excluded because
    it has a write half; FC 22 (Mask Write) is excluded because the result
    depends on the slave's current value.
    """
    try:
        code = FunctionCode(fc)
    except ValueError:
        return False
    return code in _IDEMPOTENT_FUNCTION_CODES


class ExceptionCode(IntEnum):
    """Modbus exception response codes (the body of an exception PDU).

    Mirrors the table in *Modbus Application Protocol v1.1b3 §7*. Code 0x07
    (Negative Acknowledge in pre-v1.1 Modicon controllers) is intentionally
    omitted: v1.1b3 does not assign it, and the framer surfaces any code
    outside this enum as :class:`ModbusUnknownExceptionError`.
    """

    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_DATA_ADDRESS = 0x02
    ILLEGAL_DATA_VALUE = 0x03
    SLAVE_DEVICE_FAILURE = 0x04
    ACKNOWLEDGE = 0x05
    SLAVE_DEVICE_BUSY = 0x06
    MEMORY_PARITY_ERROR = 0x08
    GATEWAY_PATH_UNAVAILABLE = 0x0A
    GATEWAY_TARGET_FAILED_TO_RESPOND = 0x0B


class WordOrder(StrEnum):
    """Word ordering for 32-bit values spread across two 16-bit registers.

    - ``HIGH_LOW``: most-significant word first. Equivalent to
      ``struct.pack(">f", ...)``.
    - ``LOW_HIGH``: least-significant word first. Common on certain
      industrial controllers; always check the device's protocol manual.

    There is no portable default — Modbus does not standardize multi-register
    word ordering (the spec defines big-endian byte order *within* a single
    16-bit register only, in App Protocol §4.2). Function signatures in
    :mod:`anymodbus.decoders` and on :class:`Slave` default to
    :attr:`HIGH_LOW` because it's the most common convention and matches
    ``struct.pack(">f", ...)``, but downstream device libraries should pass
    the value explicitly.
    """

    HIGH_LOW = "high_low"
    LOW_HIGH = "low_high"


class ByteOrder(StrEnum):
    """Byte ordering within each 16-bit register word.

    Big-endian within a word is the Modbus norm (App Protocol §4.2);
    little-endian is rare but appears on some devices.
    """

    BIG = "big"
    LITTLE = "little"


class Capability(StrEnum):
    """Tri-state describing whether a feature is available.

    Mirrors :class:`anyserial.Capability` so users who already know the
    anyserial vocabulary need not learn a second one.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


__all__ = [
    "ByteOrder",
    "Capability",
    "ExceptionCode",
    "FunctionCode",
    "WordOrder",
    "is_idempotent_function",
    "is_read_function",
    "is_write_function",
]
