"""Blocking wrapper around :class:`anymodbus.Bus`.

Reuses :mod:`anyserial.sync`'s process-wide
:class:`anyio.from_thread.BlockingPortalProvider` so opening a sync
``anymodbus.Bus`` does not spawn a second event-loop thread on top of any
existing sync ``anyserial`` ports the process already has open. See
:doc:`DESIGN` §5.4.

Every blocking method accepts an optional ``timeout`` keyword that wraps the
underlying async call in :func:`anyio.fail_after` on the portal thread;
expiry surfaces as the stdlib :class:`TimeoutError` (which
:class:`anymodbus.FrameTimeoutError` already inherits from).
"""

from __future__ import annotations

import contextlib
import logging
import warnings
from typing import TYPE_CHECKING, Any, Self

import anyio
import anyio.from_thread

# Sharing anyserial's portal singleton is the explicit DESIGN.md §5.4
# contract: opening a sync anymodbus.Bus while a sync anyserial.SerialPort
# is already open must reuse the existing event-loop thread, not spawn a
# second one. ``_get_provider`` is underscore-prefixed but ships in
# anyserial's public ``sync`` module for exactly this purpose.
from anyserial.sync import (
    _get_provider as _anyserial_get_provider,  # pyright: ignore[reportPrivateUsage]
)
from anyserial.sync import configure_portal

from anymodbus.stream import open_modbus_rtu as _async_open_modbus_rtu

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from types import TracebackType

    from anymodbus._types import ByteOrder, WordOrder
    from anymodbus.bus import Bus as _AsyncBus
    from anymodbus.config import BusConfig
    from anymodbus.slave import Slave as _AsyncSlave
    from anymodbus.stream import ParityLiteral

_LOGGER = logging.getLogger("anymodbus.sync")


def _call_with_timeout[T](
    portal: anyio.from_thread.BlockingPortal,
    afn: Callable[..., Awaitable[T]],
    timeout: float | None,
    *args: Any,
) -> T:
    """Run ``afn(*args)`` on ``portal``, bounded by ``timeout`` if not None."""
    if timeout is None:
        return portal.call(afn, *args)

    async def _bounded() -> T:
        with anyio.fail_after(timeout):
            return await afn(*args)

    return portal.call(_bounded)


