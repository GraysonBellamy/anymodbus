"""Mock Modbus slave for tests — a register bank that speaks the wire format.

:class:`MockSlave` is a pure-Python server that runs alongside a :class:`Bus`
in tests, decoding requests and producing responses against four mutable
register banks (coils, discrete inputs, holding registers, input registers).

It deliberately mirrors the on-wire framing the real :mod:`anymodbus.framer`
expects, so integration tests exercise the same length-aware reader, CRC
verification, and timing behaviour they would against real hardware.

:class:`FaultPlan` lets a test script transient failures (CRC corruption,
response delay, wrong slave address, dropped responses) without having to
write a custom mock for each scenario.
"""

from __future__ import annotations

import logging
import struct
from typing import TYPE_CHECKING, Final

import anyio
import anyio.abc

from anymodbus._mock.faults import FaultPlan
from anymodbus._types import ExceptionCode, Framing, FunctionCode
from anymodbus.crc import crc16_modbus_bytes, verify_crc
from anymodbus.exceptions import FrameError
from anymodbus.framer_ascii import encode_ascii_adu, read_ascii_frame
from anymodbus.lrc import lrc8_bytes, verify_lrc

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger("anymodbus.mock")

_BROADCAST_ADDRESS = 0
_MIN_SLAVE_ADDRESS = 1
_MAX_SLAVE_ADDRESS = 247  # *Modbus over Serial Line v1.02 §2.2*

# Wire encodings for FC 0x05 Write Single Coil (*app §6.5*).
_COIL_ON = 0xFF00
_COIL_OFF = 0x0000

# Spec quantity bounds — see *app §6.x*. These mirror the client-side bounds
# in :mod:`anymodbus.pdu`; we duplicate the constants rather than importing
# from a private module to keep the mock self-contained.
_MAX_READ_BITS = 2000
_MAX_READ_REGISTERS = 125
_MAX_WRITE_COILS = 1968
_MAX_WRITE_REGISTERS = 123

# Bytes after the 2-byte (slave + fc) request header for fixed-length request
# bodies, INCLUDING the 2-byte trailing CRC. FC 0x0F / 0x10 carry a 1-byte
# byte_count after this fixed prefix and are handled separately.
_FIXED_REQUEST_TAIL: Final[Mapping[int, int]] = {
    FunctionCode.READ_COILS: 6,  # addr(2) + count(2) + crc(2)
    FunctionCode.READ_DISCRETE_INPUTS: 6,
    FunctionCode.READ_HOLDING_REGISTERS: 6,
    FunctionCode.READ_INPUT_REGISTERS: 6,
    FunctionCode.WRITE_SINGLE_COIL: 6,  # addr(2) + value(2) + crc(2)
    FunctionCode.WRITE_SINGLE_REGISTER: 6,
    FunctionCode.DIAGNOSTICS: 6,  # subfunction(2) + data(2) + crc(2) — sub-0 only
}

_VARIABLE_REQUEST_FCS: Final[frozenset[int]] = frozenset(
    {FunctionCode.WRITE_MULTIPLE_COILS, FunctionCode.WRITE_MULTIPLE_REGISTERS}
)

_CRC_LEN = 2

# Length of the FC 0x0F / 0x10 request prefix on the wire (FC byte + address(2) +
# count(2) + byte_count(1)). The variable-length data payload follows.
_WRITE_MULTIPLE_REQUEST_PREFIX_LEN = 6

# FC 0x08 Diagnostics: only sub-function 0x0000 (Return Query Data) is modelled.
_DIAG_SUBFN_RETURN_QUERY_DATA = 0x0000
_DIAG_REQUEST_PDU_LEN = 5  # fc(1) + subfn(2) + data(2)


class _ServerException(Exception):  # noqa: N818 — internal sentinel, not user-visible
    """Internal: signals that a handler wants the loop to emit an exception PDU."""

    def __init__(self, code: ExceptionCode) -> None:
        super().__init__(int(code))
        self.code = code


