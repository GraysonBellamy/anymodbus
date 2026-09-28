"""Transaction observers: one report per attempt, with when it was sent and how it ended."""

from __future__ import annotations

import logging

import anyio
import pytest
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from anymodbus import (
    Bus,
    BusConfig,
    CRCError,
    FrameError,
    FrameTimeoutError,
    IllegalDataAddressError,
    RetryPolicy,
    TimingConfig,
    TransactionInfo,
    TransactionOutcome,
    UnexpectedResponseError,
)
from anymodbus.testing import (
    FaultPlan,
    MockServer,
    MockSlave,
    client_server_pair,
    client_slave_pair,
)

_NO_RETRY = BusConfig(request_timeout=0.2, retries=RetryPolicy(retries=0))


class _ReplySlave(MockSlave):
    """Answers every request with ``reply`` instead of the register banks."""

    def __init__(self, reply: bytes) -> None:
        super().__init__(address=1)
        self._reply = reply

    def handle(self, request_pdu: bytes) -> bytes:
        return self._reply


@pytest.mark.anyio
async def test_one_report_per_attempt_including_retries() -> None:
    events: list[TransactionInfo] = []
    cfg = BusConfig(retries=RetryPolicy(retries=1))
    async with client_slave_pair(faults=FaultPlan(corrupt_crc_after_n=0), bus_config=cfg) as (
        bus,
        _slave,
    ):
        bus.add_transaction_observer(events.append)
        await bus.slave(1).read_holding_registers(0, count=1)
        await bus.slave(1).read_holding_registers(0, count=1)
    first, second, third = events
    assert (first.outcome, first.attempt, first.max_attempts, first.will_retry) == (
        TransactionOutcome.CHECKSUM_ERROR,
        1,
        2,
        True,
    )
    assert isinstance(first.error, CRCError)
    assert (second.outcome, second.attempt, second.will_retry) == (
        TransactionOutcome.REPLY,
        2,
        False,
    )
    assert second.error is None
    assert first.request_id == second.request_id != third.request_id
    assert (third.outcome, third.attempt) == (TransactionOutcome.REPLY, 1)
    assert all((e.slave_address, e.function_code) == (1, 0x03) for e in events)


@pytest.mark.anyio
async def test_timeout_is_reported() -> None:
    events: list[TransactionInfo] = []
    async with client_slave_pair(
        faults=FaultPlan(delay_response_seconds=0.5), bus_config=_NO_RETRY
    ) as (bus, _slave):
        bus.add_transaction_observer(events.append)
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=1)
    (event,) = events
    assert event.outcome is TransactionOutcome.TIMEOUT
    assert isinstance(event.error, FrameTimeoutError)
    assert event.sent_at is not None
    assert event.ended_at - event.sent_at >= 0.2


@pytest.mark.anyio
async def test_exception_reply_is_reported() -> None:
    events: list[TransactionInfo] = []
    async with client_slave_pair(register_count=4) as (bus, _slave):
        bus.add_transaction_observer(events.append)
        with pytest.raises(IllegalDataAddressError):
            await bus.slave(1).read_holding_registers(10, count=1)
    (event,) = events
    assert event.outcome is TransactionOutcome.EXCEPTION_REPLY
    assert isinstance(event.error, IllegalDataAddressError)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "outcome", "error"),
    [
        pytest.param(
            b"\x03\x04\x00\x01\x00\x02",
            TransactionOutcome.UNEXPECTED_RESPONSE,
            UnexpectedResponseError,
            id="wrong-count",
        ),
        pytest.param(b"\x03\xfb", TransactionOutcome.FRAME_ERROR, FrameError, id="bad-byte-count"),
    ],
)
async def test_bad_replies_are_reported(
    reply: bytes, outcome: TransactionOutcome, error: type[Exception]
) -> None:
    events: list[TransactionInfo] = []
    async with client_server_pair(_ReplySlave(reply), bus_config=_NO_RETRY) as (bus, _server):
        bus.add_transaction_observer(events.append)
        with pytest.raises(error):
            await bus.slave(1).read_holding_registers(0, count=1)
    (event,) = events
    assert event.outcome is outcome
    assert isinstance(event.error, error)


