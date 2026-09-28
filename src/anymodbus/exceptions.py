"""Exception hierarchy and code mapping for ``anymodbus``.

Every exception class multi-inherits from the most natural standard-library
or AnyIO base so callers that already catch ``ValueError``, ``TimeoutError``,
``anyio.ClosedResourceError``, ``anyio.BrokenResourceError``, or
``anyio.BusyResourceError`` pick up our exceptions without new ``except``
clauses.

The :func:`code_to_exception` helper turns a Modbus exception code (1-11)
caught off the wire into the right domain exception. Mirrors the
:func:`anyserial.errno_to_exception` pattern.
"""

from __future__ import annotations

from typing import ClassVar

import anyio

from anymodbus._types import ExceptionCode, FunctionCode


class ModbusError(Exception):
    """Base class for every failure raised by ``anymodbus``."""


# ---------------------------------------------------------------------------
# Configuration errors — bad arguments to constructors, config dataclasses,
# codecs and request methods.
#
# Distinct from ProtocolError: nothing is on the wire yet. Inherits ValueError
# so existing ``except ValueError`` blocks still catch it.
# ---------------------------------------------------------------------------


class ConfigurationError(ModbusError, ValueError):
    """An invalid argument or configuration value.

    Raised by constructors and config dataclasses, by the PDU / ADU codecs and
    register decoders, and by request methods whose arguments are out of range
    (a register count above the spec maximum, a value that does not fit in a
    register, a read function code passed to a broadcast). It is always raised
    before anything is sent, so the bus and the slave are unaffected.
    """


# ---------------------------------------------------------------------------
# Wire / protocol errors — something is wrong with the bytes on the wire.
# ---------------------------------------------------------------------------


class ProtocolError(ModbusError, ValueError):
    """The codec or framer rejected something well-formed at the byte level.

    Bounds-check failures, unknown function codes, malformed PDU bodies.
    """


class ChecksumError(ProtocolError):
    """A frame was complete but its trailing checksum did not verify.

    Base class for the framing-specific checksum failures: :class:`CRCError`
    (RTU CRC-16) and :class:`LRCError` (ASCII LRC). Catch this to treat both
    framings' checksum failures uniformly; it remains a :class:`ProtocolError`,
    so existing ``except ProtocolError`` blocks are unaffected.
    """


class CRCError(ChecksumError):
    """Received RTU frame's CRC-16 did not match the computed CRC."""


class LRCError(ChecksumError):
    """Received ASCII frame's LRC did not match the computed LRC."""


class FrameError(ProtocolError):
    """ADU was truncated, contained junk between frames, or otherwise unparseable."""


class FrameTimeoutError(ModbusError, TimeoutError):
    """No response (or a partial response) arrived within the deadline."""


class UnexpectedResponseError(ProtocolError):
    """A checksum-valid reply that does not answer the request that was sent.

    The function code differs from the request's, a register or coil read
    returned a different quantity than was asked for, or a write's echo
    (address, value or quantity) differs from the request. The usual cause is
    a late reply to an earlier request; see
    :attr:`anymodbus.TimingConfig.late_reply_window`.

    On a write, the slave did answer, so the write may have been applied.
    """


class ModbusUnsupportedFunctionError(ModbusError, NotImplementedError):
    """A known Modbus function code that this client deliberately does not implement.

    Distinct from :class:`IllegalFunctionError` (which the slave raises): this
    is for the client declining to *send* a request whose function code
    ``anymodbus`` recognises but does not implement (e.g., the serial-line
    diagnostic FCs 0x07/0x0B/0x0C/0x11/0x18). No public method sends such a
    request, so nothing in ``anymodbus`` raises it today. A *reply* carrying
    one of these codes is not this error: it surfaces as :class:`CRCError`
    (line damage) or :class:`UnexpectedResponseError`. Inherits
    :class:`NotImplementedError` so generic ``except NotImplementedError``
    handlers still catch it.
    """


# ---------------------------------------------------------------------------
# Bus / transport errors — something is wrong with the connection.
# ---------------------------------------------------------------------------


class ConnectionLostError(ModbusError, anyio.BrokenResourceError):
    """The underlying stream disconnected mid-transaction."""


