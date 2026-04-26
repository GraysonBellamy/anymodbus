"""End-to-end tests for the sync wrapper.

The sync API blocks the calling thread, so the test setup runs the
:class:`MockSlave` on the same shared portal that :class:`anymodbus.sync.Bus`
dispatches to. Both sides live on one event loop in one background thread —
the slave's ``serve`` coroutine and the bus's request tasks interleave
through the standard async cooperative scheduling.

We deliberately do **not** parametrize ``anyio_backend`` here: the sync
wrapper is itself the bridge between sync code and AnyIO, and the portal
backend is set process-wide.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest
from anyserial import SerialConfig
from anyserial.sync import (
    _get_provider as _anyserial_get_provider,  # pyright: ignore[reportPrivateUsage]
)
from anyserial.testing import serial_port_pair

from anymodbus import (
    BusConfig,
    ConfigurationError,
    FrameTimeoutError,
    RetryPolicy,
)
from anymodbus.bus import Bus as AsyncBus
from anymodbus.sync import Bus as SyncBus
from anymodbus.sync import open_modbus_rtu
from anymodbus.testing import MockSlave

if TYPE_CHECKING:
    from collections.abc import Iterator

    from anyserial import SerialPort


@pytest.fixture
def sync_pair() -> Iterator[tuple[SyncBus, MockSlave]]:
    """Yield ``(sync_bus, mock_slave)`` connected over an in-process serial pair.

    Everything — the pair, the slave's serve loop, and the wrapped async bus
    — lives on the shared anyserial portal so cross-loop coupling can't bite.
    The slave runs as a long-lived task; sync bus calls dispatch as
    short-lived tasks alongside it.

    Refcount discipline: the fixture enters the provider context twice — one
    ref handed to the :class:`SyncBus`, one held for cleanup. This way
    ``slave_end.aclose`` always has a live portal to dispatch through, even
    if the test body explicitly closed the bus.
    """
    provider = _anyserial_get_provider()
    portal_for_bus = provider.__enter__()
    portal_for_cleanup = provider.__enter__()
    assert portal_for_bus is portal_for_cleanup  # singleton portal

    slave = MockSlave(address=1)
    cfg = SerialConfig(baudrate=19_200)
    client_end, slave_end = serial_port_pair(config_a=cfg, config_b=cfg)
    async_bus = AsyncBus(
        client_end,
        config=BusConfig(request_timeout=1.0, retries=RetryPolicy(retries=0)),
    )
    serve_future = portal_for_bus.start_task_soon(slave.serve, slave_end)
    sync_bus = SyncBus(async_bus, portal=portal_for_bus, provider=provider)
    try:
        yield sync_bus, slave
    finally:
        serve_future.cancel()

        async def _close_slave_end(end: SerialPort) -> None:
            with contextlib.suppress(Exception):
                await end.aclose()

        with contextlib.suppress(Exception):
            portal_for_cleanup.call(_close_slave_end, slave_end)
        sync_bus.close()
        provider.__exit__(None, None, None)


def test_sync_read_holding_registers_roundtrip(
    sync_pair: tuple[SyncBus, MockSlave],
) -> None:
    bus, slave = sync_pair
    slave.holding_registers[0:4] = [0x1234, 0xABCD, 0x0000, 0xFFFF]
    result = bus.slave(1).read_holding_registers(0, count=4)
    assert result == (0x1234, 0xABCD, 0x0000, 0xFFFF)


def test_sync_write_register_persists(
    sync_pair: tuple[SyncBus, MockSlave],
) -> None:
    bus, slave = sync_pair
    bus.slave(1).write_register(5, 0xCAFE)
    assert slave.holding_registers[5] == 0xCAFE


def test_sync_write_coils_persists(
    sync_pair: tuple[SyncBus, MockSlave],
) -> None:
    bus, slave = sync_pair
    bus.slave(1).write_coils(0, [True, False, True, True])
    assert slave.coils[0] == 0b0000_1101


def test_sync_read_float_roundtrip(
    sync_pair: tuple[SyncBus, MockSlave],
) -> None:
    bus, _slave = sync_pair
    bus.slave(1).write_float(10, 78.295)
    value = bus.slave(1).read_float(10)
    assert value == pytest.approx(78.295, rel=1e-6)


def test_sync_broadcast_write_register(
    sync_pair: tuple[SyncBus, MockSlave],
) -> None:
    bus, slave = sync_pair
    bus.broadcast_write_register(7, 0xBEEF)
    # Use a follow-up unicast as a barrier — the bus held the broadcast
    # turnaround delay before releasing the lock, so by the time this
    # returns the slave has applied the broadcast write.
    bus.slave(1).read_holding_registers(0, count=1)
    assert slave.holding_registers[7] == 0xBEEF


def test_sync_per_call_timeout_raises_timeout_error(
    sync_pair: tuple[SyncBus, MockSlave],
) -> None:
    """A short ``timeout=`` on a sync call surfaces as :class:`TimeoutError`.

    :class:`anymodbus.FrameTimeoutError` inherits from :class:`TimeoutError`,
    so callers can catch either; the wrapper's ``anyio.fail_after`` raises
    the bare ``TimeoutError`` from anyio's expiry path.
    """
    bus, _slave = sync_pair
    # Ask for an unmapped slave address so we never get a response. The bus
    # would hit its own request_timeout after 1.0s, but the per-call timeout
    # of 50ms fires first.
    with pytest.raises(TimeoutError):
        bus.slave(2).read_holding_registers(0, count=1, timeout=0.05)


def test_sync_close_is_idempotent(
    sync_pair: tuple[SyncBus, MockSlave],
) -> None:
    bus, _slave = sync_pair
    bus.close()
    bus.close()  # second close must not raise
    assert not bus.is_open


def test_sync_slave_address_validation_rejects_broadcast(
    sync_pair: tuple[SyncBus, MockSlave],
) -> None:
    bus, _slave = sync_pair
    with pytest.raises(ConfigurationError):
        bus.slave(0)


def test_sync_open_modbus_rtu_rejects_bad_path() -> None:
    """``open_modbus_rtu`` propagates errors from the underlying open."""
    with pytest.raises((OSError, ConfigurationError)):
        open_modbus_rtu(
            "/dev/definitely-does-not-exist-anymodbus",
            baudrate=19_200,
            parity="even",
            timeout=1.0,
        )


def test_sync_uses_shared_anyserial_portal() -> None:
    """The sync wrapper must reuse anyserial's process-wide portal singleton.

    Per DESIGN.md §5.4, opening a sync ``anymodbus.Bus`` while a sync
    ``anyserial.SerialPort`` is already open in the same process must NOT
    spawn a second event-loop thread. Verify by checking that the resolver
    function imported by ``anymodbus.sync`` is the same callable that
    ``anyserial.sync`` exposes.
    """
    from anymodbus import sync as anymodbus_sync  # noqa: PLC0415

    am_get_provider = getattr(anymodbus_sync, "_anyserial_get_provider")  # noqa: B009
    assert am_get_provider is _anyserial_get_provider
    # Both calls return the same singleton instance.
    assert am_get_provider() is _anyserial_get_provider()


def test_sync_frame_timeout_error_is_a_timeout_error() -> None:
    """Sanity: bare ``except TimeoutError`` catches FrameTimeoutError.

    The per-call ``timeout=`` raises bare :class:`TimeoutError` from anyio;
    the underlying ``Bus.request_timeout`` raises :class:`FrameTimeoutError`.
    Both should be catchable as ``TimeoutError`` so sync callers don't need
    to import anymodbus exception classes for the common timeout case.
    """
    assert issubclass(FrameTimeoutError, TimeoutError)