class MockSlave:
    """Pure-Python Modbus slave for integration tests.

    The four register banks (coils, discrete inputs, holding registers,
    input registers) are exposed as mutable sequences so tests can preload
    state, observe writes, and inject failure modes via :class:`FaultPlan`.

    Address validation matches *Modbus over Serial Line v1.02 §2.2*:
    addresses 1-247 are unicast, 0 is broadcast (the slave still applies
    write requests but does not respond), and 248-255 are reserved.
    """

    address: int
    coils: bytearray
    discrete_inputs: bytearray
    holding_registers: list[int]
    input_registers: list[int]
    faults: FaultPlan
    disabled_function_codes: frozenset[int]
    framing: Framing

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
        """
        if not (_MIN_SLAVE_ADDRESS <= address <= _MAX_SLAVE_ADDRESS):
            msg = (
                f"MockSlave address must be 1-247 (got {address!r}); "
                f"address 0 is broadcast and 248-255 are reserved"
            )
            raise ValueError(msg)
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
        self._coil_count = coil_count
        self._discrete_input_count = discrete_input_count
        self._register_count = register_count
        self._input_register_count = input_register_count
        # Index of the next response we will emit. Used for FaultPlan's
        # corrupt_crc_after_n / drop_response_after_n one-shot triggers.
        self._responses_emitted = 0

    async def serve(self, stream: anyio.abc.ByteStream) -> None:
        """Accept requests on ``stream``, write responses, until cancelled.

        Loops forever (or until the stream closes / the task is cancelled)
        reading one request per iteration. Bad checksums (CRC for RTU, LRC for
        ASCII) are logged and dropped — there is no in-band recovery mechanism
        on a real Modbus bus, so the mock mirrors that behaviour. The framing
        (:attr:`framing`) selects the RTU or ASCII wire reader.
        """
        serve_one = self._serve_one_ascii if self.framing is Framing.ASCII else self._serve_one_rtu
        while True:
            try:
                continue_loop = await serve_one(stream)
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                return
            if not continue_loop:
                return

    async def _serve_one_rtu(self, stream: anyio.abc.ByteStream) -> bool:
        head = await self._read_exact(stream, 2)
        addr = head[0]
        fc = head[1]

        if fc in _FIXED_REQUEST_TAIL:
            tail = await self._read_exact(stream, _FIXED_REQUEST_TAIL[fc])
        elif fc in _VARIABLE_REQUEST_FCS:
            # addr(2) + count(2) + byte_count(1) + data(byte_count) + crc(2)
            prefix = await self._read_exact(stream, 5)
            byte_count = prefix[4]
            rest = await self._read_exact(stream, byte_count + _CRC_LEN)
            tail = prefix + rest
        else:
            # Unknown FC. We don't have length info to drain the body
            # safely; the safest action is to log and stop serving so the
            # test fails loudly rather than silently corrupting later frames.
            _LOGGER.warning("MockSlave: unsupported FC 0x%02x; closing serve loop", fc)
            return False

        full_request = head + tail
        if not verify_crc(full_request):
            _LOGGER.warning("MockSlave: CRC mismatch on request, dropping")
            return True

        request_pdu = full_request[1:-_CRC_LEN]  # strip slave addr + CRC
        response_pdu = self._dispatch(addr, request_pdu)
        if response_pdu is not None:
            await self._send_response_rtu(stream, response_pdu)
        return True

    async def _serve_one_ascii(self, stream: anyio.abc.ByteStream) -> bool:
        try:
            raw = await read_ascii_frame(stream)
        except FrameError:
            # Malformed frame we can't even de-hex. A clean close between frames
            # surfaces as EndOfStream (handled in serve()), not FrameError, so
            # this branch is genuinely a corrupt request: drop and keep serving.
            _LOGGER.warning("MockSlave: malformed ASCII request frame, dropping")
            return True
        if not verify_lrc(raw):
            # Mirror the RTU bad-CRC drop (decision D2) — caller branches on the
            # bool, no exception to crash the serve task.
            _LOGGER.warning("MockSlave: LRC mismatch on request, dropping")
            return True

        addr, request_pdu = raw[0], raw[1:-1]  # strip slave addr + LRC
        response_pdu = self._dispatch(addr, request_pdu)
        if response_pdu is not None:
            await self._send_response_ascii(stream, response_pdu)
        return True

    def _dispatch(self, addr: int, request_pdu: bytes) -> bytes | None:
        """Shared request handling for both framings.

        Returns the response PDU to emit, or ``None`` when nothing should be
        sent (request for another slave, or a broadcast — *serial §2.1*).
        """
        if addr not in (self.address, _BROADCAST_ADDRESS):
            return None
        try:
            response_pdu = self._handle_request(request_pdu)
        except _ServerException as exc:
            response_pdu = bytes((request_pdu[0] | 0x80, int(exc.code)))
        if addr == _BROADCAST_ADDRESS:
            return None
        return response_pdu

    def _should_drop_or_delay(self) -> tuple[bool, int]:
        """Advance the response counter; return ``(dropped, idx)`` for this response."""
        idx = self._responses_emitted
        self._responses_emitted += 1
        plan = self.faults
        if plan.drop_response_after_n is not None and idx == plan.drop_response_after_n:
            _LOGGER.info("MockSlave: dropping response %d per FaultPlan", idx)
            return True, idx
        return False, idx

    async def _send_response_rtu(self, stream: anyio.abc.ByteStream, response_pdu: bytes) -> None:
        dropped, idx = self._should_drop_or_delay()
        if dropped:
            return
        plan = self.faults
        if plan.delay_response_seconds > 0:
            await anyio.sleep(plan.delay_response_seconds)

        slave_byte = (
            plan.wrong_slave_address if plan.wrong_slave_address is not None else self.address
        )
        head = bytes((slave_byte,)) + response_pdu
        crc = crc16_modbus_bytes(head)

        if plan.corrupt_crc_after_n is not None and idx == plan.corrupt_crc_after_n:
            _LOGGER.info("MockSlave: corrupting CRC on response %d per FaultPlan", idx)
            crc = bytes((crc[0] ^ 0x01, crc[1]))

        await stream.send(head + crc)

    async def _send_response_ascii(self, stream: anyio.abc.ByteStream, response_pdu: bytes) -> None:
        dropped, idx = self._should_drop_or_delay()
        if dropped:
            return
        plan = self.faults
        if plan.delay_response_seconds > 0:
            await anyio.sleep(plan.delay_response_seconds)

        slave_byte = (
            plan.wrong_slave_address if plan.wrong_slave_address is not None else self.address
        )
        if plan.corrupt_crc_after_n is not None and idx == plan.corrupt_crc_after_n:
            # Same fault, framing-aware: corrupt the LRC by flipping a bit of
            # the binary frame before hex-encoding, so the client sees LRCError.
            _LOGGER.info("MockSlave: corrupting LRC on response %d per FaultPlan", idx)
            body = bytes((slave_byte,)) + response_pdu
            frame = bytearray(body + lrc8_bytes(body))
            frame[-1] ^= 0x01
            await stream.send(b":" + frame.hex().upper().encode("ascii") + b"\r\n")
            return

        await stream.send(encode_ascii_adu(slave_address=slave_byte, pdu=response_pdu))

    # ------------------------------------------------------------------
    # Per-FC handlers. Each takes the request PDU (FC byte + body) and
    # returns the response PDU (FC byte + body), or raises
    # :class:`_ServerException` to be translated into an exception PDU.
    # ------------------------------------------------------------------

    def _handle_request(self, pdu: bytes) -> bytes:  # noqa: PLR0911 — one return per FC
        fc = pdu[0]
        if fc in self.disabled_function_codes:
            raise _ServerException(ExceptionCode.ILLEGAL_FUNCTION)
        if fc == FunctionCode.READ_COILS:
            return self._handle_read_bits(pdu, self.coils, self._coil_count)
        if fc == FunctionCode.READ_DISCRETE_INPUTS:
            return self._handle_read_bits(pdu, self.discrete_inputs, self._discrete_input_count)
        if fc == FunctionCode.READ_HOLDING_REGISTERS:
            return self._handle_read_registers(pdu, self.holding_registers)
        if fc == FunctionCode.READ_INPUT_REGISTERS:
            return self._handle_read_registers(pdu, self.input_registers)
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
        raise _ServerException(ExceptionCode.ILLEGAL_FUNCTION)

    def _handle_diagnostic_loopback(self, pdu: bytes) -> bytes:
        # FC 0x08 sub 0x0000 (Return Query Data): echo fc + subfn + data word.
        if len(pdu) != _DIAG_REQUEST_PDU_LEN:
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        (subfn,) = struct.unpack(">H", pdu[1:3])
        if subfn != _DIAG_SUBFN_RETURN_QUERY_DATA:
            # We model sub-0 only; reject other sub-functions as the spec allows.
            raise _ServerException(ExceptionCode.ILLEGAL_FUNCTION)
        return pdu

    def _handle_read_bits(self, pdu: bytes, bank: bytearray, bank_size: int) -> bytes:
        fc, addr, count = struct.unpack(">BHH", pdu)
        if not (1 <= count <= _MAX_READ_BITS):
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if addr + count > bank_size:
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        nbytes = (count + 7) // 8
        out = bytearray(nbytes)
        for i in range(count):
            src = addr + i
            if bank[src >> 3] & (1 << (src & 7)):
                out[i >> 3] |= 1 << (i & 7)
        return bytes((fc, nbytes)) + bytes(out)

    def _handle_read_registers(self, pdu: bytes, bank: list[int]) -> bytes:
        fc, addr, count = struct.unpack(">BHH", pdu)
        if not (1 <= count <= _MAX_READ_REGISTERS):
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if addr + count > len(bank):
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        values = bank[addr : addr + count]
        return bytes((fc, count * 2)) + struct.pack(f">{count}H", *values)

    def _handle_write_single_coil(self, pdu: bytes) -> bytes:
        _, addr, value = struct.unpack(">BHH", pdu)
        if value not in (_COIL_ON, _COIL_OFF):
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if addr >= self._coil_count:
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        if value == _COIL_ON:
            self.coils[addr >> 3] |= 1 << (addr & 7)
        else:
            self.coils[addr >> 3] &= (~(1 << (addr & 7))) & 0xFF
        return pdu  # FC 0x05 echoes the request

    def _handle_write_single_register(self, pdu: bytes) -> bytes:
        _, addr, value = struct.unpack(">BHH", pdu)
        if addr >= len(self.holding_registers):
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        self.holding_registers[addr] = value
        return pdu  # FC 0x06 echoes the request

    def _handle_write_multiple_coils(self, pdu: bytes) -> bytes:
        if len(pdu) < _WRITE_MULTIPLE_REQUEST_PREFIX_LEN:
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        fc, addr, count, byte_count = struct.unpack(
            ">BHHB", pdu[:_WRITE_MULTIPLE_REQUEST_PREFIX_LEN]
        )
        data = pdu[_WRITE_MULTIPLE_REQUEST_PREFIX_LEN:]
        if not (1 <= count <= _MAX_WRITE_COILS):
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if byte_count != (count + 7) // 8 or len(data) != byte_count:
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if addr + count > self._coil_count:
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        for i in range(count):
            target = addr + i
            bit = (data[i >> 3] >> (i & 7)) & 1
            if bit:
                self.coils[target >> 3] |= 1 << (target & 7)
            else:
                self.coils[target >> 3] &= (~(1 << (target & 7))) & 0xFF
        return struct.pack(">BHH", fc, addr, count)

    def _handle_write_multiple_registers(self, pdu: bytes) -> bytes:
        if len(pdu) < _WRITE_MULTIPLE_REQUEST_PREFIX_LEN:
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        fc, addr, count, byte_count = struct.unpack(
            ">BHHB", pdu[:_WRITE_MULTIPLE_REQUEST_PREFIX_LEN]
        )
        data = pdu[_WRITE_MULTIPLE_REQUEST_PREFIX_LEN:]
        if not (1 <= count <= _MAX_WRITE_REGISTERS):
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if byte_count != count * 2 or len(data) != byte_count:
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_VALUE)
        if addr + count > len(self.holding_registers):
            raise _ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        values = struct.unpack(f">{count}H", data)
        for i, v in enumerate(values):
            self.holding_registers[addr + i] = v
        return struct.pack(">BHH", fc, addr, count)

    @staticmethod
    async def _read_exact(stream: anyio.abc.ByteStream, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = await stream.receive(n - len(buf))
            if not chunk:
                # AnyIO contract: receive returns >=1 byte or raises EndOfStream.
                raise anyio.EndOfStream
            buf.extend(chunk)
        return bytes(buf)


__all__ = ["MockSlave"]