class TransportError(ConnectionLostError, OSError):
    """The stream raised an operating-system error during a transaction.

    Covers port failures that are not already a disconnect or a close: for
    example an :class:`anyserial.SerialError` for an errno or Windows error
    code that ``anyserial`` does not map to ``SerialDisconnectedError``, or a
    failing ``drain`` / ``reset_input_buffer``. The original exception is the
    ``__cause__``, and its ``errno`` is copied when it has one.

    It is a :class:`ConnectionLostError` (so an :class:`anyio.BrokenResourceError`)
    because the port is in an unknown state, and an :class:`OSError` so that
    ``except OSError`` handlers written for the raw error keep working.
    """


class BusClosedError(ModbusError, anyio.ClosedResourceError):
    """Operation attempted on a bus that has already been closed."""


# ---------------------------------------------------------------------------
# Modbus exception responses — the slave returned a function-code-with-high-
# -bit-set frame indicating a semantic refusal of the request.
#
# These inherit ``ModbusError`` (not ``ProtocolError``): the frame itself was
# perfectly well-formed; the slave just didn't like what we asked for.
# ---------------------------------------------------------------------------


class ModbusExceptionResponse(ModbusError):  # noqa: N818 — public API; "Response" is the spec term for FC|0x80 frames
    """Base class for slave-returned exception responses (FC | 0x80, code 1-11).

    The exception code is exposed as :attr:`exception_code` (instance attribute,
    set in ``__init__``). Subclasses pin a default via :attr:`default_code` so
    callers can construct them without specifying the code each time; passing
    an explicit ``exception_code`` overrides the default. Constructing the
    base :class:`ModbusExceptionResponse` directly is supported for codes
    outside the standard 1-11 range.
    """

    #: Per-subclass default code; ``None`` on the base class.
    default_code: ClassVar[int | None] = None

    def __init__(
        self,
        *,
        function_code: int,
        exception_code: int | None = None,
        message: str | None = None,
    ) -> None:
        if exception_code is None:
            if self.default_code is None:
                msg = (
                    "exception_code must be supplied when instantiating the base "
                    "ModbusExceptionResponse"
                )
                raise TypeError(msg)
            exception_code = self.default_code
        self.function_code: int = function_code
        self.exception_code: int = exception_code
        text = message or (
            f"slave returned exception {exception_code:#04x} for function code {function_code:#04x}"
        )
        super().__init__(text)


class IllegalFunctionError(ModbusExceptionResponse):
    """Exception code 0x01 — slave does not implement this function."""

    default_code = ExceptionCode.ILLEGAL_FUNCTION


class IllegalDataAddressError(ModbusExceptionResponse):
    """Exception code 0x02 — address (or address+count) outside slave's map."""

    default_code = ExceptionCode.ILLEGAL_DATA_ADDRESS


class IllegalDataValueError(ModbusExceptionResponse):
    """Exception code 0x03 — value in the data field is invalid for the slave."""

    default_code = ExceptionCode.ILLEGAL_DATA_VALUE


class SlaveDeviceFailureError(ModbusExceptionResponse):
    """Exception code 0x04 — unrecoverable error in the slave."""

    default_code = ExceptionCode.SLAVE_DEVICE_FAILURE


class AcknowledgeError(ModbusExceptionResponse):
    """Exception code 0x05 — slave accepted but needs more time. Poll again."""

    default_code = ExceptionCode.ACKNOWLEDGE


class SlaveDeviceBusyError(ModbusExceptionResponse):
    """Exception code 0x06 — slave is busy with another command. Retry later."""

    default_code = ExceptionCode.SLAVE_DEVICE_BUSY


class MemoryParityError(ModbusExceptionResponse):
    """Exception code 0x08 — slave detected a memory parity error during read."""

    default_code = ExceptionCode.MEMORY_PARITY_ERROR


class GatewayPathUnavailableError(ModbusExceptionResponse):
    """Exception code 0x0A — gateway could not allocate an internal path."""

    default_code = ExceptionCode.GATEWAY_PATH_UNAVAILABLE


