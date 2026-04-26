"""``client_slave_pair`` — wire a :class:`Bus` to a :class:`MockSlave` over a serial pair.

Builds on :func:`anyserial.testing.serial_port_pair` so the test setup looks
identical to real RTU traffic on the wire — bytes flow through a backed
mock fd, the framer runs unchanged, and timing-dependent code paths can be
exercised.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from anymodbus._mock.slave import MockSlave
from anymodbus.bus import Bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from anymodbus._mock.faults import FaultPlan
    from anymodbus.config import BusConfig

# Reference baud — picks up the spec-floor timing path (1.75 ms / 0.75 ms)
# without driving real bit-rate latency. Tests that need a different baud
# can build their own pair from anyserial.testing directly.
_DEFAULT_TEST_BAUD = 19_200


@asynccontextmanager
async def client_slave_pair(
    *,
    slave_address: int = 1,
    register_count: int = 256,
    coil_count: int = 256,
    discrete_input_count: int | None = None,
    input_register_count: int | None = None,
    faults: FaultPlan | None = None,
    disabled_function_codes: frozenset[int] | None = None,
    bus_config: BusConfig | None = None,
    baudrate: int = _DEFAULT_TEST_BAUD,
) -> AsyncGenerator[tuple[Bus, MockSlave]]:
    """Yield ``(bus, mock_slave)`` connected over an in-process serial pair.

    The :class:`MockSlave` runs in a background task in its own task group;
    on context exit, the slave task is cancelled and both ends of the
    underlying serial pair are closed.

    Args:
        slave_address: Modbus address the mock slave responds to. Defaults to 1.
        register_count: Size of the holding register bank (and the default
            size of the input register bank).
        coil_count: Size of the coils bit bank (and the default size of the
            discrete-inputs bank).
        discrete_input_count: Optional independent size for the
            discrete-inputs bank. Defaults to ``coil_count``.
        input_register_count: Optional independent size for the input
            register bank. Defaults to ``register_count``.
        faults: Optional :class:`FaultPlan` for the mock slave.
        disabled_function_codes: FCs the mock slave should refuse with
            :class:`anymodbus.IllegalFunctionError`. Useful for capability-
            probe tests that need a slave with specific gaps.
        bus_config: Optional :class:`BusConfig` for the bus side.
        baudrate: Serial baudrate applied to both ends. Affects auto-resolved
            inter-frame timing.

    Yields:
        ``(bus, mock_slave)``. The bus is fully wired and ready for I/O; the
        mock_slave's register banks may be mutated mid-test.
    """
    cfg = SerialConfig(baudrate=baudrate)
    client_end, slave_end = serial_port_pair(config_a=cfg, config_b=cfg)
    bus = Bus(client_end, config=bus_config)
    slave = MockSlave(
        address=slave_address,
        register_count=register_count,
        coil_count=coil_count,
        discrete_input_count=discrete_input_count,
        input_register_count=input_register_count,
        faults=faults,
        disabled_function_codes=disabled_function_codes,
    )
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(slave.serve, slave_end)
            try:
                yield bus, slave
            finally:
                tg.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            await bus.aclose()
            await slave_end.aclose()


__all__ = ["client_slave_pair"]
