"""Integration tests for :class:`MockServer` and the public :class:`MockSlave` hooks.

Several mock slaves share one line behind a :class:`MockServer`; each answers
only its own address. Also covers :meth:`MockSlave.handle` overrides,
:class:`ServerException`, :class:`QuantityLimits`, and the server's handling
of damaged and unknown requests.
"""

from __future__ import annotations

import anyio
import anyio.abc
import pytest

from anymodbus import (
    BusConfig,
    ExceptionCode,
    FrameTimeoutError,
    Framing,
    IllegalDataAddressError,
    IllegalDataValueError,
    RetryPolicy,
)
from anymodbus.exceptions import ConfigurationError
from anymodbus.framer import encode_adu
from anymodbus.pdu import encode_read_holding_registers_request
from anymodbus.testing import (
    MockServer,
    MockSlave,
    QuantityLimits,
    ServerException,
    client_server_pair,
    client_slave_pair,
)

_FAST = BusConfig(request_timeout=0.2, retries=RetryPolicy(retries=0))


async def _read_exact(stream: anyio.abc.ByteStream, n: int) -> bytes:
    buf = bytearray()
    with anyio.fail_after(1.0):
        while len(buf) < n:
            buf.extend(await stream.receive(n - len(buf)))
    return bytes(buf)


@pytest.mark.anyio
async def test_two_slaves_each_answer_their_own_address() -> None:
    one, two = MockSlave(address=1), MockSlave(address=2)
    one.holding_registers[0] = 0x1111
    two.holding_registers[0] = 0x2222
    async with client_server_pair(one, two) as (bus, server):
        assert await bus.slave(1).read_holding_registers(0, count=1) == (0x1111,)
        assert await bus.slave(2).read_holding_registers(0, count=1) == (0x2222,)
        await bus.slave(2).write_register(5, 0xBEEF)
        assert set(server.slaves) == {1, 2}
    assert two.holding_registers[5] == 0xBEEF
    assert one.holding_registers[5] == 0
    assert (one.response_count, two.response_count) == (1, 2)


@pytest.mark.anyio
async def test_absent_address_is_silent() -> None:
    one, two = MockSlave(address=1), MockSlave(address=2)
    async with client_server_pair(one, two, bus_config=_FAST) as (bus, _server):
        with pytest.raises(FrameTimeoutError):
            await bus.slave(3).read_holding_registers(0, count=1)
        # The line still works afterwards.
        assert await bus.slave(1).read_holding_registers(0, count=1) == (0,)
    assert (one.response_count, two.response_count) == (1, 0)


@pytest.mark.anyio
async def test_request_with_bad_crc_is_dropped() -> None:
    seen: list[tuple[int, bytes]] = []
    slave = MockSlave(address=1)
    slave.holding_registers[0] = 7
    async with client_server_pair(slave, on_request=lambda a, p: seen.append((a, p))) as (
        bus,
        _server,
    ):
        good = encode_adu(slave_address=1, pdu=encode_read_holding_registers_request(0, 1))
        # Writing to the bus's stream directly bypasses the bus; fine in a test.
        await bus.stream.send(good[:-1] + bytes((good[-1] ^ 0xFF,)))
        await anyio.sleep(0.05)
        assert slave.response_count == 0
        assert await bus.slave(1).read_holding_registers(0, count=1) == (7,)
    assert seen == [(1, encode_read_holding_registers_request(0, 1))]


@pytest.mark.anyio
async def test_broadcast_is_applied_by_every_slave() -> None:
    one, two = MockSlave(address=1), MockSlave(address=2)
    async with client_server_pair(one, two) as (bus, _server):
        await bus.broadcast_write_register(3, 0x0042)
        # A unicast afterwards proves the broadcast was processed first.
        await bus.slave(1).read_holding_registers(0, count=1)
    assert one.holding_registers[3] == two.holding_registers[3] == 0x0042
    assert (one.response_count, two.response_count) == (1, 0)