class GatewayTargetFailedToRespondError(ModbusExceptionResponse):
    """Exception code 0x0B — target on the far side of the gateway didn't respond."""

    default_code = ExceptionCode.GATEWAY_TARGET_FAILED_TO_RESPOND


class ModbusUnknownExceptionError(ModbusExceptionResponse):
    """Slave returned an exception code not assigned by *app §7*.

    The slave returned a well-formed exception ADU; we just don't have a named
    subclass for the code it chose. The raw byte is preserved on
    :attr:`exception_code` (inherited from :class:`ModbusExceptionResponse`),
    so callers wanting "any slave-returned exception" can simply
    ``except ModbusExceptionResponse`` and branch on
    :attr:`exception_code` if needed.

    The notable case is **0x07 (Negative Acknowledge)**: pre-v1.1 Modicon
    controllers used 0x07, but v1.1b3 §7 does not list it (the NAK semantic
    was repositioned as a Diagnostics counter, FC 0x08 sub 0x10). Anything a
    legacy device emits as 0x07 surfaces here with
    ``exception_code == 0x07``; downstream device libraries that target old
    Modicon hardware can subclass or branch on the code as needed.
    """

    # No ``default_code``: this class is only ever instantiated with an
    # explicit ``exception_code`` from the wire.
    default_code = None


_EXCEPTION_CODE_TO_CLASS: dict[int, type[ModbusExceptionResponse]] = {
    ExceptionCode.ILLEGAL_FUNCTION: IllegalFunctionError,
    ExceptionCode.ILLEGAL_DATA_ADDRESS: IllegalDataAddressError,
    ExceptionCode.ILLEGAL_DATA_VALUE: IllegalDataValueError,
    ExceptionCode.SLAVE_DEVICE_FAILURE: SlaveDeviceFailureError,
    ExceptionCode.ACKNOWLEDGE: AcknowledgeError,
    ExceptionCode.SLAVE_DEVICE_BUSY: SlaveDeviceBusyError,
    ExceptionCode.MEMORY_PARITY_ERROR: MemoryParityError,
    ExceptionCode.GATEWAY_PATH_UNAVAILABLE: GatewayPathUnavailableError,
    ExceptionCode.GATEWAY_TARGET_FAILED_TO_RESPOND: GatewayTargetFailedToRespondError,
}


def code_to_exception(
    *,
    function_code: int,
    exception_code: int,
    message: str | None = None,
) -> ModbusExceptionResponse:
    """Build the right exception class for a Modbus exception-response code.

    Codes assigned by *app §7* (1-6, 8, 10, 11) dispatch to the matching
    :class:`ModbusExceptionResponse` subclass. Anything else — including
    legacy 0x07 (Negative Acknowledge) and the unassigned 0x09 / 0x0C-0xFF
    range — surfaces as :class:`ModbusUnknownExceptionError`, which is also
    a :class:`ModbusExceptionResponse`, with the raw byte preserved on
    :attr:`ModbusExceptionResponse.exception_code`.

    Args:
        function_code: The function-code byte from the response (the high
            bit is already stripped by the framer; pass the original
            request's FC).
        exception_code: The exception-code byte from the response body.
        message: Optional override for the exception's message.

    Returns:
        An exception instance ready to ``raise``.
    """
    cls = _EXCEPTION_CODE_TO_CLASS.get(exception_code, ModbusUnknownExceptionError)
    return cls(
        function_code=function_code,
        exception_code=exception_code,
        message=message,
    )


def is_exception_response(function_code_byte: int) -> bool:
    """Return True if the high bit of ``function_code_byte`` is set."""
    return bool(function_code_byte & 0x80)


def strip_exception_bit(function_code_byte: int) -> FunctionCode:
    """Return the underlying :class:`FunctionCode` from an exception response byte."""
    return FunctionCode(function_code_byte & 0x7F)


__all__ = [
    "AcknowledgeError",
    "BusClosedError",
    "CRCError",
    "ChecksumError",
    "ConfigurationError",
    "ConnectionLostError",
    "FrameError",
    "FrameTimeoutError",
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
    "SlaveDeviceBusyError",
    "SlaveDeviceFailureError",
    "TransportError",
    "UnexpectedResponseError",
    "code_to_exception",
    "is_exception_response",
    "strip_exception_bit",
]
