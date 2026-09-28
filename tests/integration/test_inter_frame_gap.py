"""Regression tests: the inter-frame idle gap runs from the end of the last transaction.

:class:`anymodbus.Bus` waits ``TimingConfig.inter_frame_idle`` before each
request. Up to 0.2.0 the gap was measured from the end of the previous
transaction only when it succeeded; otherwise it was measured from when the
previous *request* was sent. A reply that took longer than the gap (as real
devices' do) had already used it up, so after an exception response, a
checksum or framing error, a timeout or a cancellation the next request went
out with no gap at all.

Each test ends one transaction in a particular way, then sends a normal
request and checks, on the slave's side of the wire, that it arrived at least
``inter_frame_idle`` after the first transaction ended. Only lower bounds are
asserted: sleeps overshoot (by up to ~16 ms on Windows), which only lengthens
the gap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import anyio.abc
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
    ModbusError,
    RetryPolicy,
    TimingConfig,
    UnexpectedResponseError,
)
from anymodbus._types import FunctionCode
from anymodbus.crc import crc16_modbus_bytes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_SLAVE = 1
_IDLE = 0.05
# The first reply is held back for longer than the idle gap, as a real device's
# is (13-27 ms against a few ms of idle on the bench that found this bug). On an
# instant in-process pair a send-time stamp would otherwise still cover the gap.
_REPLY_DELAY = 0.1
_REQUEST_TIMEOUT = 0.3
# retries=0: a retried attempt sleeps the idle gap itself, which would hide the bug.
_CONFIG = BusConfig(
    request_timeout=_REQUEST_TIMEOUT,
    timing=TimingConfig(inter_frame_idle=_IDLE),
    retries=RetryPolicy(retries=0),
)
# FC 0x03 and FC 0x06 requests are both 8 bytes on the wire.
_REQUEST_LEN = 8


def _frame(pdu: bytes) -> bytes:
    head = bytes((_SLAVE,)) + pdu
    return head + crc16_modbus_bytes(head)


_GOOD = _frame(bytes((FunctionCode.READ_HOLDING_REGISTERS, 2, 0x12, 0x34)))
_EXCEPTION = _frame(bytes((FunctionCode.READ_HOLDING_REGISTERS | 0x80, 0x02)))
_BAD_CRC = _GOOD[:-1] + bytes((_GOOD[-1] ^ 0x01,))
_WRONG_FC = _frame(bytes((FunctionCode.READ_INPUT_REGISTERS, 2, 0x12, 0x34)))
_BAD_BYTE_COUNT = _frame(bytes((FunctionCode.READ_HOLDING_REGISTERS, 251)))


async def _read_exact(stream: anyio.abc.ByteStream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        buf.extend(await stream.receive(n - len(buf)))
    return bytes(buf)


class _ScriptedSlave:
    """Answers the first request with ``first_reply`` (or not at all), then one more at once."""

    def __init__(self, first_reply: bytes | None) -> None:
        self._first_reply = first_reply
        self.second_request_at = 0.0

    async def serve(self, stream: anyio.abc.ByteStream) -> None:
        await _read_exact(stream, _REQUEST_LEN)
        if self._first_reply is not None:
            await anyio.sleep(_REPLY_DELAY)
            await stream.send(self._first_reply)
        await _read_exact(stream, _REQUEST_LEN)
        self.second_request_at = anyio.current_time()
        await stream.send(_GOOD)


async def _gap_after(
    first_transaction: Callable[[Bus], Awaitable[None]],
    *,
    first_reply: bytes | None,
    config: BusConfig = _CONFIG,
) -> float:
    """Return the time from the end of ``first_transaction`` to the next request's arrival."""
    serial_config = SerialConfig(baudrate=19_200)
    client_end, slave_end = serial_port_pair(config_a=serial_config, config_b=serial_config)
    bus = Bus(client_end, config=config)
    slave = _ScriptedSlave(first_reply)
    ended_at = 0.0
    async with anyio.create_task_group() as tg, bus, slave_end:
        _ = tg.start_soon(slave.serve, slave_end)
        await first_transaction(bus)
        ended_at = anyio.current_time()
        assert await bus.slave(_SLAVE).read_holding_registers(0, count=1) == (0x1234,)
    return slave.second_request_at - ended_at


@pytest.mark.anyio
async def test_idle_gap_after_normal_reply() -> None:
    async def first(bus: Bus) -> None:
        _ = await bus.slave(_SLAVE).read_holding_registers(0, count=1)

    assert await _gap_after(first, first_reply=_GOOD) >= _IDLE


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("first_reply", "error"),
    [
        pytest.param(_EXCEPTION, IllegalDataAddressError, id="exception-response"),
        pytest.param(_BAD_CRC, CRCError, id="crc-error"),
        pytest.param(_WRONG_FC, UnexpectedResponseError, id="unexpected-response"),
        pytest.param(_BAD_BYTE_COUNT, FrameError, id="malformed-response"),
        pytest.param(None, FrameTimeoutError, id="timeout"),
    ],
)
async def test_idle_gap_after_failed_transaction(
    first_reply: bytes | None, error: type[ModbusError]
) -> None:
    async def first(bus: Bus) -> None:
        with pytest.raises(error):
            await bus.slave(_SLAVE).read_holding_registers(0, count=1)

    assert await _gap_after(first, first_reply=first_reply) >= _IDLE


@pytest.mark.anyio
async def test_idle_gap_after_cancelled_transaction() -> None:
    async def first(bus: Bus) -> None:
        with anyio.move_on_after(_REPLY_DELAY) as scope:
            await bus.slave(_SLAVE).read_holding_registers(0, count=1)
        assert scope.cancelled_caught

    assert await _gap_after(first, first_reply=None) >= _IDLE


@pytest.mark.anyio
async def test_idle_gap_after_cancelled_broadcast() -> None:
    config = _CONFIG.with_changes(
        timing=TimingConfig(inter_frame_idle=_IDLE, broadcast_turnaround=1.0)
    )

    async def first(bus: Bus) -> None:
        with anyio.move_on_after(_REPLY_DELAY) as scope:
            await bus.broadcast_write_register(0, 1)
        assert scope.cancelled_caught

    assert await _gap_after(first, first_reply=None, config=config) >= _IDLE
