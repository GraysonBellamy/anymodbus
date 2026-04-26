"""End-to-end smoke tests for ``Bus._txn`` and broadcasts.

These use a small hand-rolled echo slave that decodes the request enough to
construct a plausible response, encodes it with a real CRC, and writes it
back across an :func:`anyserial.testing.serial_port_pair`. The framer and
timing paths in :class:`anymodbus.Bus` see real bytes on the wire.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import anyio
import anyio.abc
import pytest
from anyserial import SerialConfig
from anyserial.testing import serial_port_pair

from anymodbus import (
    Bus,
    BusConfig,
    ConfigurationError,
    FrameTimeoutError,
    IllegalDataAddressError,
    RetryPolicy,
    TimingConfig,
)
from anymodbus._types import ExceptionCode, FunctionCode
from anymodbus.crc import crc16_modbus_bytes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# A non-default but realistic baud — picks up the auto-timing path.
_TEST_BAUD = 19_200


def _frame(slave: int, pdu: bytes) -> bytes:
    """Wrap a PDU in slave + CRC for tx from the fake slave."""
    head = bytes((slave,)) + pdu
    return head + crc16_modbus_bytes(head)


def _exception_pdu(fc: int, exception_code: int) -> bytes:
    return bytes((fc | 0x80, exception_code))


async def _read_request(stream: anyio.abc.ByteStream) -> tuple[int, bytes]:
    """Read one Modbus RTU request from ``stream``.

    Recognises only the FCs we exercise here. Returns ``(slave, pdu)`` with
    the trailing CRC stripped; the caller is responsible for whatever
    response shape it wants.
    """
    head = await _read_exact(stream, 2)
    slave = head[0]
    fc = head[1]
    if fc in (
        FunctionCode.READ_COILS,
        FunctionCode.READ_DISCRETE_INPUTS,
        FunctionCode.READ_HOLDING_REGISTERS,
        FunctionCode.READ_INPUT_REGISTERS,
        FunctionCode.WRITE_SINGLE_COIL,
        FunctionCode.WRITE_SINGLE_REGISTER,
    ):
        body = await _read_exact(stream, 4)
        crc = await _read_exact(stream, 2)
        return slave, head[1:] + body + crc
    if fc in (
        FunctionCode.WRITE_MULTIPLE_COILS,
        FunctionCode.WRITE_MULTIPLE_REGISTERS,
    ):
        body = await _read_exact(stream, 5)
        byte_count = body[4]
        rest = await _read_exact(stream, byte_count + 2)  # data + crc
        return slave, head[1:] + body + rest
    msg = f"echo slave: unsupported FC {fc:#04x}"
    raise AssertionError(msg)


async def _read_exact(stream: anyio.abc.ByteStream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = await stream.receive(n - len(buf))
        buf.extend(chunk)
    return bytes(buf)


async def _serve_one_fc03(
    stream: anyio.abc.ByteStream,
    slave_address: int,
    register_values: tuple[int, ...],
) -> None:
    """Read a single FC 3 request and answer with ``register_values``."""
    _, pdu = await _read_request(stream)
    assert pdu[0] == FunctionCode.READ_HOLDING_REGISTERS
    response_pdu = bytes(
        (FunctionCode.READ_HOLDING_REGISTERS, len(register_values) * 2)
    ) + struct.pack(f">{len(register_values)}H", *register_values)
    await stream.send(_frame(slave_address, response_pdu))


async def _serve_n(
    stream: anyio.abc.ByteStream,
    slave_address: int,
    handler: Callable[[bytes], Awaitable[bytes | None]],
    count: int,
) -> None:
    """Loop ``count`` times: read one request, call ``handler``, optionally reply.

    ``handler`` returns the response PDU (FC + body, no CRC, no slave byte)
    or ``None`` to drop the response entirely (for timeout simulations).
    """
    for _ in range(count):
        _, request_pdu = await _read_request(stream)
        response_pdu = await handler(request_pdu)
        if response_pdu is not None:
            await stream.send(_frame(slave_address, response_pdu))


def _pair() -> tuple[anyio.abc.ByteStream, anyio.abc.ByteStream]:
    cfg = SerialConfig(baudrate=_TEST_BAUD)
    return serial_port_pair(config_a=cfg, config_b=cfg)


@pytest.mark.anyio
async def test_bus_txn_read_holding_registers_roundtrip() -> None:
    """A simple FC 3 request returns the bytes the fake slave wrote back."""
    client_end, slave_end = _pair()
    bus = Bus(client_end)
    expected = (0x0001, 0x0203, 0x0405)
    result: tuple[int, ...] = ()
    async with anyio.create_task_group() as tg, bus, slave_end:
        tg.start_soon(_serve_one_fc03, slave_end, 1, expected)
        result = await bus.slave(1).read_holding_registers(0x0010, count=3)
    assert result == expected


@pytest.mark.anyio
async def test_bus_txn_serializes_concurrent_callers() -> None:
    """Two tasks sharing one bus do not interleave their frames."""
    client_end, slave_end = _pair()
    bus = Bus(client_end)

    async def handler(request_pdu: bytes) -> bytes:
        # Echo back a single register with the value of the request's start
        # address — gives each caller a unique, identifiable response.
        addr = struct.unpack(">H", request_pdu[1:3])[0]
        return bytes((FunctionCode.READ_HOLDING_REGISTERS, 2)) + struct.pack(">H", addr)

    addresses = list(range(8))
    results: dict[int, int] = {}

    async with anyio.create_task_group() as tg, bus, slave_end:
        tg.start_soon(_serve_n, slave_end, 1, handler, len(addresses))

        async def fetch(addr: int) -> None:
            (val,) = await bus.slave(1).read_holding_registers(addr, count=1)
            results[addr] = val

        async with anyio.create_task_group() as inner:
            for a in addresses:
                inner.start_soon(fetch, a)

    assert results == {a: a for a in addresses}


@pytest.mark.anyio
async def test_bus_txn_request_timeout_raises_frame_timeout() -> None:
    """``request_timeout`` elapses with no bytes → :class:`FrameTimeoutError`."""
    client_end, slave_end = _pair()
    cfg = BusConfig(request_timeout=0.05, retries=RetryPolicy(retries=0))
    bus = Bus(client_end, config=cfg)
    async with bus, slave_end:
        with pytest.raises(FrameTimeoutError):
            await bus.slave(1).read_holding_registers(0, count=1)


@pytest.mark.anyio
async def test_bus_txn_outer_cancel_scope_preempts_request_timeout() -> None:
    """An outer ``move_on_after`` shorter than ``request_timeout`` wins."""
    client_end, slave_end = _pair()
    cfg = BusConfig(request_timeout=3.0, retries=RetryPolicy(retries=0))
    bus = Bus(client_end, config=cfg)
    async with bus, slave_end:
        cancelled = False
        with anyio.move_on_after(0.05) as scope:
            try:
                await bus.slave(1).read_holding_registers(0, count=1)
            except FrameTimeoutError:  # pragma: no cover — wrong path
                pytest.fail("outer scope should have preempted before timeout fired")
        cancelled = scope.cancelled_caught
        assert cancelled


@pytest.mark.anyio
async def test_bus_txn_modbus_exception_not_retried() -> None:
    """Exception responses surface immediately — no silent retry."""
    client_end, slave_end = _pair()
    cfg = BusConfig(retries=RetryPolicy(retries=3))  # would retry 3x if eligible
    bus = Bus(client_end, config=cfg)

    async def respond_exception(_request: bytes) -> bytes:
        return _exception_pdu(
            FunctionCode.READ_HOLDING_REGISTERS, ExceptionCode.ILLEGAL_DATA_ADDRESS
        )

    async with anyio.create_task_group() as tg, bus, slave_end:
        # Exactly ONE response is served; if Bus retried, the test would
        # block forever waiting for a second response (and pytest would
        # eventually fail by timeout). The narrow expectation is that
        # IllegalDataAddressError surfaces on the first try.
        tg.start_soon(_serve_n, slave_end, 1, respond_exception, 1)
        with pytest.raises(IllegalDataAddressError):
            await bus.slave(1).read_holding_registers(0x9999, count=1)


@pytest.mark.anyio
async def test_bus_rejects_unicast_to_address_zero() -> None:
    """``Slave(0)`` is rejected at construction; broadcasts have their own API."""
    client_end, slave_end = _pair()
    bus = Bus(client_end)
    async with bus, slave_end:
        with pytest.raises(ConfigurationError):
            bus.slave(0)


@pytest.mark.anyio
async def test_bus_broadcast_write_register_no_response_expected() -> None:
    """A broadcast write returns ``None`` and does not wait for an rx frame."""
    client_end, slave_end = _pair()
    cfg = BusConfig(
        timing=TimingConfig(broadcast_turnaround=0.001),
        retries=RetryPolicy(retries=0),
    )
    bus = Bus(client_end, config=cfg)

    received: list[bytes] = []

    async def collector(stream: anyio.abc.ByteStream) -> None:
        # Read the first request frame to confirm it hit the wire; do NOT
        # send any response back.
        _, pdu = await _read_request(stream)
        received.append(pdu)

    async with anyio.create_task_group() as tg, bus, slave_end:
        tg.start_soon(collector, slave_end)
        # broadcast_write_register returns None — the call completing without
        # blocking on rx is the assertion (combined with the request-frame
        # check below).
        await bus.broadcast_write_register(0x0010, 0x1234)
    assert len(received) == 1
    assert received[0][0] == FunctionCode.WRITE_SINGLE_REGISTER


@pytest.mark.anyio
async def test_bus_broadcast_holds_turnaround_delay() -> None:
    """The bus lock is held for ``broadcast_turnaround`` after a broadcast."""
    client_end, slave_end = _pair()
    turnaround = 0.05
    cfg = BusConfig(
        timing=TimingConfig(broadcast_turnaround=turnaround),
        retries=RetryPolicy(retries=0),
    )
    bus = Bus(client_end, config=cfg)

    async def consume_one(stream: anyio.abc.ByteStream) -> None:
        await _read_request(stream)

    elapsed = 0.0
    async with anyio.create_task_group() as tg, bus, slave_end:
        tg.start_soon(consume_one, slave_end)
        start = anyio.current_time()
        await bus.broadcast_write_register(0x0010, 0x1234)
        elapsed = anyio.current_time() - start
    assert elapsed >= turnaround


@pytest.mark.anyio
async def test_bus_honours_post_tx_settle() -> None:
    """``post_tx_settle`` adds a settling delay between send and rx start.

    The fault-free round-trip for a 1-register read takes well under 1 ms on
    an in-process serial pair, so a 50 ms settle is dominated by the sleep.
    """
    client_end, slave_end = _pair()
    settle = 0.05
    cfg = BusConfig(
        timing=TimingConfig(post_tx_settle=settle),
        retries=RetryPolicy(retries=0),
    )
    bus = Bus(client_end, config=cfg)
    expected = (0xCAFE,)
    result: tuple[int, ...] = ()
    elapsed = 0.0

    async with anyio.create_task_group() as tg, bus, slave_end:
        tg.start_soon(_serve_one_fc03, slave_end, 1, expected)
        start = anyio.current_time()
        result = await bus.slave(1).read_holding_registers(0, count=1)
        elapsed = anyio.current_time() - start
    assert result == expected
    assert elapsed >= settle
