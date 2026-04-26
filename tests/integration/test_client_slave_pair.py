"""End-to-end tests using :func:`client_slave_pair` and :class:`MockSlave`.

Exercises every standard FC (0x01-0x06, 0x0F, 0x10) through the full client
+ framer + serial-pair + mock-slave stack. These tests are what make the
length-aware framer, the inter-frame timing, and the per-FC encoders
honest under realistic byte flow — no stubs above the wire.
"""

from __future__ import annotations

import anyio
import pytest

from anymodbus import (
    BusConfig,
    IllegalDataAddressError,
    IllegalDataValueError,
    RetryPolicy,
)
from anymodbus.testing import client_slave_pair


@pytest.mark.anyio
async def test_read_coils_returns_preloaded_state() -> None:
    async with client_slave_pair() as (bus, slave):
        # Preload coils 0,2,5 → ON; rest OFF.
        slave.coils[0] = 0b0010_0101
        result = await bus.slave(1).read_coils(0, count=8)
    assert result == (True, False, True, False, False, True, False, False)


@pytest.mark.anyio
async def test_read_coils_spans_multiple_bytes() -> None:
    async with client_slave_pair() as (bus, slave):
        slave.coils[0] = 0xFF
        slave.coils[1] = 0x00
        slave.coils[2] = 0xAA
        result = await bus.slave(1).read_coils(0, count=20)
    expected = (
        (True,) * 8 + (False,) * 8 + (False, True, False, True)  # bits 0..3 of 0xAA, LSB-first
    )
    assert result == expected


@pytest.mark.anyio
async def test_read_discrete_inputs_independent_from_coils() -> None:
    async with client_slave_pair() as (bus, slave):
        slave.coils[0] = 0xFF
        slave.discrete_inputs[0] = 0x00
        coils = await bus.slave(1).read_coils(0, count=8)
        discrete = await bus.slave(1).read_discrete_inputs(0, count=8)
    assert coils == (True,) * 8
    assert discrete == (False,) * 8


@pytest.mark.anyio
async def test_read_holding_registers_roundtrip() -> None:
    async with client_slave_pair() as (bus, slave):
        slave.holding_registers[0:4] = [0x1234, 0xABCD, 0x0000, 0xFFFF]
        result = await bus.slave(1).read_holding_registers(0, count=4)
    assert result == (0x1234, 0xABCD, 0x0000, 0xFFFF)


@pytest.mark.anyio
async def test_read_input_registers_independent_from_holding() -> None:
    async with client_slave_pair() as (bus, slave):
        slave.holding_registers[0] = 0xAAAA
        slave.input_registers[0] = 0x5555
        h = await bus.slave(1).read_holding_registers(0, count=1)
        i = await bus.slave(1).read_input_registers(0, count=1)
    assert h == (0xAAAA,)
    assert i == (0x5555,)


@pytest.mark.anyio
async def test_write_single_coil_persists_in_bank() -> None:
    async with client_slave_pair() as (bus, slave):
        await bus.slave(1).write_coil(3, on=True)
        await bus.slave(1).write_coil(7, on=True)
    assert slave.coils[0] == 0b1000_1000


@pytest.mark.anyio
async def test_write_single_register_persists_in_bank() -> None:
    async with client_slave_pair() as (bus, slave):
        await bus.slave(1).write_register(5, 0xCAFE)
    assert slave.holding_registers[5] == 0xCAFE


@pytest.mark.anyio
async def test_write_multiple_coils_persists_in_bank() -> None:
    async with client_slave_pair() as (bus, slave):
        await bus.slave(1).write_coils(
            0, [True, False, True, True, False, False, False, True, True, False]
        )
    # Bits 0,2,3,7,8 set.
    assert slave.coils[0] == 0b1000_1101
    assert slave.coils[1] == 0b0000_0001


@pytest.mark.anyio
async def test_write_multiple_registers_persists_in_bank() -> None:
    async with client_slave_pair() as (bus, slave):
        await bus.slave(1).write_registers(10, [0x0001, 0x0203, 0x0405])
    assert slave.holding_registers[10:13] == [0x0001, 0x0203, 0x0405]


@pytest.mark.anyio
async def test_read_out_of_range_raises_illegal_data_address() -> None:
    async with client_slave_pair(register_count=16) as (bus, _slave):
        with pytest.raises(IllegalDataAddressError):
            await bus.slave(1).read_holding_registers(15, count=4)


@pytest.mark.anyio
async def test_write_out_of_range_raises_illegal_data_address() -> None:
    async with client_slave_pair(register_count=16) as (bus, _slave):
        with pytest.raises(IllegalDataAddressError):
            await bus.slave(1).write_register(20, 0x1234)


