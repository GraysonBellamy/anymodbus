"""Integration tests for :meth:`anymodbus.Slave.probe`.

The probe issues real Modbus transactions through the bus, framer, and
mock slave — these tests exercise the full stack rather than mocking the
``_txn`` boundary.
"""

from __future__ import annotations

import anyio
import pytest

from anymodbus import (
    BusConfig,
    Capability,
    FunctionCode,
    RetryPolicy,
)
from anymodbus.testing import client_slave_pair


@pytest.mark.anyio
async def test_probe_fully_supported_slave() -> None:
    """A mock slave that handles all standard FCs reports SUPPORTED for reads."""
    async with client_slave_pair() as (bus, _slave):
        caps = await bus.slave(1).probe()

    for fc in (
        FunctionCode.READ_COILS,
        FunctionCode.READ_DISCRETE_INPUTS,
        FunctionCode.READ_HOLDING_REGISTERS,
        FunctionCode.READ_INPUT_REGISTERS,
    ):
        assert caps.get(fc) is Capability.SUPPORTED, (
            f"FC {fc!r} should be SUPPORTED, got {caps.get(fc)!r}"
        )

    # Writes are never probed — they always come back UNKNOWN.
    for fc in (
        FunctionCode.WRITE_SINGLE_COIL,
        FunctionCode.WRITE_SINGLE_REGISTER,
        FunctionCode.WRITE_MULTIPLE_COILS,
        FunctionCode.WRITE_MULTIPLE_REGISTERS,
    ):
        assert caps.get(fc) is Capability.UNKNOWN, (
            f"FC {fc!r} should be UNKNOWN (not probed), got {caps.get(fc)!r}"
        )


@pytest.mark.anyio
async def test_probe_marks_disabled_fc_unsupported() -> None:
    """An ``IllegalFunctionError`` from the slave maps to UNSUPPORTED."""
    disabled = frozenset({int(FunctionCode.READ_DISCRETE_INPUTS)})
    async with client_slave_pair(disabled_function_codes=disabled) as (bus, _slave):
        caps = await bus.slave(1).probe()

    assert caps.get(FunctionCode.READ_COILS) is Capability.SUPPORTED
    assert caps.get(FunctionCode.READ_DISCRETE_INPUTS) is Capability.UNSUPPORTED
    assert caps.get(FunctionCode.READ_HOLDING_REGISTERS) is Capability.SUPPORTED
    assert caps.get(FunctionCode.READ_INPUT_REGISTERS) is Capability.SUPPORTED


@pytest.mark.anyio
async def test_probe_silent_slave_returns_unknown() -> None:
    """Timeout maps to UNKNOWN, not UNSUPPORTED.

    A slave that just doesn't answer (offline, wrong address, wrong baud) is
    indistinguishable from a slave that drops unknown FCs without responding;
    we must not downgrade silence to UNSUPPORTED.
    """
    cfg = BusConfig(request_timeout=0.05, retries=RetryPolicy(retries=0))
    async with client_slave_pair(slave_address=99, bus_config=cfg) as (bus, _slave):
        # Probe slave 1 — but the mock answers as 99, so every probe times out.
        caps = await bus.slave(1).probe()

    for fc in (
        FunctionCode.READ_COILS,
        FunctionCode.READ_DISCRETE_INPUTS,
        FunctionCode.READ_HOLDING_REGISTERS,
        FunctionCode.READ_INPUT_REGISTERS,
    ):
        assert caps.get(fc) is Capability.UNKNOWN


@pytest.mark.anyio
async def test_probe_silent_slave_short_circuits_after_first_timeout() -> None:
    """A timeout on FC1 must skip waiting for FC2/3/4 to also time out.

    With request_timeout=0.5 and retries=0, four serial timeouts would take
    ~2.0 s. After the first timeout the probe should bail and report UNKNOWN
    for the remaining FCs without further wire activity, completing in close
    to 0.5 s rather than 2.0 s.
    """
    cfg = BusConfig(request_timeout=0.5, retries=RetryPolicy(retries=0))
    async with client_slave_pair(slave_address=99, bus_config=cfg) as (bus, _slave):
        start = anyio.current_time()
        caps = await bus.slave(1).probe()
        elapsed = anyio.current_time() - start

    # If the short-circuit didn't fire, this would take ~4 * 0.5 = 2.0 s.
    # Allow generous slack for slow CI: anything well under 4 timeouts is
    # proof enough that we bailed.
    assert elapsed < 1.5, f"probe took {elapsed:.2f}s; expected short-circuit after first timeout"
    for fc in (
        FunctionCode.READ_COILS,
        FunctionCode.READ_DISCRETE_INPUTS,
        FunctionCode.READ_HOLDING_REGISTERS,
        FunctionCode.READ_INPUT_REGISTERS,
    ):
        assert caps.get(fc) is Capability.UNKNOWN


@pytest.mark.anyio
async def test_probe_walks_address_on_illegal_data_address() -> None:
    """A small register bank (which rejects address 0x40+) still reports SUPPORTED.

    The walk includes 0x0000 and 0x0001 — the slave with register_count=2
    will accept both. The probe succeeds at address 0 and reports SUPPORTED.
    To exercise the walk itself, we use register_count=1, which only covers
    address 0x0000 — every other walk address triggers IllegalDataAddress.
    The probe should still report SUPPORTED because at least one address
    succeeded.
    """
    async with client_slave_pair(register_count=1, coil_count=1) as (bus, _slave):
        caps = await bus.slave(1).probe()

    for fc in (
        FunctionCode.READ_COILS,
        FunctionCode.READ_DISCRETE_INPUTS,
        FunctionCode.READ_HOLDING_REGISTERS,
        FunctionCode.READ_INPUT_REGISTERS,
    ):
        assert caps.get(fc) is Capability.SUPPORTED


@pytest.mark.anyio
async def test_probe_caches_result_on_slave_handle() -> None:
    """After ``probe()``, ``capabilities`` returns the cached instance."""
    async with client_slave_pair() as (bus, _slave):
        slave = bus.slave(1)
        assert slave.capabilities is None
        caps = await slave.probe()
        assert slave.capabilities is caps


@pytest.mark.anyio
async def test_probe_cancellation_leaves_capabilities_unset() -> None:
    """An outer ``move_on_after`` mid-probe leaves ``_capabilities`` as None.

    We deliberately make the cache update atomic — partial probes don't
    leak into ``slave.capabilities``.
    """
    cfg = BusConfig(request_timeout=5.0, retries=RetryPolicy(retries=0))
    async with client_slave_pair(slave_address=99, bus_config=cfg) as (bus, _slave):
        slave = bus.slave(1)
        with anyio.move_on_after(0.1):
            await slave.probe()
        assert slave.capabilities is None