@pytest.mark.anyio
async def test_unknown_function_code_is_refused_with_illegal_function() -> None:
    async with client_server_pair(MockSlave(address=1)) as (bus, _server):
        await bus.stream.send(encode_adu(slave_address=1, pdu=bytes((0x41, 0x00, 0x01))))
        reply = await _read_exact(bus.stream, 5)
    assert reply == encode_adu(slave_address=1, pdu=bytes((0xC1, ExceptionCode.ILLEGAL_FUNCTION)))


@pytest.mark.anyio
async def test_malformed_request_is_answered_with_illegal_data_value() -> None:
    # FC 0x10 with a byte_count that disagrees with the quantity.
    pdu = bytes((0x10, 0x00, 0x00, 0x00, 0x02, 0x02, 0x00, 0x01))
    async with client_server_pair(MockSlave(address=1)) as (bus, _server):
        await bus.stream.send(encode_adu(slave_address=1, pdu=pdu))
        reply = await _read_exact(bus.stream, 5)
    assert reply == encode_adu(slave_address=1, pdu=bytes((0x90, ExceptionCode.ILLEGAL_DATA_VALUE)))


@pytest.mark.anyio
@pytest.mark.parametrize("framing", [Framing.RTU, Framing.ASCII])
async def test_quantity_limits(framing: Framing) -> None:
    slave = MockSlave(address=1, framing=framing, limits=QuantityLimits(read_registers=64))
    async with client_server_pair(slave, framing=framing) as (bus, _server):
        assert len(await bus.slave(1).read_holding_registers(0, count=64)) == 64
        with pytest.raises(IllegalDataValueError):
            await bus.slave(1).read_holding_registers(0, count=65)


@pytest.mark.anyio
async def test_client_slave_pair_passes_limits() -> None:
    async with client_slave_pair(limits=QuantityLimits(write_registers=2)) as (bus, _slave):
        await bus.slave(1).write_registers(0, [1, 2])
        with pytest.raises(IllegalDataValueError):
            await bus.slave(1).write_registers(0, [1, 2, 3])


class _GuardedSlave(MockSlave):
    """Refuses reads above register 9 with ILLEGAL_DATA_ADDRESS; otherwise the default."""

    def handle(self, request_pdu: bytes) -> bytes:
        if request_pdu[0] == 0x03 and int.from_bytes(request_pdu[1:3]) > 9:
            raise ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        return super().handle(request_pdu)


@pytest.mark.anyio
async def test_handle_override_can_raise_server_exception() -> None:
    async with client_server_pair(_GuardedSlave(address=1)) as (bus, _server):
        assert await bus.slave(1).read_holding_registers(9, count=1) == (0,)
        with pytest.raises(IllegalDataAddressError):
            await bus.slave(1).read_holding_registers(10, count=1)


class _RecordingSlave(MockSlave):
    """Overrides the internal ``_handle_request`` hook, as existing subclasses do."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.seen: list[bytes] = []

    def _handle_request(self, pdu: bytes) -> bytes:
        self.seen.append(pdu)
        return super()._handle_request(pdu)


@pytest.mark.anyio
async def test_handle_request_override_still_takes_effect() -> None:
    slave = _RecordingSlave(address=1)
    async with client_server_pair(slave) as (bus, _server):
        await bus.slave(1).read_holding_registers(0, count=2)
    assert slave.seen == [encode_read_holding_registers_request(0, 2)]


def test_server_rejects_duplicate_address() -> None:
    server = MockServer(MockSlave(address=1))
    with pytest.raises(ConfigurationError, match="already"):
        server.add(MockSlave(address=1))


def test_server_rejects_other_framing() -> None:
    with pytest.raises(ConfigurationError, match="framing"):
        MockServer(MockSlave(address=1, framing=Framing.ASCII))


@pytest.mark.parametrize(
    "kwargs",
    [{"read_bits": 0}, {"read_registers": 126}, {"write_coils": 1969}, {"write_registers": 0}],
)
def test_quantity_limits_are_validated(kwargs: dict[str, int]) -> None:
    with pytest.raises(ConfigurationError):
        QuantityLimits(**kwargs)