class Bus:
    """Blocking wrapper around :class:`anymodbus.Bus`.

    Methods mirror the async API but accept an optional ``timeout=`` keyword
    that wraps each call in :func:`anyio.fail_after` on the portal thread;
    expiry surfaces as the stdlib :class:`TimeoutError`.

    Construction is via :func:`open_modbus_rtu` or, for tests that already
    hold an async :class:`anymodbus.Bus`, by passing it as ``async_bus=``.
    """

    __slots__ = (
        "_async_bus",
        "_closed",
        "_portal",
        "_portal_entered",
        "_provider",
    )

    def __init__(
        self,
        async_bus: _AsyncBus,
        *,
        portal: anyio.from_thread.BlockingPortal,
        provider: anyio.from_thread.BlockingPortalProvider,
    ) -> None:
        """Wrap an already-built async bus. Prefer :func:`open_modbus_rtu`.

        The ``provider`` context is assumed already entered once on behalf
        of this bus; :meth:`close` releases that reference.
        """
        self._async_bus: _AsyncBus = async_bus
        self._portal: anyio.from_thread.BlockingPortal = portal
        self._provider: anyio.from_thread.BlockingPortalProvider = provider
        self._portal_entered: bool = True
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Properties — direct delegation, no portal
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """Whether the bus is usable for I/O."""
        return not self._closed and self._async_bus.is_open

    @property
    def config(self) -> BusConfig:
        """Active :class:`BusConfig`."""
        return self._async_bus.config

    # ------------------------------------------------------------------
    # Construction of slave handles
    # ------------------------------------------------------------------

    def slave(self, address: int) -> Slave:
        """Return a sync :class:`Slave` handle bound to ``address``.

        Address validation (1-247 for unicast) happens inside the underlying
        async :class:`anymodbus.Slave` constructor; address 0 raises
        :class:`anymodbus.ConfigurationError`.
        """
        async_slave = self._async_bus.slave(address)
        return Slave(async_slave, portal=self._portal)

    # ------------------------------------------------------------------
    # Broadcast methods — sync mirrors of Bus.broadcast_*
    # ------------------------------------------------------------------

    def broadcast_write_coil(self, address: int, *, on: bool, timeout: float | None = None) -> None:
        """FC 0x05 — Broadcast Write Single Coil to slave address 0."""

        async def _call() -> None:
            await self._async_bus.broadcast_write_coil(address, on=on)

        _call_with_timeout(self._portal, _call, timeout)

    def broadcast_write_register(
        self, address: int, value: int, *, timeout: float | None = None
    ) -> None:
        """FC 0x06 — Broadcast Write Single Register to slave address 0."""
        _call_with_timeout(
            self._portal, self._async_bus.broadcast_write_register, timeout, address, value
        )

    def broadcast_write_coils(
        self,
        address: int,
        values: Sequence[bool],
        *,
        timeout: float | None = None,
    ) -> None:
        """FC 0x0F — Broadcast Write Multiple Coils to slave address 0."""
        _call_with_timeout(
            self._portal, self._async_bus.broadcast_write_coils, timeout, address, values
        )

    def broadcast_write_registers(
        self,
        address: int,
        values: Sequence[int],
        *,
        timeout: float | None = None,
    ) -> None:
        """FC 0x10 — Broadcast Write Multiple Registers to slave address 0."""
        _call_with_timeout(
            self._portal,
            self._async_bus.broadcast_write_registers,
            timeout,
            address,
            values,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self, *, timeout: float | None = None) -> None:
        """Close the bus and the underlying stream. Idempotent.

        Releases this bus's reference on the shared portal; the event-loop
        thread shuts down when the last sync port / bus is closed.
        """
        if self._closed:
            return
        self._closed = True
        try:
            _call_with_timeout(self._portal, self._async_bus.aclose, timeout)
        finally:
            self._release_portal()

    def _release_portal(self) -> None:
        if not self._portal_entered:
            return
        self._portal_entered = False
        try:
            self._provider.__exit__(None, None, None)
        except RuntimeError as exc:
            # Portal teardown may legitimately race with shutdown of the
            # event-loop thread when several sync resources release in
            # parallel; we still want close() to succeed for the caller.
            # Anything else is unexpected and should bubble up.
            _LOGGER.warning("error releasing sync portal: %s", exc)

    def __enter__(self) -> Self:
        """Return ``self`` so ``with`` expressions can bind the bus."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the bus on exit from the ``with`` block."""
        self.close()

    def __del__(self) -> None:
        """Emit :class:`ResourceWarning` if the bus was leaked open."""
        if getattr(self, "_closed", True):
            return
        warnings.warn(
            "unclosed sync Modbus bus; use `with` or call `bus.close()`",
            ResourceWarning,
            stacklevel=2,
        )
        with contextlib.suppress(Exception):
            self.close()


