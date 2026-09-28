"""Wire a :class:`Bus` to mock slaves over an in-process serial pair.

:func:`client_slave_pair` builds one :class:`MockSlave`; :func:`client_server_pair`
serves slaves you build yourself (subclasses included) through a
:class:`MockServer`. Both build on :func:`anyserial.testing.serial_port_pair`,
whose ends are real :class:`anyserial.SerialPort` s, so the test setup looks
identical to real RTU traffic on the wire — bytes flow through a backed mock
fd, the framer runs unchanged, the bus sees the configured baud rate and uses
the port's drain and input reset, and timing-dependent code paths can be
exercised.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from anymodbus._mock.server import MockServer
from anymodbus._mock.slave import MockSlave
from anymodbus._types import Framing
from anymodbus.bus import Bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from anymodbus._mock.faults import FaultPlan
    from anymodbus._mock.slave import QuantityLimits
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
    framing: Framing = Framing.RTU,
    limits: QuantityLimits | None = None,
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
        framing: Wire framing for *both* ends — :attr:`Framing.RTU` (default)
            or :attr:`Framing.ASCII`. One register bank backs either framing.
        limits: Optional per-request quantity caps for the mock slave.

    Yields:
        ``(bus, mock_slave)``. The bus is fully wired and ready for I/O; the
        mock_slave's register banks may be mutated mid-test.
    """
    cfg = SerialConfig(baudrate=baudrate)
    client_end, slave_end = serial_port_pair(config_a=cfg, config_b=cfg)
    bus = Bus(client_end, config=bus_config, framing=framing)
    slave = MockSlave(
        address=slave_address,
        register_count=register_count,
        coil_count=coil_count,
        discrete_input_count=discrete_input_count,
        input_register_count=input_register_count,
        faults=faults,
        disabled_function_codes=disabled_function_codes,
        framing=framing,
        limits=limits,
    )
    try:
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(slave.serve, slave_end)
            try:
                yield bus, slave
            finally:
                tg.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            await bus.aclose()
            await slave_end.aclose()


@asynccontextmanager
async def client_server_pair(
    *slaves: MockSlave,
    bus_config: BusConfig | None = None,
    baudrate: int = _DEFAULT_TEST_BAUD,
    framing: Framing = Framing.RTU,
    on_request: Callable[[int, bytes], None] | None = None,
) -> AsyncGenerator[tuple[Bus, MockServer]]:
    """Yield ``(bus, server)``: a bus and a :class:`MockServer` for ``slaves`` on one line.

    Like :func:`client_slave_pair`, but for slaves built by the caller —
    several on one line, or subclasses that override :meth:`MockSlave.handle`.
    Each slave must use ``framing``.

    Args:
        *slaves: The mock slaves to serve, each with a distinct address.
        bus_config: Optional :class:`BusConfig` for the bus side.
        baudrate: Serial baudrate applied to both ends.
        framing: Wire framing for both ends.
        on_request: Passed to :class:`MockServer`: called with
            ``(address, request_pdu)`` for every checksum-valid request.

    Yields:
        ``(bus, server)``. The slaves are reachable as ``server.slaves``.
    """
    server = MockServer(*slaves, framing=framing, on_request=on_request)
    cfg = SerialConfig(baudrate=baudrate)
    client_end, server_end = serial_port_pair(config_a=cfg, config_b=cfg)
    bus = Bus(client_end, config=bus_config, framing=framing)
    try:
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(server.serve, server_end)
            try:
                yield bus, server
            finally:
                tg.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            await bus.aclose()
            await server_end.aclose()


__all__ = ["client_server_pair", "client_slave_pair"]
