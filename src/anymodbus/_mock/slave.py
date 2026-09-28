"""Mock Modbus slave for tests — a register bank that speaks the wire format.

:class:`MockSlave` is a pure-Python slave that runs alongside a :class:`Bus`
in tests, decoding requests and producing responses against four mutable
register banks (coils, discrete inputs, holding registers, input registers).

It deliberately mirrors the on-wire framing the real :mod:`anymodbus.framer`
expects, so integration tests exercise the same length-aware reader, CRC
verification, and timing behaviour they would against real hardware.

:meth:`MockSlave.handle` answers one request PDU and is the extension point
for simulating a particular device; raise :class:`ServerException` from it to
send an exception response. :class:`QuantityLimits` caps request sizes the way
a real device does. :class:`FaultPlan` lets a test script transient failures
(CRC corruption, response delay, wrong slave address, dropped responses)
without having to write a custom mock for each scenario. To put several mock
slaves on one line, serve them together with :class:`MockServer`.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

import anyio
import anyio.abc

from anymodbus._mock.faults import FaultPlan
from anymodbus._types import ExceptionCode, Framing, FunctionCode
from anymodbus.crc import crc16_modbus_bytes
from anymodbus.exceptions import ConfigurationError
from anymodbus.framer_ascii import encode_ascii_adu
from anymodbus.lrc import lrc8_bytes
from anymodbus.pdu import (
    ReadRequest,
    decode_diagnostic_loopback_request,
    decode_read_coils_request,
    decode_read_discrete_inputs_request,
    decode_read_holding_registers_request,
    decode_read_input_registers_request,
    decode_write_multiple_coils_request,
    decode_write_multiple_registers_request,
    decode_write_single_coil_request,
    decode_write_single_register_request,
    encode_diagnostic_loopback_response,
    encode_read_coils_response,
    encode_read_discrete_inputs_response,
    encode_read_holding_registers_response,
    encode_read_input_registers_response,
    encode_write_multiple_coils_response,
    encode_write_multiple_registers_response,
    encode_write_single_coil_response,
    encode_write_single_register_response,
)

_LOGGER = logging.getLogger("anymodbus.mock")

_MIN_SLAVE_ADDRESS = 1
_MAX_SLAVE_ADDRESS = 247  # *Modbus over Serial Line v1.02 §2.2*

# Spec quantity bounds — see *app §6.x*. These mirror the client-side bounds
# in :mod:`anymodbus.pdu`; we duplicate the constants rather than importing
# from a private module to keep the mock self-contained.
_MAX_READ_BITS = 2000
_MAX_READ_REGISTERS = 125
_MAX_WRITE_COILS = 1968
_MAX_WRITE_REGISTERS = 123

# FC 0x08 Diagnostics: only sub-function 0x0000 (Return Query Data) is modelled.
_DIAG_SUBFN_RETURN_QUERY_DATA = 0x0000
_DIAG_REQUEST_PDU_LEN = 5  # fc(1) + subfn(2) + data(2)


class ServerException(Exception):  # noqa: N818 — named for the spec's "exception response"
    """Raise from :meth:`MockSlave.handle` to answer with a Modbus exception response.

    The serving loop turns it into ``function_code | 0x80`` followed by
    :attr:`code`, the way a real device refuses a request.

    Args:
        code: The exception code to send, usually an :class:`ExceptionCode`
            such as :attr:`ExceptionCode.ILLEGAL_DATA_ADDRESS`.
    """

    def __init__(self, code: ExceptionCode | int) -> None:
        super().__init__(int(code))
        self.code = code


# Also importable under this name, for subclasses that raise it by it.
_ServerException = ServerException


@dataclass(frozen=True, slots=True, kw_only=True)
class QuantityLimits:
    """The largest quantity a :class:`MockSlave` accepts in one request, per kind.

    Defaults are the spec maxima. A lower value simulates a device with a
    smaller cap (e.g. ``QuantityLimits(read_registers=64)``); a request above
    it is answered with :attr:`ExceptionCode.ILLEGAL_DATA_VALUE`, as the spec
    requires for a quantity out of range.

    Attributes:
        read_bits: FC 0x01 / 0x02, at most 2000.
        read_registers: FC 0x03 / 0x04, at most 125.
        write_coils: FC 0x0F, at most 1968.
        write_registers: FC 0x10, at most 123.
    """

    read_bits: int = _MAX_READ_BITS
    read_registers: int = _MAX_READ_REGISTERS
    write_coils: int = _MAX_WRITE_COILS
    write_registers: int = _MAX_WRITE_REGISTERS

    def __post_init__(self) -> None:
        """Validate each limit against its spec maximum."""
        for name, value, spec_max in (
            ("read_bits", self.read_bits, _MAX_READ_BITS),
            ("read_registers", self.read_registers, _MAX_READ_REGISTERS),
            ("write_coils", self.write_coils, _MAX_WRITE_COILS),
            ("write_registers", self.write_registers, _MAX_WRITE_REGISTERS),
        ):
            if not (1 <= value <= spec_max):
                msg = f"{name} must be in [1, {spec_max}] (got {value!r})"
                raise ConfigurationError(msg)


class MockSlave:
    """Pure-Python Modbus slave for integration tests.

    The four register banks (coils, discrete inputs, holding registers,
    input registers) are exposed as mutable sequences so tests can preload
    state, observe writes, and inject failure modes via :class:`FaultPlan`.

    Address validation matches *Modbus over Serial Line v1.02 §2.2*:
    addresses 1-247 are unicast, 0 is broadcast (the slave still applies
    write requests but does not respond), and 248-255 are reserved.

    To simulate a particular device, subclass and override :meth:`handle`,
    calling ``super().handle(request_pdu)`` for the requests the register
    banks should answer.
    """

    address: int
    coils: bytearray
    discrete_inputs: bytearray
    holding_registers: list[int]
    input_registers: list[int]
    faults: FaultPlan
    disabled_function_codes: frozenset[int]
    framing: Framing
    limits: QuantityLimits

    def __init__(
        self,
        *,
        address: int = 1,
        register_count: int = 256,
        coil_count: int = 256,
        discrete_input_count: int | None = None,
        input_register_count: int | None = None,
        faults: FaultPlan | None = None,
        disabled_function_codes: frozenset[int] | None = None,
        framing: Framing = Framing.RTU,
        limits: QuantityLimits | None = None,
    ) -> None:
        """Construct a mock slave.

        Args:
            address: Modbus unit address. Must be 1-247.
            register_count: Size of the holding register bank, and the
                default size of the input register bank when
                ``input_register_count`` is not supplied.
            coil_count: Size of the coils bit bank, and the default size of
                the discrete-inputs bank when ``discrete_input_count`` is
                not supplied.
            discrete_input_count: Optional independent size for the
                discrete-inputs bank. Defaults to ``coil_count``.
            input_register_count: Optional independent size for the input
                register bank. Defaults to ``register_count``.
            faults: Optional :class:`FaultPlan` for transient failure modes.
            disabled_function_codes: FCs to refuse with
                :attr:`ExceptionCode.ILLEGAL_FUNCTION` even though the mock
                otherwise implements them. Used by capability-probe tests to
                simulate a slave that lacks specific function codes.
            framing: Wire framing the slave reads and emits — :attr:`Framing.RTU`
                (binary + CRC) or :attr:`Framing.ASCII` (``:``..LRC..CRLF). The
                same register banks back either framing.
            limits: Per-request quantity caps. Defaults to the spec maxima.
        """
        if not (_MIN_SLAVE_ADDRESS <= address <= _MAX_SLAVE_ADDRESS):
            msg = (
                f"MockSlave address must be 1-247 (got {address!r}); "
                f"address 0 is broadcast and 248-255 are reserved"
            )
            raise ConfigurationError(msg)
        if discrete_input_count is None:
            discrete_input_count = coil_count
        if input_register_count is None:
            input_register_count = register_count
        self.address = address
        self.coils = bytearray((coil_count + 7) // 8)
        self.discrete_inputs = bytearray((discrete_input_count + 7) // 8)
        self.holding_registers = [0] * register_count
        self.input_registers = [0] * input_register_count
        self.faults = faults if faults is not None else FaultPlan()
        self.disabled_function_codes = (
            disabled_function_codes if disabled_function_codes is not None else frozenset()
        )
        self.framing = framing
        self.limits = limits if limits is not None else QuantityLimits()
        self._coil_count = coil_count
        self._discrete_input_count = discrete_input_count
        # Index of the next response we will emit. Used for FaultPlan's
        # corrupt_crc_after_n / drop_response_after_n one-shot triggers.
        self._responses_emitted = 0

    @property
    def response_count(self) -> int:
        """Responses this slave has produced so far, including dropped ones.

        The 0-based index of the next response is this value; it is the index
        :class:`FaultPlan`'s ``*_after_n`` fields refer to.
        """
        return self._responses_emitted

    async def serve(self, stream: anyio.abc.ByteStream) -> None:
        """Accept requests on ``stream``, write responses, until cancelled.

        Serves this slave alone, as ``MockServer(self, framing=self.framing)``
        would. Bad checksums (CRC for RTU, LRC for ASCII) and unreadable
        frames are logged and dropped — there is no in-band recovery
        mechanism on a real Modbus bus, so the mock mirrors that behaviour.
        Returns when the stream closes.
        """
        from anymodbus._mock.server import MockServer  # noqa: PLC0415 — server imports this module

        await MockServer(self, framing=self.framing).serve(stream)

    def handle(self, request_pdu: bytes) -> bytes:
        """Answer one request PDU (FC byte + body) with a response PDU.

        The extension point for simulating a device: override it, handle the
        requests you want to customise, and call ``super().handle(request_pdu)``
        for the rest. Raise :class:`ServerException` to answer with a Modbus
        exception response; a :class:`anymodbus.ProtocolError` from a request
        decoder (a malformed request, or a quantity outside the spec range) is
        answered with :attr:`ExceptionCode.ILLEGAL_DATA_VALUE`.

        The default answers FC 0x01-0x06, 0x0F, 0x10 and 0x08 sub-function 0
        from the register banks, within :attr:`limits`, refuses
        :attr:`disabled_function_codes` and any other FC with
        :attr:`ExceptionCode.ILLEGAL_FUNCTION`, and applies broadcast writes
        the same way (the server sends no reply to a broadcast).
        """
        return self._handle_request(request_pdu)

    async def send_response(self, stream: anyio.abc.ByteStream, response_pdu: bytes) -> None:
        """Send ``response_pdu`` on ``stream`` as this slave, applying :attr:`faults`.

        Frames it for :attr:`framing` with this slave's address. The
        :class:`FaultPlan` may drop it, delay it, corrupt its checksum, or send
        it from another address. Every call counts towards
        :attr:`response_count`, dropped or not.
        """
        idx = self._responses_emitted
        self._responses_emitted += 1
        plan = self.faults
        if plan.drop_response_after_n is not None and idx == plan.drop_response_after_n:
            _LOGGER.info("MockSlave: dropping response %d per FaultPlan", idx)
            return
        if plan.delay_response_seconds > 0:
            await anyio.sleep(plan.delay_response_seconds)
        slave_byte = (
            plan.wrong_slave_address if plan.wrong_slave_address is not None else self.address
        )
        corrupt = plan.corrupt_crc_after_n is not None and idx == plan.corrupt_crc_after_n
        if corrupt:
            _LOGGER.info("MockSlave: corrupting the checksum of response %d per FaultPlan", idx)
        body = bytes((slave_byte,)) + response_pdu
        if self.framing is Framing.ASCII:
            if corrupt:
                # Flip a bit of the binary LRC before hex-encoding, so the
                # client sees LRCError rather than a malformed frame.
                frame = bytearray(body + lrc8_bytes(body))
                frame[-1] ^= 0x01
                await stream.send(b":" + frame.hex().upper().encode("ascii") + b"\r\n")
                return
            await stream.send(encode_ascii_adu(slave_address=slave_byte, pdu=response_pdu))
            return
        crc = crc16_modbus_bytes(body)
        if corrupt:
            crc = bytes((crc[0] ^ 0x01, crc[1]))
        await stream.send(body + crc)

    # ------------------------------------------------------------------
    # Per-FC handlers. ``handle`` delegates here; subclasses may override
    # either. Each takes the request PDU (FC byte + body) and returns the
    # response PDU, or raises ServerException / ProtocolError.
    # ------------------------------------------------------------------

    def _handle_request(self, pdu: bytes) -> bytes:  # noqa: PLR0911 — one return per FC
        fc = pdu[0]
        if fc in self.disabled_function_codes:
            raise ServerException(ExceptionCode.ILLEGAL_FUNCTION)
        if fc == FunctionCode.READ_COILS:
            bits = self._read_bits(decode_read_coils_request(pdu), self.coils, self._coil_count)
            return encode_read_coils_response(bits)
        if fc == FunctionCode.READ_DISCRETE_INPUTS:
            bits = self._read_bits(
                decode_read_discrete_inputs_request(pdu),
                self.discrete_inputs,
                self._discrete_input_count,
            )
            return encode_read_discrete_inputs_response(bits)
        if fc == FunctionCode.READ_HOLDING_REGISTERS:
            words = self._read_registers(
                decode_read_holding_registers_request(pdu), self.holding_registers
            )
            return encode_read_holding_registers_response(words)
        if fc == FunctionCode.READ_INPUT_REGISTERS:
            words = self._read_registers(
                decode_read_input_registers_request(pdu), self.input_registers
            )
            return encode_read_input_registers_response(words)
        if fc == FunctionCode.WRITE_SINGLE_COIL:
            return self._handle_write_single_coil(pdu)
        if fc == FunctionCode.WRITE_SINGLE_REGISTER:
            return self._handle_write_single_register(pdu)
        if fc == FunctionCode.WRITE_MULTIPLE_COILS:
            return self._handle_write_multiple_coils(pdu)
        if fc == FunctionCode.WRITE_MULTIPLE_REGISTERS:
            return self._handle_write_multiple_registers(pdu)
        if fc == FunctionCode.DIAGNOSTICS:
            return self._handle_diagnostic_loopback(pdu)
        raise ServerException(ExceptionCode.ILLEGAL_FUNCTION)

    def _handle_diagnostic_loopback(self, pdu: bytes) -> bytes:
        # FC 0x08 sub 0x0000 (Return Query Data): echo fc + subfn + data word.
        if len(pdu) == _DIAG_REQUEST_PDU_LEN:
            (subfn,) = struct.unpack(">H", pdu[1:3])
            if subfn != _DIAG_SUBFN_RETURN_QUERY_DATA:
                # We model sub-0 only; reject other sub-functions as the spec allows.
                raise ServerException(ExceptionCode.ILLEGAL_FUNCTION)
        return encode_diagnostic_loopback_response(decode_diagnostic_loopback_request(pdu))

    def _read_bits(self, request: ReadRequest, bank: bytearray, bank_size: int) -> list[bool]:
        if request.count > self.limits.read_bits:
            raise ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if request.address + request.count > bank_size:
            raise ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        return [
            bool(bank[src >> 3] & (1 << (src & 7)))
            for src in range(request.address, request.address + request.count)
        ]

    def _read_registers(self, request: ReadRequest, bank: list[int]) -> list[int]:
        if request.count > self.limits.read_registers:
            raise ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if request.address + request.count > len(bank):
            raise ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        return bank[request.address : request.address + request.count]

    def _set_coil(self, address: int, *, on: bool) -> None:
        if on:
            self.coils[address >> 3] |= 1 << (address & 7)
        else:
            self.coils[address >> 3] &= (~(1 << (address & 7))) & 0xFF

    def _handle_write_single_coil(self, pdu: bytes) -> bytes:
        request = decode_write_single_coil_request(pdu)
        if request.address >= self._coil_count:
            raise ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        self._set_coil(request.address, on=request.on)
        return encode_write_single_coil_response(request.address, on=request.on)

    def _handle_write_single_register(self, pdu: bytes) -> bytes:
        request = decode_write_single_register_request(pdu)
        if request.address >= len(self.holding_registers):
            raise ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        self.holding_registers[request.address] = request.value
        return encode_write_single_register_response(request.address, request.value)

    def _handle_write_multiple_coils(self, pdu: bytes) -> bytes:
        request = decode_write_multiple_coils_request(pdu)
        count = len(request.values)
        if count > self.limits.write_coils:
            raise ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if request.address + count > self._coil_count:
            raise ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        for i, on in enumerate(request.values):
            self._set_coil(request.address + i, on=on)
        return encode_write_multiple_coils_response(request.address, count)

    def _handle_write_multiple_registers(self, pdu: bytes) -> bytes:
        request = decode_write_multiple_registers_request(pdu)
        count = len(request.values)
        if count > self.limits.write_registers:
            raise ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if request.address + count > len(self.holding_registers):
            raise ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        self.holding_registers[request.address : request.address + count] = request.values
        return encode_write_multiple_registers_response(request.address, count)


__all__ = ["MockSlave", "QuantityLimits", "ServerException"]
