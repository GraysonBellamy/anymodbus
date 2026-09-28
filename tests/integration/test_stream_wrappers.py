"""Streams that wrap a serial port: optional capabilities and port failures.

The bus uses a stream's ``drain()`` and ``reset_input_buffer()`` whenever the
stream has them as async methods, whatever its type, so a wrapper that
forwards them keeps drain-after-send and the input reset. And every failure
the stream raises reaches the caller as a :class:`ModbusError`: a disconnect
as :class:`ConnectionLostError`, a close as :class:`BusClosedError`, and any
other ``OSError`` (including a plain :class:`anyserial.SerialError`) as
:class:`TransportError`, which is still an ``OSError``.
"""

from __future__ import annotations

import errno
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
import pytest
from anyserial import (
    SerialClosedError,
    SerialConfig,
    SerialDisconnectedError,
    SerialError,
    SerialPort,
)
from anyserial.testing import serial_port_pair

from anymodbus import (
    Bus,
    BusClosedError,
    BusConfig,
    ConnectionLostError,
    FrameTimeoutError,
    ModbusError,
    RetryPolicy,
    TimingConfig,
    TransactionInfo,
    TransactionOutcome,
    TransportError,
)
from anymodbus.stream import SupportsDrain, SupportsResetInputBuffer
from anymodbus.testing import FaultPlan, MockServer, MockSlave

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

_IDLE = 0.05


class _PlainWrapper(anyio.abc.ByteStream):
    """Forwards send / receive / close to a serial port; nothing else."""

    def __init__(self, inner: SerialPort, *, forward_attributes: bool = True) -> None:
        self.inner = inner
        self.calls: list[str] = []
        self.fail: dict[str, BaseException] = {}
        self._forward_attributes = forward_attributes

    def _enter(self, name: str, *, record: bool = True) -> None:
        if record:
            self.calls.append(name)
        exc = self.fail.pop(name, None)
        if exc is not None:
            raise exc

    async def send(self, item: bytes) -> None:
        self._enter("send")
        await self.inner.send(item)

    async def receive(self, max_bytes: int = 65536) -> bytes:
        self._enter("receive", record=False)
        return await self.inner.receive(max_bytes)

    async def send_eof(self) -> None:
        await self.inner.send_eof()

    async def aclose(self) -> None:
        await self.inner.aclose()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return self.inner.extra_attributes if self._forward_attributes else {}


class _Wrapper(_PlainWrapper):
    """Also forwards the serial port's ``drain`` and ``reset_input_buffer``."""

    async def drain(self) -> None:
        self._enter("drain")
        await self.inner.drain()

    async def reset_input_buffer(self) -> None:
        self._enter("reset_input_buffer")
        await self.inner.reset_input_buffer()


class _SyncDrainWrapper(_PlainWrapper):
    """Has a ``drain`` attribute, but not a coroutine: the bus must not await it."""

    def drain(self) -> None:
        self.calls.append("drain")


@asynccontextmanager
async def _line(
    wrap: Callable[[SerialPort], _PlainWrapper],
    *,
    config: BusConfig | None = None,
    slave: MockSlave | None = None,
    baudrate: int = 19_200,
) -> AsyncGenerator[tuple[Bus, _PlainWrapper, list[TransactionInfo]]]:
    serial_config = SerialConfig(baudrate=baudrate)
    client_end, server_end = serial_port_pair(config_a=serial_config, config_b=serial_config)
    stream = wrap(client_end)
    events: list[TransactionInfo] = []
    bus = Bus(stream, config=config, on_transaction=events.append)
    server = MockServer(slave if slave is not None else MockSlave(address=1))
    async with bus, server_end, anyio.create_task_group() as tg:
        _ = tg.start_soon(server.serve, server_end)
        yield bus, stream, events
        tg.cancel()


# ---------------------------------------------------------------------------
# Capabilities.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_wrapper_satisfies_the_protocols() -> None:
    client_end, server_end = serial_port_pair()
    async with client_end, server_end:
        assert isinstance(client_end, SupportsDrain)
        assert isinstance(_Wrapper(client_end), SupportsDrain)
        assert isinstance(_Wrapper(client_end), SupportsResetInputBuffer)
        assert not isinstance(_PlainWrapper(client_end), SupportsDrain)
        assert not isinstance(_PlainWrapper(client_end), SupportsResetInputBuffer)


@pytest.mark.anyio
async def test_wrapper_keeps_drain_and_input_reset() -> None:
    async with _line(_Wrapper) as (bus, stream, _events):
        await bus.slave(1).read_holding_registers(0, count=1)
        await bus.slave(1).write_register(0, 1)
    assert stream.calls == ["reset_input_buffer", "send", "drain"] * 2


@pytest.mark.anyio
async def test_stream_without_the_methods_still_works() -> None:
    async with _line(_PlainWrapper) as (bus, stream, _events):
        assert await bus.slave(1).read_holding_registers(0, count=1) == (0,)
    assert stream.calls == ["send"]


@pytest.mark.anyio
async def test_synchronous_drain_is_ignored() -> None:
    async with _line(_SyncDrainWrapper) as (bus, stream, _events):
        assert await bus.slave(1).read_holding_registers(0, count=1) == (0,)
    assert stream.calls == ["send"]


@pytest.mark.anyio
async def test_config_flags_still_disable_drain_and_reset() -> None:
    cfg = BusConfig(drain_after_send=False, reset_input_buffer_before_request=False)
    async with _line(_Wrapper, config=cfg) as (bus, stream, _events):
        await bus.slave(1).read_holding_registers(0, count=1)
    assert stream.calls == ["send"]


