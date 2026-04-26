"""Per-slave handle bound to a :class:`Bus` and a Modbus unit address.

A :class:`Slave` is cheap — it holds a reference to the bus and an
``int`` address, nothing else. Methods build the request PDU via
:mod:`anymodbus.pdu`, hand it to ``Bus._txn``, and decode the response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from anymodbus._types import ByteOrder, Capability, FunctionCode, WordOrder
from anymodbus.capabilities import SlaveCapabilities
from anymodbus.decoders import (
    decode_float32,
    decode_int32,
    decode_string,
    encode_float32,
    encode_int32,
    encode_string,
)
from anymodbus.exceptions import (
    ConfigurationError,
    ConnectionLostError,
    FrameTimeoutError,
    IllegalDataAddressError,
    IllegalFunctionError,
)
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

if TYPE_CHECKING:
    from collections.abc import Sequence

    from anymodbus.bus import Bus

_MIN_UNICAST_ADDRESS = 0x01
_MAX_UNICAST_ADDRESS = 0xF7  # 247 — *Modbus over Serial Line v1.02 §2.2*

# Number of registers needed for a 32-bit float / int.
_REGISTERS_PER_32BIT = 2

# Function codes :meth:`Slave.probe` actively probes (reads only). Writes are
# excluded by design — there's no spec-defined non-destructive write probe, so
# any FC 5/6/F/10 result reported by the slave on a real probe would mutate
# state. They surface as :attr:`Capability.UNKNOWN` instead.
_PROBE_READ_FUNCTION_CODES: tuple[FunctionCode, ...] = (
    FunctionCode.READ_COILS,
    FunctionCode.READ_DISCRETE_INPUTS,
    FunctionCode.READ_HOLDING_REGISTERS,
    FunctionCode.READ_INPUT_REGISTERS,
)

# Write FCs we deliberately do NOT probe — the result would mutate slave state.
# Reported as UNKNOWN so callers can distinguish "we didn't probe" from
# "the slave refused".
_PROBE_NON_PROBED_FUNCTION_CODES: tuple[FunctionCode, ...] = (
    FunctionCode.WRITE_SINGLE_COIL,
    FunctionCode.WRITE_SINGLE_REGISTER,
    FunctionCode.WRITE_MULTIPLE_COILS,
    FunctionCode.WRITE_MULTIPLE_REGISTERS,
)

# Addresses tried in sequence on IllegalDataAddressError. A reasonable spread
# across the typical 0-65535 register space; if none of these are valid the
# probe gives up and reports UNKNOWN for that FC.
_PROBE_ADDRESS_WALK: tuple[int, ...] = (0x0000, 0x0001, 0x0040, 0x0100, 0x1000)


class Slave:
    """A handle for talking to one Modbus slave on a :class:`Bus`.

    Per *Modbus over Serial Line v1.02 §2.2*, only addresses 1-247 are valid
    unicast slave addresses. Address 0 is the broadcast address — broadcasts
    go through :meth:`Bus.broadcast_write_coil` / :meth:`Bus.broadcast_*` so
    callers can't accidentally broadcast a read FC. Addresses 248-255 are
    reserved by the spec.
    """

    __slots__ = ("_address", "_bus", "_capabilities")

    def __init__(self, bus: Bus, address: int) -> None:
        if address == 0:
            msg = "Use Bus.broadcast_* methods for broadcasts; bus.slave() is unicast only"
            raise ConfigurationError(msg)
        if not (_MIN_UNICAST_ADDRESS <= address <= _MAX_UNICAST_ADDRESS):
            msg = (
                f"Slave address must be 1-247. Address 0 is broadcast "
                f"(see Bus.broadcast_*); 248-255 are reserved by the spec. "
                f"(got {address!r})"
            )
            raise ConfigurationError(msg)
        self._bus = bus
        self._address = address
        self._capabilities: SlaveCapabilities | None = None

    @property
    def address(self) -> int:
        """Modbus unit address (1-247). Validated at construction; read-only."""
        return self._address

    @property
    def bus(self) -> Bus:
        """The :class:`Bus` this slave handle was created from."""
        return self._bus

    @property
    def capabilities(self) -> SlaveCapabilities | None:
        """Cached capabilities from the most recent :meth:`probe`, or ``None``."""
        return self._capabilities

    # ------------------------------------------------------------------
    # Standard read function codes
    # ------------------------------------------------------------------

    async def read_coils(self, address: int, *, count: int) -> tuple[bool, ...]:
        """FC 0x01 — Read ``count`` coils starting at ``address``."""
        pdu = encode_read_coils_request(address, count)
        response_pdu = await self._bus._txn(  # pyright: ignore[reportPrivateUsage]
            slave_address=self.address,
            request_pdu=pdu,
            expected_function_code=FunctionCode.READ_COILS,
        )
        return decode_read_coils_response(response_pdu, expected_count=count)

    async def read_discrete_inputs(self, address: int, *, count: int) -> tuple[bool, ...]:
        """FC 0x02 — Read ``count`` discrete inputs starting at ``address``."""
        pdu = encode_read_discrete_inputs_request(address, count)
        response_pdu = await self._bus._txn(  # pyright: ignore[reportPrivateUsage]
            slave_address=self.address,
            request_pdu=pdu,
            expected_function_code=FunctionCode.READ_DISCRETE_INPUTS,
        )
        return decode_read_discrete_inputs_response(response_pdu, expected_count=count)

    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        """FC 0x03 — Read ``count`` holding registers starting at ``address``."""
        pdu = encode_read_holding_registers_request(address, count)
        response_pdu = await self._bus._txn(  # pyright: ignore[reportPrivateUsage]
            slave_address=self.address,
            request_pdu=pdu,
            expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
        )
        return decode_read_holding_registers_response(response_pdu)

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        """FC 0x04 — Read ``count`` input registers starting at ``address``."""
        pdu = encode_read_input_registers_request(address, count)
        response_pdu = await self._bus._txn(  # pyright: ignore[reportPrivateUsage]
            slave_address=self.address,
            request_pdu=pdu,
            expected_function_code=FunctionCode.READ_INPUT_REGISTERS,
        )
        return decode_read_input_registers_response(response_pdu)

    # ------------------------------------------------------------------
    # Standard write function codes
    # ------------------------------------------------------------------

    async def write_coil(self, address: int, *, on: bool) -> None:
        """FC 0x05 — Write a single coil. The slave's echo is verified."""
        pdu = encode_write_single_coil_request(address, on=on)
        response_pdu = await self._bus._txn(  # pyright: ignore[reportPrivateUsage]
            slave_address=self.address,
            request_pdu=pdu,
            expected_function_code=FunctionCode.WRITE_SINGLE_COIL,
        )
        # Validates the wire value is 0xFF00/0x0000 per *app §6.5*.
        decode_write_single_coil_response(response_pdu)

    async def write_register(self, address: int, value: int) -> None:
        """FC 0x06 — Write a single holding register."""
        pdu = encode_write_single_register_request(address, value)
        response_pdu = await self._bus._txn(  # pyright: ignore[reportPrivateUsage]
            slave_address=self.address,
            request_pdu=pdu,
            expected_function_code=FunctionCode.WRITE_SINGLE_REGISTER,
        )
        decode_write_single_register_response(response_pdu)

    async def write_coils(self, address: int, values: Sequence[bool]) -> None:
        """FC 0x0F — Write multiple coils."""
        pdu = encode_write_multiple_coils_request(address, values)
        response_pdu = await self._bus._txn(  # pyright: ignore[reportPrivateUsage]
            slave_address=self.address,
            request_pdu=pdu,
            expected_function_code=FunctionCode.WRITE_MULTIPLE_COILS,
        )
        decode_write_multiple_coils_response(response_pdu)

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        """FC 0x10 — Write multiple holding registers."""
        pdu = encode_write_multiple_registers_request(address, values)
        response_pdu = await self._bus._txn(  # pyright: ignore[reportPrivateUsage]
            slave_address=self.address,
            request_pdu=pdu,
            expected_function_code=FunctionCode.WRITE_MULTIPLE_REGISTERS,
        )
        decode_write_multiple_registers_response(response_pdu)

    # ------------------------------------------------------------------
    # Higher-level helpers — float / int32 / string with explicit ordering.
    # ------------------------------------------------------------------

    async def read_float(
        self,
        address: int,
        *,
        word_order: WordOrder = WordOrder.HIGH_LOW,
        byte_order: ByteOrder = ByteOrder.BIG,
    ) -> float:
        """Read two registers and decode as IEEE 754 float32.

        Defaults are :attr:`WordOrder.HIGH_LOW` / :attr:`ByteOrder.BIG`,
        equivalent to ``struct.pack(">f", ...)``. Real devices vary — the
        Modbus spec does not standardize multi-register word ordering. Pass
        ``word_order`` / ``byte_order`` explicitly when your device's
        protocol manual specifies a different layout.
        """
        words = await self.read_holding_registers(address, count=_REGISTERS_PER_32BIT)
        return decode_float32(words, word_order=word_order, byte_order=byte_order)

    async def write_float(
        self,
        address: int,
        value: float,
        *,
        word_order: WordOrder = WordOrder.HIGH_LOW,
        byte_order: ByteOrder = ByteOrder.BIG,
    ) -> None:
        """Encode ``value`` as IEEE 754 float32 and write into two registers."""
        words = encode_float32(value, word_order=word_order, byte_order=byte_order)
        await self.write_registers(address, words)

    async def read_int32(
        self,
        address: int,
        *,
        signed: bool = True,
        word_order: WordOrder = WordOrder.HIGH_LOW,
        byte_order: ByteOrder = ByteOrder.BIG,
    ) -> int:
        """Read two registers and decode as a 32-bit (signed by default) integer."""
        words = await self.read_holding_registers(address, count=_REGISTERS_PER_32BIT)
        return decode_int32(words, signed=signed, word_order=word_order, byte_order=byte_order)

    async def write_int32(
        self,
        address: int,
        value: int,
        *,
        signed: bool = True,
        word_order: WordOrder = WordOrder.HIGH_LOW,
        byte_order: ByteOrder = ByteOrder.BIG,
    ) -> None:
        """Encode ``value`` as a 32-bit integer and write into two registers."""
        words = encode_int32(value, signed=signed, word_order=word_order, byte_order=byte_order)
        await self.write_registers(address, words)

    async def read_string(
        self,
        address: int,
        *,
        register_count: int | None = None,
        byte_count: int | None = None,
        byte_order: ByteOrder = ByteOrder.BIG,
        encoding: str = "ascii",
        strip_null: bool = True,
    ) -> str:
        """Read a string field. Specify length as ``register_count`` or ``byte_count``.

        ``byte_count`` is convenient for spec'd-in-bytes fields whose length
        isn't a multiple of two: the read is rounded up to
        ``ceil(byte_count / 2)`` registers and the decoded string is then
        truncated to ``byte_count`` bytes (before optional null-stripping).
        Exactly one of ``register_count`` / ``byte_count`` must be supplied.

        ``byte_order=LITTLE`` is for devices that store strings byte-swapped
        within each register.
        """
        if (register_count is None) == (byte_count is None):
            msg = "supply exactly one of register_count or byte_count"
            raise ConfigurationError(msg)
        if register_count is None:
            assert byte_count is not None
            count = (byte_count + 1) // 2
        else:
            count = register_count
        words = await self.read_holding_registers(address, count=count)
        decoded = decode_string(words, byte_order=byte_order, encoding=encoding, strip_null=False)
        if byte_count is not None:
            decoded = decoded[:byte_count]
        if strip_null:
            decoded = decoded.rstrip("\x00")
        return decoded

    async def write_string(
        self,
        address: int,
        value: str,
        *,
        register_count: int | None = None,
        byte_count: int | None = None,
        byte_order: ByteOrder = ByteOrder.BIG,
        encoding: str = "ascii",
        pad: bytes = b"\x00",
    ) -> None:
        """Encode ``value`` and write it. Length specified as registers or bytes.

        See :func:`anymodbus.decoders.encode_string` for the encoding rules.
        """
        words = encode_string(
            value,
            register_count=register_count,
            byte_count=byte_count,
            byte_order=byte_order,
            encoding=encoding,
            pad=pad,
        )
        await self.write_registers(address, words)

    # ------------------------------------------------------------------
    # Capability probing
    # ------------------------------------------------------------------

    async def probe(self) -> SlaveCapabilities:
        """Probe the slave for function-code support. Caches the result.

        Issues one ``count=1`` request per read FC (1-4) and walks a small
        set of probe addresses on :class:`IllegalDataAddressError` until one
        succeeds (or the walk is exhausted). Outcomes are mapped per the
        rubric in :mod:`anymodbus.capabilities`:

        - successful response → :attr:`Capability.SUPPORTED`
        - :class:`IllegalFunctionError` → :attr:`Capability.UNSUPPORTED`
        - :class:`IllegalDataAddressError` after walking every probe address
          → :attr:`Capability.UNKNOWN` (the FC is supported but we couldn't
          find a valid address; downstream code should not rely on this FC
          being usable without a known-valid address)
        - :class:`FrameTimeoutError` / :class:`ConnectionLostError` →
          :attr:`Capability.UNKNOWN`. Probing is short-circuited after the
          first such error: a silent or disconnected slave will keep
          timing out, and we don't want to wait through
          ``len(FCs) * request_timeout`` seconds before giving up.

        Write FCs (5, 6, 0x0F, 0x10) are **never** probed — there's no
        spec-defined non-destructive write probe. They surface as
        :attr:`Capability.UNKNOWN` so callers can distinguish "we didn't
        probe" from "the slave refused".

        Cancellation: probes are issued sequentially. If the surrounding
        scope cancels mid-probe, ``self._capabilities`` is **not** updated
        — the cache is all-or-nothing for clarity.

        Returns:
            The freshly built :class:`SlaveCapabilities`. Also assigned to
            :attr:`capabilities` so subsequent reads don't re-probe.
        """
        verdicts: dict[FunctionCode, Capability] = {}
        bus_unresponsive = False

        for fc in _PROBE_READ_FUNCTION_CODES:
            if bus_unresponsive:
                verdicts[fc] = Capability.UNKNOWN
                continue
            verdict, became_unresponsive = await self._probe_one_read_fc(fc)
            verdicts[fc] = verdict
            if became_unresponsive:
                bus_unresponsive = True

        for fc in _PROBE_NON_PROBED_FUNCTION_CODES:
            verdicts[fc] = Capability.UNKNOWN

        capabilities = SlaveCapabilities(function_codes=verdicts)
        self._capabilities = capabilities
        return capabilities

    async def _probe_one_read_fc(self, fc: FunctionCode) -> tuple[Capability, bool]:
        """Probe a single read FC, returning ``(verdict, bus_unresponsive)``.

        ``bus_unresponsive=True`` signals that the surrounding probe loop
        should short-circuit further probes — a timeout / disconnect on one
        FC almost certainly means the rest will time out too.
        """
        last_address_error_seen = False
        for probe_address in _PROBE_ADDRESS_WALK:
            try:
                if fc is FunctionCode.READ_COILS:
                    await self.read_coils(probe_address, count=1)
                elif fc is FunctionCode.READ_DISCRETE_INPUTS:
                    await self.read_discrete_inputs(probe_address, count=1)
                elif fc is FunctionCode.READ_HOLDING_REGISTERS:
                    await self.read_holding_registers(probe_address, count=1)
                elif fc is FunctionCode.READ_INPUT_REGISTERS:
                    await self.read_input_registers(probe_address, count=1)
                else:
                    # _PROBE_READ_FUNCTION_CODES is closed; this branch is
                    # unreachable but keeps mypy/pyright exhaustive.
                    msg = f"unexpected probe FC: {fc!r}"
                    raise AssertionError(msg)
            except IllegalFunctionError:
                return Capability.UNSUPPORTED, False
            except IllegalDataAddressError:
                last_address_error_seen = True
                continue  # walk to the next probe address
            except (FrameTimeoutError, ConnectionLostError):
                return Capability.UNKNOWN, True
            else:
                return Capability.SUPPORTED, False

        # Walk exhausted with only IllegalDataAddressError responses. The FC
        # is implemented but every address we tried was invalid — surface as
        # UNKNOWN so callers don't assume blanket support.
        if last_address_error_seen:
            return Capability.UNKNOWN, False
        # Loop body never ran (empty walk) — defensive, not reachable today.
        return Capability.UNKNOWN, False  # pragma: no cover


__all__ = ["Slave"]