@pytest.mark.anyio
async def test_slave_rejects_oversize_quantity_with_illegal_data_value() -> None:
    """A request whose count exceeds the per-FC max is rejected by the slave."""
    import struct  # noqa: PLC0415

    from anymodbus._types import FunctionCode  # noqa: PLC0415

    async with client_slave_pair() as (bus, _slave):
        # Count 200 exceeds FC 3's max of 125. The high-level encoder would
        # refuse client-side; we go through Bus._txn with a hand-built PDU to
        # validate the slave honours the bound too.
        bad_pdu = struct.pack(">BHH", FunctionCode.READ_HOLDING_REGISTERS, 0, 200)
        with pytest.raises(IllegalDataValueError):
            await bus._txn(  # pyright: ignore[reportPrivateUsage]
                slave_address=1,
                request_pdu=bad_pdu,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            )


@pytest.mark.anyio
async def test_concurrent_callers_serialize_through_bus_lock() -> None:
    """16 concurrent reads on one bus must not interleave frames or fuse."""
    async with client_slave_pair() as (bus, slave):
        # Preload 16 distinct register values so we can detect cross-talk.
        for i in range(16):
            slave.holding_registers[i] = 0x1000 | i

        results: dict[int, int] = {}

        async def fetch(addr: int) -> None:
            (val,) = await bus.slave(1).read_holding_registers(addr, count=1)
            results[addr] = val

        async with anyio.create_task_group() as tg:
            for addr in range(16):
                tg.start_soon(fetch, addr)

    assert results == {addr: 0x1000 | addr for addr in range(16)}


@pytest.mark.anyio
async def test_request_to_other_slave_address_is_ignored() -> None:
    """A request addressed to a different slave produces no response → timeout."""
    cfg = BusConfig(request_timeout=0.05, retries=RetryPolicy(retries=0))
    async with client_slave_pair(slave_address=1, bus_config=cfg) as (bus, _slave):
        from anymodbus import FrameTimeoutError  # noqa: PLC0415

        with pytest.raises(FrameTimeoutError):
            # We have a slave at addr 1; ask for addr 2.
            await bus.slave(2).read_holding_registers(0, count=1)


@pytest.mark.anyio
async def test_broadcast_write_register_is_applied_no_response() -> None:
    """Broadcasts apply the write but elicit no response."""
    async with client_slave_pair() as (bus, slave):
        await bus.broadcast_write_register(7, 0xBEEF)
    assert slave.holding_registers[7] == 0xBEEF


@pytest.mark.anyio
async def test_broadcast_write_coils_is_applied() -> None:
    async with client_slave_pair() as (bus, slave):
        await bus.broadcast_write_coils(0, [True, True, False, False, True])
    assert slave.coils[0] == 0b0001_0011


@pytest.mark.anyio
async def test_write_string_roundtrip() -> None:
    """``write_string`` followed by ``read_string`` returns the original value."""
    async with client_slave_pair() as (bus, _slave):
        await bus.slave(1).write_string(20, "Hi", register_count=2)
        result = await bus.slave(1).read_string(20, register_count=2)
    assert result == "Hi"


@pytest.mark.anyio
async def test_write_string_pads_short_value() -> None:
    """Values shorter than the register window are right-padded with NULs."""
    async with client_slave_pair() as (bus, slave):
        await bus.slave(1).write_string(30, "Hi", register_count=2)
    # "Hi" + NUL NUL → registers 0x4869, 0x0000.
    assert slave.holding_registers[30] == 0x4869
    assert slave.holding_registers[31] == 0x0000


@pytest.mark.anyio
async def test_independent_discrete_input_count_is_honoured() -> None:
    """``discrete_input_count`` sizes the discrete-inputs bank independently."""
    # Coils get 8, discrete inputs get 32.
    async with client_slave_pair(coil_count=8, discrete_input_count=32) as (bus, _slave):
        # Reading discrete inputs at addr 16 (out-of-range under conjoined sizing,
        # in-range now) must succeed.
        result = await bus.slave(1).read_discrete_inputs(16, count=8)
    assert result == (False,) * 8


@pytest.mark.anyio
async def test_independent_input_register_count_is_honoured() -> None:
    """``input_register_count`` sizes the input register bank independently."""
    async with client_slave_pair(register_count=4, input_register_count=64) as (bus, slave):
        slave.input_registers[40] = 0x1234
        result = await bus.slave(1).read_input_registers(40, count=1)
    assert result == (0x1234,)
