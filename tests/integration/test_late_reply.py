"""The late-reply window: a reply that arrives after its attempt ended is discarded.

When a read times out or is cancelled, its reply may still be on the wire.
Without a window, a reply with the same function code and length is taken as
the answer to the *next* request — a read at another address gets the old
data. With ``TimingConfig.late_reply_window`` the bus sends nothing until the
window has passed after an uncertain attempt, reading and discarding whatever
arrives meanwhile.

The slave here holds back chosen replies. Timing is checked on the bus side
with a transaction observer (``sent_at`` / ``ended_at``); only generous bounds
are asserted, since sleeps overshoot (by up to ~16 ms on Windows).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
import pytest
from anyserial import SerialConfig, SerialPort
from anyserial.testing import serial_port_pair

from anymodbus import (
    Bus,
    BusConfig,
    FrameTimeoutError,
    IllegalDataAddressError,
    ModbusError,
    RetryPolicy,
    TimingConfig,
    TransactionInfo,
    TransactionOutcome,
    UnexpectedResponseError,
)
from anymodbus.testing import MockServer, MockSlave

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

_TIMEOUT = 0.1
_LATE = 0.15  # the held-back reply arrives this long after its request
_WINDOW = 0.3
# A 3-register FC 0x03 reply: slave(1) + fc(1) + byte_count(1) + 6 + crc(2).
_REPLY_BYTES = 11
_A = (1, 2, 3)  # registers 0-2
_B = (4, 5, 6)  # registers 16-18


class _Slave(MockSlave):
    """Holds back or replaces chosen replies, by 0-based request index."""

    def __init__(
        self,
        *,
        delays: Mapping[int, float] | None = None,
        replies: Mapping[int, bytes] | None = None,
    ) -> None:
        super().__init__(address=1)
        self.holding_registers[0:3] = _A
        self.holding_registers[16:19] = _B
        self._delays = dict(delays or {})
        self._replies = dict(replies or {})
        self._handled = 0

    def handle(self, request_pdu: bytes) -> bytes:
        index = self._handled
        self._handled += 1
        return self._replies.get(index, super().handle(request_pdu))

    async def send_response(self, stream: anyio.abc.ByteStream, response_pdu: bytes) -> None:
        delay = self._delays.get(self.response_count, 0.0)
        if delay:
            await anyio.sleep(delay)
        await super().send_response(stream, response_pdu)


class _NoResetStream(anyio.abc.ByteStream):
    """Forwards to a serial port but offers no ``reset_input_buffer`` (nor ``drain``)."""

    def __init__(self, inner: SerialPort) -> None:
        self._inner = inner

    async def send(self, item: bytes) -> None:
        await self._inner.send(item)

    async def receive(self, max_bytes: int = 65536) -> bytes:
        return await self._inner.receive(max_bytes)

    async def send_eof(self) -> None:
        await self._inner.send_eof()

    async def aclose(self) -> None:
        await self._inner.aclose()

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return self._inner.extra_attributes


def _config(window: float, *, retries: int = 0, **timing: float) -> BusConfig:
    return BusConfig(
        request_timeout=_TIMEOUT,
        retries=RetryPolicy(retries=retries),
        timing=TimingConfig(late_reply_window=window, **timing),
    )


@asynccontextmanager
async def _line(
    slave: MockSlave,
    config: BusConfig,
    *,
    wrap: Callable[[SerialPort], anyio.abc.ByteStream] | None = None,
) -> AsyncGenerator[tuple[Bus, list[TransactionInfo]]]:
    serial_config = SerialConfig(baudrate=19_200)
    client_end, server_end = serial_port_pair(config_a=serial_config, config_b=serial_config)
    stream = wrap(client_end) if wrap is not None else client_end
    events: list[TransactionInfo] = []
    bus = Bus(stream, config=config, on_transaction=events.append)
    # The task group is entered last, so it exits (cancelling the server)
    # before the streams close; the closes do not run in the cancelled scope.
    async with bus, server_end, anyio.create_task_group() as tg:
        _ = tg.start_soon(MockServer(slave).serve, server_end)
        yield bus, events
        tg.cancel()


async def _read_a_then_b(bus: Bus) -> tuple[int, ...]:
    with pytest.raises(FrameTimeoutError):
        await bus.slave(1).read_holding_registers(0, count=3)
    return await bus.slave(1).read_holding_registers(16, count=3)


@pytest.mark.anyio
async def test_without_a_window_the_late_reply_answers_the_next_read() -> None:
    # The hazard, and the default: B at another address gets A's registers.
    async with _line(_Slave(delays={0: _LATE}), _config(0.0)) as (bus, _events):
        assert await _read_a_then_b(bus) == _A


@pytest.mark.anyio
async def test_window_discards_the_late_reply() -> None:
    async with _line(_Slave(delays={0: _LATE}), _config(_WINDOW)) as (bus, events):
        assert await _read_a_then_b(bus) == _B
    first, second = events
    assert first.outcome is TransactionOutcome.TIMEOUT
    assert second.sent_at is not None
    assert second.sent_at - first.ended_at >= _WINDOW
    assert second.discarded_bytes == _REPLY_BYTES


@pytest.mark.anyio
async def test_window_after_a_cancelled_read() -> None:
    async with _line(_Slave(delays={0: _LATE}), _config(_WINDOW)) as (bus, events):
        with anyio.move_on_after(0.05) as scope:
            await bus.slave(1).read_holding_registers(0, count=3)
        assert scope.cancelled_caught
        assert await bus.slave(1).read_holding_registers(16, count=3) == _B
    assert events[0].outcome is TransactionOutcome.CANCELLED
    assert events[0].sent_at is not None
    assert events[1].discarded_bytes == _REPLY_BYTES


@pytest.mark.anyio
async def test_retry_waits_out_the_window() -> None:
    # Without the window the retry would take attempt 1's reply, and its own
    # reply would then answer the next call.
    async with _line(_Slave(delays={0: _LATE}), _config(_WINDOW, retries=1)) as (bus, events):
        assert await bus.slave(1).read_holding_registers(0, count=3) == _A
        assert await bus.slave(1).read_holding_registers(16, count=3) == _B
    assert [e.outcome for e in events] == [
        TransactionOutcome.TIMEOUT,
        TransactionOutcome.REPLY,
        TransactionOutcome.REPLY,
    ]
    assert events[0].will_retry
    assert events[1].discarded_bytes == _REPLY_BYTES
    assert events[2].discarded_bytes == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("replies", "first", "opens_window"),
    [
        pytest.param({0: b"\x03\x02\x00\x01"}, None, True, id="wrong-count"),
        pytest.param({0: b"\x04\x06" + bytes(6)}, None, True, id="wrong-function-code"),
        pytest.param({}, None, False, id="normal-reply"),
        pytest.param({}, 300, False, id="exception-reply"),
    ],
)
async def test_which_outcomes_open_a_window(
    replies: dict[int, bytes], first: int | None, opens_window: bool
) -> None:
    async with _line(_Slave(replies=replies), _config(_WINDOW)) as (bus, events):
        address = first if first is not None else 0
        with suppress(UnexpectedResponseError, IllegalDataAddressError):
            await bus.slave(1).read_holding_registers(address, count=3)
        await bus.slave(1).read_holding_registers(16, count=3)
    second = events[1]
    assert second.sent_at is not None
    gap = second.sent_at - events[0].ended_at
    if opens_window:
        assert gap >= _WINDOW
    else:
        assert gap < _WINDOW / 2


@pytest.mark.anyio
async def test_cancelled_before_sending_opens_no_window() -> None:
    config = _config(0.5, inter_frame_idle=0.1)
    async with _line(_Slave(), config) as (bus, events):
        await bus.slave(1).read_holding_registers(0, count=3)
        with anyio.move_on_after(0.03):
            # Cancelled while waiting out the 0.1 s inter-frame gap: nothing sent.
            await bus.slave(1).read_holding_registers(0, count=3)
        await bus.slave(1).read_holding_registers(16, count=3)
    assert events[1].outcome is TransactionOutcome.CANCELLED
    assert events[1].sent_at is None
    assert events[2].sent_at is not None
    assert events[2].sent_at - events[0].ended_at < 0.3


@pytest.mark.anyio
async def test_broadcast_waits_out_the_window() -> None:
    async with _line(_Slave(delays={0: _LATE}), _config(_WINDOW)) as (bus, events):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=3)
        await bus.broadcast_write_register(3, 1)
    broadcast = events[1]
    assert broadcast.outcome is TransactionOutcome.BROADCAST_SENT
    assert broadcast.sent_at is not None
    assert broadcast.sent_at - events[0].ended_at >= _WINDOW
    assert broadcast.discarded_bytes == _REPLY_BYTES


@pytest.mark.anyio
@pytest.mark.parametrize(("window", "expected"), [(0.0, _A), (0.2, _B)])
async def test_window_drains_a_stream_without_input_reset(
    window: float, expected: tuple[int, ...]
) -> None:
    # The late reply arrives while the caller is idle and waits in the buffer.
    # The stream cannot reset its input, so only the window's discard read
    # removes it, even though the window itself has already passed.
    async with _line(_Slave(delays={0: _LATE}), _config(window), wrap=_NoResetStream) as (
        bus,
        events,
    ):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=3)
        await anyio.sleep(0.3)
        assert await bus.slave(1).read_holding_registers(16, count=3) == expected
    if window:
        assert events[1].discarded_bytes == _REPLY_BYTES


class _ChatteringSlave(MockSlave):
    """Instead of replying, keeps the line busy with a byte every 10 ms for a while."""

    async def send_response(self, stream: anyio.abc.ByteStream, response_pdu: bytes) -> None:
        del response_pdu
        with anyio.move_on_after(0.6):
            while True:
                await stream.send(b"\x00")
                await anyio.sleep(0.01)


@pytest.mark.anyio
async def test_a_line_that_stays_busy_is_logged_and_the_request_goes_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The discard stops request_timeout after the window, even if bytes keep
    # arriving (a chattering line, another master): the bus logs it and sends.
    config = _config(0.05, inter_frame_idle=0.05)
    async with _line(_ChatteringSlave(address=1), config) as (bus, events):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=3)
        with (
            caplog.at_level(logging.WARNING, logger="anymodbus.bus"),
            suppress(ModbusError),
        ):
            await bus.slave(1).read_holding_registers(16, count=3)
    assert "still busy" in caplog.text
    assert events[1].sent_at is not None
    assert events[1].discarded_bytes > 0