@pytest.mark.anyio
@pytest.mark.parametrize(("forward", "baud_for_timing"), [(True, 1_200), (False, 19_200)])
async def test_auto_timing_uses_a_forwarded_baud_rate(forward: bool, baud_for_timing: int) -> None:
    # A wrapper that forwards ``extra_attributes`` exposes the port's
    # SerialStreamAttribute.config; one that does not gets the 19200 fallback.
    def wrap(inner: SerialPort) -> _PlainWrapper:
        return _Wrapper(inner, forward_attributes=forward)

    async with _line(wrap, baudrate=1_200) as (bus, _stream, _events):
        await bus.slave(1).read_holding_registers(0, count=1)
        idle = bus._inter_frame_idle  # pyright: ignore[reportPrivateUsage]
    assert idle == pytest.approx(max(3.5 * 11 / baud_for_timing, 0.00175))


# ---------------------------------------------------------------------------
# Port failures.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method", "exc", "expected", "phase"),
    [
        pytest.param(
            "send",
            OSError(errno.EPIPE, "broken pipe"),
            TransportError,
            "sending",
            id="send-oserror",
        ),
        pytest.param(
            "receive",
            SerialError(errno.EIO, "read failed"),
            TransportError,
            "reading",
            id="receive-serialerror",
        ),
        pytest.param(
            "drain",
            SerialError(errno.EIO, "ioctl failed"),
            TransportError,
            "draining",
            id="drain-serialerror",
        ),
        pytest.param(
            "reset_input_buffer",
            OSError(errno.EIO, "ioctl failed"),
            TransportError,
            "resetting",
            id="reset-oserror",
        ),
        pytest.param(
            "send", OSError("no errno"), TransportError, "sending", id="send-oserror-no-errno"
        ),
        pytest.param(
            "send",
            SerialDisconnectedError(errno.EIO, "gone"),
            ConnectionLostError,
            "sending",
            id="send-disconnected",
        ),
        pytest.param(
            "receive",
            SerialClosedError(errno.EBADF, "closed"),
            BusClosedError,
            "reading",
            id="receive-closed",
        ),
    ],
)
async def test_port_failure_is_a_modbus_error(
    method: str, exc: BaseException, expected: type[ModbusError], phase: str
) -> None:
    async with _line(_Wrapper) as (bus, stream, events):
        stream.fail[method] = exc
        with pytest.raises(expected, match=phase) as ei:
            await bus.slave(1).read_holding_registers(0, count=1)
    assert ei.value.__cause__ is exc
    assert events[0].outcome is TransactionOutcome.CONNECTION_ERROR
    if expected is TransportError:
        # Still an OSError (and a ConnectionLostError), with the errno kept.
        caught: object = ei.value
        assert isinstance(caught, OSError)
        assert isinstance(caught, ConnectionLostError)
        assert caught.errno == getattr(exc, "errno", None)


@pytest.mark.anyio
async def test_next_transaction_still_gets_its_idle_gap() -> None:
    cfg = BusConfig(timing=TimingConfig(inter_frame_idle=_IDLE))
    async with _line(_Wrapper, config=cfg) as (bus, stream, events):
        await bus.slave(1).read_holding_registers(0, count=1)
        stream.fail["reset_input_buffer"] = OSError(errno.EIO, "ioctl failed")
        with pytest.raises(TransportError):
            await bus.slave(1).read_holding_registers(0, count=1)
        await bus.slave(1).read_holding_registers(0, count=1)
    failed, recovered = events[1], events[2]
    assert failed.sent_at is None
    assert recovered.sent_at is not None
    assert recovered.sent_at - failed.ended_at >= _IDLE


@pytest.mark.anyio
async def test_stream_ending_during_the_late_reply_window_is_connection_lost() -> None:
    cfg = BusConfig(
        request_timeout=0.05,
        retries=RetryPolicy(retries=0),
        timing=TimingConfig(late_reply_window=0.1),
    )
    slave = MockSlave(address=1, faults=FaultPlan(delay_response_seconds=0.5))
    async with _line(_Wrapper, config=cfg, slave=slave) as (bus, stream, _events):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=1)
        stream.fail["receive"] = anyio.EndOfStream()
        with pytest.raises(ConnectionLostError, match="ended while waiting"):
            await bus.slave(1).read_holding_registers(0, count=1)


@pytest.mark.anyio
async def test_broadcast_port_failure_is_a_modbus_error() -> None:
    async with _line(_Wrapper) as (bus, stream, _events):
        stream.fail["send"] = SerialError(errno.EIO, "write failed")
        with pytest.raises(TransportError, match="broadcast"):
            await bus.broadcast_write_register(0, 1)


@pytest.mark.anyio
async def test_timeout_is_not_rewrapped() -> None:
    # FrameTimeoutError is a TimeoutError, which is an OSError: it must reach
    # the caller as itself, not as a TransportError.
    cfg = BusConfig(request_timeout=0.05, retries=RetryPolicy(retries=0))
    slave = MockSlave(address=1, faults=FaultPlan(delay_response_seconds=0.5))
    async with _line(_Wrapper, config=cfg, slave=slave) as (bus, _stream, _events):
        with pytest.raises(FrameTimeoutError) as ei:
            await bus.slave(1).read_holding_registers(0, count=1)
    assert not isinstance(ei.value, TransportError)
