"""End-to-end ASCII-framing and FC08-loopback integration tests.

The standard FC matrix is run under **both** RTU and ASCII framing off one
register bank (handoff §F parity), plus FC08 sub-0 loopback, exception frames
over ASCII, and the ASCII checksum-corruption fault path.
"""

from __future__ import annotations

import math

import anyio
import pytest

from anymodbus import (
    BusConfig,
    IllegalDataAddressError,
    IllegalFunctionError,
    LRCError,
    RegisterSource,
    RetryPolicy,
)
from anymodbus._types import Framing, FunctionCode
from anymodbus.decoders import encode_float32
from anymodbus.testing import FaultPlan, client_slave_pair

pytestmark = pytest.mark.anyio

_FRAMINGS = [
    pytest.param(Framing.RTU, id="rtu"),
    pytest.param(Framing.ASCII, id="ascii"),
]


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_read_holding_registers_roundtrip(framing: Framing) -> None:
    async with client_slave_pair(framing=framing) as (bus, slave):
        slave.holding_registers[0:3] = [0x1234, 0xABCD, 0xFFFF]
        result = await bus.slave(1).read_holding_registers(0, count=3)
    assert result == (0x1234, 0xABCD, 0xFFFF)
    assert bus.framing is framing


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_read_input_registers_roundtrip(framing: Framing) -> None:
    async with client_slave_pair(framing=framing) as (bus, slave):
        slave.input_registers[0] = 0x5555
        result = await bus.slave(1).read_input_registers(0, count=1)
    assert result == (0x5555,)


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_read_coils_roundtrip(framing: Framing) -> None:
    async with client_slave_pair(framing=framing) as (bus, slave):
        slave.coils[0] = 0b0010_0101
        result = await bus.slave(1).read_coils(0, count=8)
    assert result == (True, False, True, False, False, True, False, False)


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_write_single_register_persists(framing: Framing) -> None:
    async with client_slave_pair(framing=framing) as (bus, slave):
        await bus.slave(1).write_register(5, 0xCAFE)
    assert slave.holding_registers[5] == 0xCAFE


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_write_multiple_registers_persists(framing: Framing) -> None:
    async with client_slave_pair(framing=framing) as (bus, slave):
        await bus.slave(1).write_registers(10, [0x0001, 0x0203, 0x0405])
    assert slave.holding_registers[10:13] == [0x0001, 0x0203, 0x0405]


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_diagnostic_loopback_echoes(framing: Framing) -> None:
    async with client_slave_pair(framing=framing) as (bus, _slave):
        echoed = await bus.slave(1).diagnostic_loopback(b"\xab\xcd")
    assert echoed == b"\xab\xcd"


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_diagnostic_loopback_default_zero(framing: Framing) -> None:
    async with client_slave_pair(framing=framing) as (bus, _slave):
        echoed = await bus.slave(1).diagnostic_loopback()
    assert echoed == b"\x00\x00"


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_out_of_range_raises_illegal_data_address(framing: Framing) -> None:
    async with client_slave_pair(register_count=16, framing=framing) as (bus, _slave):
        with pytest.raises(IllegalDataAddressError):
            await bus.slave(1).read_holding_registers(15, count=4)


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_disabled_fc_raises_illegal_function(framing: Framing) -> None:
    disabled = frozenset({int(FunctionCode.READ_INPUT_REGISTERS)})
    async with client_slave_pair(framing=framing, disabled_function_codes=disabled) as (
        bus,
        _slave,
    ):
        with pytest.raises(IllegalFunctionError):
            await bus.slave(1).read_input_registers(0, count=1)


@pytest.mark.parametrize("framing", _FRAMINGS)
async def test_read_float_input_source(framing: Framing) -> None:
    # The servomex idle-O2 cross-check: 20.378 as float32 in two input regs.
    async with client_slave_pair(framing=framing) as (bus, slave):
        slave.input_registers[0:2] = list(encode_float32(20.378))
        value = await bus.slave(1).read_float(0, source=RegisterSource.INPUT)
    # math.isclose over pytest.approx — approx's stubs leak Unknown under pyright strict.
    assert math.isclose(value, 20.378, rel_tol=1e-6)


async def test_read_float_defaults_to_holding_fc03() -> None:
    # With FC04 disabled, read_float (default source=HOLDING) still works via
    # FC03, and an explicit source=INPUT is refused — proving the default bank.
    disabled = frozenset({int(FunctionCode.READ_INPUT_REGISTERS)})
    async with client_slave_pair(disabled_function_codes=disabled) as (bus, slave):
        slave.holding_registers[0:2] = list(encode_float32(1.5))
        assert math.isclose(await bus.slave(1).read_float(0), 1.5, rel_tol=1e-6)
        with pytest.raises(IllegalFunctionError):
            await bus.slave(1).read_float(0, source=RegisterSource.INPUT)


async def test_startup_settle_delays_only_first_transaction() -> None:
    from anymodbus import TimingConfig  # noqa: PLC0415

    settle = 0.2
    cfg = BusConfig(timing=TimingConfig(startup_settle=settle))
    async with client_slave_pair(bus_config=cfg) as (bus, slave):
        slave.holding_registers[0] = 0x1234
        t0 = anyio.current_time()
        await bus.slave(1).read_holding_registers(0, count=1)
        first_elapsed = anyio.current_time() - t0
        t1 = anyio.current_time()
        await bus.slave(1).read_holding_registers(0, count=1)
        second_elapsed = anyio.current_time() - t1
    # First transaction pays the one-shot settle; the second does not.
    assert first_elapsed >= settle
    assert second_elapsed < settle


# ---------------------------------------------------------------------------
# ASCII-specific: the corrupt-checksum fault corrupts the LRC -> LRCError.
# ---------------------------------------------------------------------------


async def test_ascii_corrupt_lrc_surfaces_as_lrc_error() -> None:
    cfg = BusConfig(retries=RetryPolicy(retries=0))
    plan = FaultPlan(corrupt_crc_after_n=0)
    async with client_slave_pair(framing=Framing.ASCII, faults=plan, bus_config=cfg) as (
        bus,
        slave,
    ):
        slave.holding_registers[0] = 0xDEAD
        with pytest.raises(LRCError):
            await bus.slave(1).read_holding_registers(0, count=1)


async def test_ascii_corrupt_lrc_recovers_via_retry() -> None:
    cfg = BusConfig(retries=RetryPolicy(retries=1))
    plan = FaultPlan(corrupt_crc_after_n=0)  # first response only
    async with client_slave_pair(framing=Framing.ASCII, faults=plan, bus_config=cfg) as (
        bus,
        slave,
    ):
        slave.holding_registers[0] = 0xCAFE
        result = await bus.slave(1).read_holding_registers(0, count=1)
    assert result == (0xCAFE,)
