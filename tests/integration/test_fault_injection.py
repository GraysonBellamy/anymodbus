"""Fault-injection integration tests for the :class:`MockSlave` + :class:`FaultPlan` pair.

Each :class:`FaultPlan` field has its own test, plus a couple of composite
scenarios that verify retry-policy interaction. Faults are scheduled at
``MockSlave`` construction time and apply per-response based on the slave's
zero-indexed response counter — ``corrupt_crc_after_n=0`` corrupts the very
first response, ``=1`` corrupts the second, and so on.
"""

from __future__ import annotations

import anyio
import pytest

from anymodbus import (
    BusConfig,
    CRCError,
    FrameTimeoutError,
    RetryPolicy,
)
from anymodbus.testing import FaultPlan, client_slave_pair


@pytest.mark.anyio
async def test_corrupt_crc_surfaces_as_crc_error_with_no_retry() -> None:
    """A bad CRC on the first (and only) attempt → :class:`CRCError`."""
    cfg = BusConfig(retries=RetryPolicy(retries=0))
    plan = FaultPlan(corrupt_crc_after_n=0)
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, slave):
        slave.holding_registers[0] = 0xDEAD
        with pytest.raises(CRCError):
            await bus.slave(1).read_holding_registers(0, count=1)


@pytest.mark.anyio
async def test_corrupt_crc_recovers_via_retry() -> None:
    """``CRCError`` on first attempt + retry → second attempt's good CRC succeeds."""
    cfg = BusConfig(retries=RetryPolicy(retries=1))
    plan = FaultPlan(corrupt_crc_after_n=0)  # first response only
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, slave):
        slave.holding_registers[0] = 0xCAFE
        result = await bus.slave(1).read_holding_registers(0, count=1)
    assert result == (0xCAFE,)


@pytest.mark.anyio
async def test_corrupt_crc_only_affects_indexed_response() -> None:
    """``corrupt_crc_after_n=1`` → response #0 is good, response #1 corrupt."""
    cfg = BusConfig(retries=RetryPolicy(retries=0))
    plan = FaultPlan(corrupt_crc_after_n=1)
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, slave):
        slave.holding_registers[0] = 0x0001
        slave.holding_registers[1] = 0x0002
        # First call must succeed.
        first = await bus.slave(1).read_holding_registers(0, count=1)
        assert first == (0x0001,)
        # Second call's response has corrupted CRC.
        with pytest.raises(CRCError):
            await bus.slave(1).read_holding_registers(1, count=1)


@pytest.mark.anyio
async def test_delay_response_exceeding_timeout_raises_frame_timeout() -> None:
    """A response delayed past ``request_timeout`` → :class:`FrameTimeoutError`."""
    cfg = BusConfig(request_timeout=0.05, retries=RetryPolicy(retries=0))
    plan = FaultPlan(delay_response_seconds=0.5)
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, _slave):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=1)


@pytest.mark.anyio
async def test_delay_response_within_timeout_still_succeeds() -> None:
    """A short delay under ``request_timeout`` returns normally."""
    cfg = BusConfig(request_timeout=1.0, retries=RetryPolicy(retries=0))
    plan = FaultPlan(delay_response_seconds=0.05)
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, slave):
        slave.holding_registers[0] = 0x1234
        start = anyio.current_time()
        result = await bus.slave(1).read_holding_registers(0, count=1)
        elapsed = anyio.current_time() - start
    assert result == (0x1234,)
    assert elapsed >= 0.05


@pytest.mark.anyio
async def test_wrong_slave_address_keeps_waiting_until_timeout() -> None:
    """A reply from the wrong slave is drained; the deadline still bounds the wait.

    Per *serial §2.4.1* the master keeps listening through the response timeout
    when an unexpected slave answers. With no further reply from the right
    slave, the deadline eventually fires.
    """
    cfg = BusConfig(request_timeout=0.1, retries=RetryPolicy(retries=0))
    plan = FaultPlan(wrong_slave_address=99)
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, _slave):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=1)


@pytest.mark.anyio
async def test_drop_response_raises_frame_timeout() -> None:
    """``drop_response_after_n=0`` → first response dropped → timeout."""
    cfg = BusConfig(request_timeout=0.05, retries=RetryPolicy(retries=0))
    plan = FaultPlan(drop_response_after_n=0)
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, _slave):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=1)


@pytest.mark.anyio
async def test_drop_response_recovers_via_retry() -> None:
    """First response dropped, retry succeeds."""
    cfg = BusConfig(request_timeout=0.1, retries=RetryPolicy(retries=1))
    plan = FaultPlan(drop_response_after_n=0)
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, slave):
        slave.holding_registers[0] = 0xBEEF
        result = await bus.slave(1).read_holding_registers(0, count=1)
    assert result == (0xBEEF,)


@pytest.mark.anyio
async def test_write_not_retried_under_default_policy_after_dropped_response() -> None:
    """A dropped response on a non-idempotent FC must NOT silently retry.

    The whole point of ``retry_idempotent_only=True`` is to avoid double-firing
    a write whose response was lost — so this test asserts the timeout
    surfaces despite ``retries=3`` being configured.
    """
    cfg = BusConfig(
        request_timeout=0.05,
        retries=RetryPolicy(retries=3, retry_idempotent_only=True),
    )
    plan = FaultPlan(drop_response_after_n=0)
    async with client_slave_pair(faults=plan, bus_config=cfg) as (bus, slave):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).write_register(0, 0x1234)
        # The write itself reached the slave (the response was the dropped part).
        assert slave.holding_registers[0] == 0x1234