@pytest.mark.anyio
async def test_cancellation_is_reported() -> None:
    events: list[TransactionInfo] = []
    async with client_slave_pair(faults=FaultPlan(delay_response_seconds=0.5)) as (bus, _slave):
        bus.add_transaction_observer(events.append)
        with anyio.move_on_after(0.05):
            await bus.slave(1).read_holding_registers(0, count=1)
    (event,) = events
    assert event.outcome is TransactionOutcome.CANCELLED
    assert event.error is None
    assert event.sent_at is not None
    assert not event.will_retry


@pytest.mark.anyio
async def test_broadcast_is_reported() -> None:
    events: list[TransactionInfo] = []
    cfg = BusConfig(timing=TimingConfig(broadcast_turnaround=0.02))
    async with client_slave_pair(bus_config=cfg) as (bus, _slave):
        bus.add_transaction_observer(events.append)
        await bus.broadcast_write_register(3, 7)
    (event,) = events
    assert event.outcome is TransactionOutcome.BROADCAST_SENT
    assert (event.slave_address, event.function_code) == (0, 0x06)
    assert (event.attempt, event.max_attempts, event.will_retry) == (1, 1, False)
    assert event.sent_at is not None
    assert event.ended_at - event.sent_at >= 0.02


@pytest.mark.anyio
async def test_sent_at_is_after_the_inter_frame_gap() -> None:
    idle = 0.05
    events: list[TransactionInfo] = []
    received: list[float] = []
    cfg = BusConfig(timing=TimingConfig(inter_frame_idle=idle))
    async with client_server_pair(
        MockSlave(address=1),
        bus_config=cfg,
        on_request=lambda _a, _p: received.append(anyio.current_time()),
    ) as (bus, _server):
        bus.add_transaction_observer(events.append)
        await bus.slave(1).read_holding_registers(0, count=1)
        await bus.slave(1).read_holding_registers(0, count=1)
    first, second = events
    assert second.sent_at is not None
    assert second.started_at < second.sent_at < second.ended_at
    # The gap is waited out inside the call, before the request is sent.
    assert second.sent_at - first.ended_at >= idle
    # The request was on the wire by sent_at: the slave had it no earlier.
    assert all(
        e.sent_at is not None and e.sent_at <= r for e, r in zip(events, received, strict=True)
    )
    assert second.sent_at_ns is not None
    assert second.sent_at_ns <= second.ended_at_ns
    assert first.ended_at_ns <= second.sent_at_ns


@pytest.mark.anyio
async def test_raising_observer_does_not_break_the_transaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[TransactionInfo] = []

    def broken(_info: TransactionInfo) -> None:
        msg = "observer bug"
        raise RuntimeError(msg)

    async with client_slave_pair() as (bus, slave):
        slave.holding_registers[0] = 5
        bus.add_transaction_observer(broken)
        bus.add_transaction_observer(events.append)
        with caplog.at_level(logging.ERROR, logger="anymodbus.bus"):
            assert await bus.slave(1).read_holding_registers(0, count=1) == (5,)
    assert len(events) == 1
    assert "observer" in caplog.text
    assert "observer bug" in caplog.text


@pytest.mark.anyio
async def test_observers_can_be_removed_and_are_called_in_order() -> None:
    calls: list[str] = []
    async with client_slave_pair() as (bus, _slave):
        remove_a = bus.add_transaction_observer(lambda _i: calls.append("a"))
        bus.add_transaction_observer(lambda _i: calls.append("b"))
        await bus.slave(1).read_holding_registers(0, count=1)
        remove_a()
        remove_a()  # harmless
        await bus.slave(1).read_holding_registers(0, count=1)
    assert calls == ["a", "b", "b"]


@pytest.mark.anyio
async def test_constructor_observer() -> None:
    events: list[TransactionInfo] = []
    cfg = SerialConfig(baudrate=19_200)
    client_end, server_end = serial_port_pair(config_a=cfg, config_b=cfg)
    bus = Bus(client_end, on_transaction=events.append)
    async with bus, server_end, anyio.create_task_group() as tg:
        _ = tg.start_soon(MockServer(MockSlave(address=1)).serve, server_end)
        await bus.slave(1).read_holding_registers(0, count=1)
        tg.cancel()
    assert [e.outcome for e in events] == [TransactionOutcome.REPLY]