class Slave:
    """Blocking per-slave handle. Methods mirror async :class:`anymodbus.Slave`.

    Each method shares the parent :class:`Bus`'s portal — constructing many
    sync slave handles on one bus is cheap.
    """

    __slots__ = ("_async_slave", "_portal")

    def __init__(
        self,
        async_slave: _AsyncSlave,
        *,
        portal: anyio.from_thread.BlockingPortal,
    ) -> None:
        self._async_slave: _AsyncSlave = async_slave
        self._portal: anyio.from_thread.BlockingPortal = portal

    @property
    def address(self) -> int:
        """The Modbus unit address this slave handle was created with."""
        return self._async_slave.address

    # ------------------------------------------------------------------
    # Standard read function codes
    # ------------------------------------------------------------------

    def read_coils(
        self, address: int, *, count: int, timeout: float | None = None
    ) -> tuple[bool, ...]:
        """Sync FC 0x01 — Read ``count`` coils starting at ``address``."""

        async def _call() -> tuple[bool, ...]:
            return await self._async_slave.read_coils(address, count=count)

        return _call_with_timeout(self._portal, _call, timeout)

    def read_discrete_inputs(
        self, address: int, *, count: int, timeout: float | None = None
    ) -> tuple[bool, ...]:
        """Sync FC 0x02 — Read ``count`` discrete inputs starting at ``address``."""

        async def _call() -> tuple[bool, ...]:
            return await self._async_slave.read_discrete_inputs(address, count=count)

        return _call_with_timeout(self._portal, _call, timeout)

    def read_holding_registers(
        self, address: int, *, count: int, timeout: float | None = None
    ) -> tuple[int, ...]:
        """Sync FC 0x03 — Read ``count`` holding registers starting at ``address``."""

        async def _call() -> tuple[int, ...]:
            return await self._async_slave.read_holding_registers(address, count=count)

        return _call_with_timeout(self._portal, _call, timeout)

    def read_input_registers(
        self, address: int, *, count: int, timeout: float | None = None
    ) -> tuple[int, ...]:
        """Sync FC 0x04 — Read ``count`` input registers starting at ``address``."""

        async def _call() -> tuple[int, ...]:
            return await self._async_slave.read_input_registers(address, count=count)

        return _call_with_timeout(self._portal, _call, timeout)

    # ------------------------------------------------------------------
    # Standard write function codes
    # ------------------------------------------------------------------

    def write_coil(self, address: int, *, on: bool, timeout: float | None = None) -> None:
        """Sync FC 0x05 — Write a single coil. The slave's echo is verified."""

        async def _call() -> None:
            await self._async_slave.write_coil(address, on=on)

        _call_with_timeout(self._portal, _call, timeout)

    def write_register(self, address: int, value: int, *, timeout: float | None = None) -> None:
        """Sync FC 0x06 — Write a single holding register."""
        _call_with_timeout(self._portal, self._async_slave.write_register, timeout, address, value)

    def write_coils(
        self,
        address: int,
        values: Sequence[bool],
        *,
        timeout: float | None = None,
    ) -> None:
        """Sync FC 0x0F — Write multiple coils."""
        _call_with_timeout(self._portal, self._async_slave.write_coils, timeout, address, values)

    def write_registers(
        self,
        address: int,
        values: Sequence[int],
        *,
        timeout: float | None = None,
    ) -> None:
        """Sync FC 0x10 — Write multiple holding registers."""
        _call_with_timeout(
            self._portal, self._async_slave.write_registers, timeout, address, values
        )

    # ------------------------------------------------------------------
    # Higher-level helpers — float / int32 / string with explicit ordering
    # ------------------------------------------------------------------

    def read_float(
        self,
        address: int,
        *,
        word_order: WordOrder | None = None,
        byte_order: ByteOrder | None = None,
        timeout: float | None = None,
    ) -> float:
        """Sync read_float.

        ``None`` for ordering keeps the async defaults (``HIGH_LOW`` / ``BIG``).
        """
        kwargs: dict[str, Any] = {}
        if word_order is not None:
            kwargs["word_order"] = word_order
        if byte_order is not None:
            kwargs["byte_order"] = byte_order

        async def _call() -> float:
            return await self._async_slave.read_float(address, **kwargs)

        return _call_with_timeout(self._portal, _call, timeout)

    def write_float(
        self,
        address: int,
        value: float,
        *,
        word_order: WordOrder | None = None,
        byte_order: ByteOrder | None = None,
        timeout: float | None = None,
    ) -> None:
        """Sync write_float."""
        kwargs: dict[str, Any] = {}
        if word_order is not None:
            kwargs["word_order"] = word_order
        if byte_order is not None:
            kwargs["byte_order"] = byte_order

        async def _call() -> None:
            await self._async_slave.write_float(address, value, **kwargs)

        _call_with_timeout(self._portal, _call, timeout)

    def read_int32(
        self,
        address: int,
        *,
        signed: bool = True,
        word_order: WordOrder | None = None,
        byte_order: ByteOrder | None = None,
        timeout: float | None = None,
    ) -> int:
        """Sync read_int32."""
        kwargs: dict[str, Any] = {"signed": signed}
        if word_order is not None:
            kwargs["word_order"] = word_order
        if byte_order is not None:
            kwargs["byte_order"] = byte_order

        async def _call() -> int:
            return await self._async_slave.read_int32(address, **kwargs)

        return _call_with_timeout(self._portal, _call, timeout)

    def write_int32(
        self,
        address: int,
        value: int,
        *,
        signed: bool = True,
        word_order: WordOrder | None = None,
        byte_order: ByteOrder | None = None,
        timeout: float | None = None,
    ) -> None:
        """Sync write_int32."""
        kwargs: dict[str, Any] = {"signed": signed}
        if word_order is not None:
            kwargs["word_order"] = word_order
        if byte_order is not None:
            kwargs["byte_order"] = byte_order

        async def _call() -> None:
            await self._async_slave.write_int32(address, value, **kwargs)

        _call_with_timeout(self._portal, _call, timeout)

    def read_string(
        self,
        address: int,
        *,
        register_count: int | None = None,
        byte_count: int | None = None,
        byte_order: ByteOrder | None = None,
        encoding: str = "ascii",
        strip_null: bool = True,
        timeout: float | None = None,
    ) -> str:
        """Sync read_string. Supply ``register_count`` *or* ``byte_count``."""
        kwargs: dict[str, Any] = {
            "encoding": encoding,
            "strip_null": strip_null,
        }
        if register_count is not None:
            kwargs["register_count"] = register_count
        if byte_count is not None:
            kwargs["byte_count"] = byte_count
        if byte_order is not None:
            kwargs["byte_order"] = byte_order

        async def _call() -> str:
            return await self._async_slave.read_string(address, **kwargs)

        return _call_with_timeout(self._portal, _call, timeout)

    def write_string(
        self,
        address: int,
        value: str,
        *,
        register_count: int | None = None,
        byte_count: int | None = None,
        byte_order: ByteOrder | None = None,
        encoding: str = "ascii",
        pad: bytes = b"\x00",
        timeout: float | None = None,
    ) -> None:
        """Sync write_string. Supply ``register_count`` *or* ``byte_count``."""
        kwargs: dict[str, Any] = {
            "encoding": encoding,
            "pad": pad,
        }
        if register_count is not None:
            kwargs["register_count"] = register_count
        if byte_count is not None:
            kwargs["byte_count"] = byte_count
        if byte_order is not None:
            kwargs["byte_order"] = byte_order

        async def _call() -> None:
            await self._async_slave.write_string(address, value, **kwargs)

        _call_with_timeout(self._portal, _call, timeout)


def open_modbus_rtu(
    path: str,
    *,
    baudrate: int,
    parity: ParityLiteral,
    config: BusConfig | None = None,
    timeout: float | None = None,
) -> Bus:
    """Open a serial port and return a blocking :class:`Bus`.

    Counterpart to :func:`anymodbus.open_modbus_rtu`. See that function for
    parameter documentation. ``timeout`` bounds the open operation itself —
    important for flaky USB-RS485 adapters that can hang on enumeration.
    """
    provider = _anyserial_get_provider()
    portal = provider.__enter__()
    try:

        async def _open() -> _AsyncBus:
            return await _async_open_modbus_rtu(
                path, baudrate=baudrate, parity=parity, config=config
            )

        async_bus = _call_with_timeout(portal, _open, timeout)
    except BaseException:
        provider.__exit__(None, None, None)
        raise
    return Bus(async_bus, portal=portal, provider=provider)


__all__ = ["Bus", "Slave", "configure_portal", "open_modbus_rtu"]
