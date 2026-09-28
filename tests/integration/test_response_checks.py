"""A reply must answer the request that was sent.

A register read whose reply carries another number of registers, a write whose
echo differs from the request, and a reply whose function code was damaged in
transit all raise a retryable ``ProtocolError`` instead of handing the caller
the wrong data. Reads are retried under the default :class:`RetryPolicy`;
writes are not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from anymodbus import (
    BusConfig,
    CRCError,
    RetryPolicy,
    UnexpectedResponseError,
)
from anymodbus.framer import encode_adu
from anymodbus.testing import MockSlave, client_server_pair

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import anyio.abc

    from anymodbus import Slave

_NO_RETRY = BusConfig(request_timeout=0.5, retries=RetryPolicy(retries=0))
_DEFAULT = BusConfig(request_timeout=0.5)


class _ScriptedSlave(MockSlave):
    """Replaces the reply to the n-th request (0-based) with a scripted PDU.

    Every request is still handled normally first, so writes still land.
    """

    def __init__(self, replies: dict[int, bytes], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.replies = replies
        self.requests = 0

    def handle(self, request_pdu: bytes) -> bytes:
        index = self.requests
        self.requests += 1
        response = super().handle(request_pdu)
        return self.replies.get(index, response)


class _DamagingSlave(MockSlave):
    """Sends its first reply with the function-code byte changed after the CRC is computed."""

    def __init__(self, damaged_fc: int, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.damaged_fc = damaged_fc
        self.replies_sent = 0

    async def send_response(self, stream: anyio.abc.ByteStream, response_pdu: bytes) -> None:
        self.replies_sent += 1
        if self.replies_sent > 1:
            await super().send_response(stream, response_pdu)
            return
        frame = bytearray(encode_adu(slave_address=self.address, pdu=response_pdu))
        frame[1] = self.damaged_fc
        await stream.send(bytes(frame))


# ---------------------------------------------------------------------------
# Register count.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(b"\x03\x02\x00\x01", id="one-word-short"),
        pytest.param(b"\x03\x06\x00\x01\x00\x02\x00\x03", id="one-word-long"),
    ],
)
async def test_wrong_register_count_raises(reply: bytes) -> None:
    slave = _ScriptedSlave({0: reply}, address=1)
    async with client_server_pair(slave, bus_config=_NO_RETRY) as (bus, _server):
        with pytest.raises(UnexpectedResponseError, match="request asked for 2"):
            await bus.slave(1).read_holding_registers(0, count=2)


@pytest.mark.anyio
async def test_wrong_input_register_count_raises() -> None:
    slave = _ScriptedSlave({0: b"\x04\x02\x00\x01"}, address=1)
    async with client_server_pair(slave, bus_config=_NO_RETRY) as (bus, _server):
        with pytest.raises(UnexpectedResponseError):
            await bus.slave(1).read_input_registers(0, count=2)


@pytest.mark.anyio
async def test_wrong_register_count_is_retried_by_default() -> None:
    slave = _ScriptedSlave({0: b"\x03\x02\x00\x01"}, address=1)
    slave.holding_registers[0:2] = [0x1234, 0x5678]
    async with client_server_pair(slave, bus_config=_DEFAULT) as (bus, _server):
        assert await bus.slave(1).read_holding_registers(0, count=2) == (0x1234, 0x5678)
    assert slave.requests == 2


@pytest.mark.anyio
async def test_typed_read_checks_the_count() -> None:
    # read_float asks for two registers; a one-register reply must not decode.
    slave = _ScriptedSlave({0: b"\x03\x02\x00\x01"}, address=1)
    async with client_server_pair(slave, bus_config=_NO_RETRY) as (bus, _server):
        with pytest.raises(UnexpectedResponseError):
            await bus.slave(1).read_float(0)


@pytest.mark.anyio
async def test_wrong_coil_byte_count_raises() -> None:
    slave = _ScriptedSlave({0: b"\x01\x01\xff"}, address=1)
    async with client_server_pair(slave, bus_config=_NO_RETRY) as (bus, _server):
        with pytest.raises(UnexpectedResponseError):
            await bus.slave(1).read_coils(0, count=9)


@pytest.mark.anyio
async def test_correct_replies_still_pass() -> None:
    slave = _ScriptedSlave({}, address=1)
    slave.holding_registers[0:3] = [1, 2, 3]
    async with client_server_pair(slave, bus_config=_NO_RETRY) as (bus, _server):
        assert await bus.slave(1).read_holding_registers(0, count=3) == (1, 2, 3)
        await bus.slave(1).write_register(4, 9)
        await bus.slave(1).write_registers(5, [7, 8])
        await bus.slave(1).write_coil(3, on=True)
        await bus.slave(1).write_coils(0, [True, False])
        assert await bus.slave(1).diagnostic_loopback(b"\x12\x34") == b"\x12\x34"


# ---------------------------------------------------------------------------
# Write echoes.
# ---------------------------------------------------------------------------


async def _write_register(slave: Slave) -> None:
    await slave.write_register(4, 9)


async def _write_registers(slave: Slave) -> None:
    await slave.write_registers(4, [1, 2])


async def _write_coil(slave: Slave) -> None:
    await slave.write_coil(4, on=True)


async def _write_coils(slave: Slave) -> None:
    await slave.write_coils(4, [True, False])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("write", "echo"),
    [
        pytest.param(_write_register, b"\x06\x00\x04\x00\x08", id="fc06-value"),
        pytest.param(_write_register, b"\x06\x00\x05\x00\x09", id="fc06-address"),
        pytest.param(_write_registers, b"\x10\x00\x04\x00\x01", id="fc10-count"),
        pytest.param(_write_coil, b"\x05\x00\x04\x00\x00", id="fc05-value"),
        pytest.param(_write_coils, b"\x0f\x00\x05\x00\x02", id="fc0f-address"),
    ],
)
async def test_mismatched_write_echo_raises_and_is_not_retried(
    write: Callable[[Slave], Awaitable[None]], echo: bytes
) -> None:
    slave = _ScriptedSlave({0: echo}, address=1)
    async with client_server_pair(slave, bus_config=_DEFAULT) as (bus, _server):
        with pytest.raises(UnexpectedResponseError, match="may have been applied"):
            await write(bus.slave(1))
    assert slave.requests == 1


@pytest.mark.anyio
async def test_loopback_echo_mismatch_is_retried() -> None:
    slave = _ScriptedSlave({0: b"\x08\x00\x00\xff\xff"}, address=1)
    async with client_server_pair(slave, bus_config=_DEFAULT) as (bus, _server):
        assert await bus.slave(1).diagnostic_loopback(b"\x12\x34") == b"\x12\x34"
    assert slave.requests == 2


# ---------------------------------------------------------------------------
# A reply function code the client never sends: damage, or a confused slave.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_damaged_function_code_on_a_read_is_retried() -> None:
    slave = _DamagingSlave(0x0B, address=1)  # 03 -> 0B is one bit
    slave.holding_registers[0] = 0xCAFE
    async with client_server_pair(slave, bus_config=_DEFAULT) as (bus, _server):
        assert await bus.slave(1).read_holding_registers(0, count=1) == (0xCAFE,)
    assert slave.replies_sent == 2


@pytest.mark.anyio
async def test_damaged_function_code_on_a_write_is_a_crc_error() -> None:
    slave = _DamagingSlave(0x07, address=1)  # 06 -> 07 is one bit
    async with client_server_pair(slave, bus_config=_DEFAULT) as (bus, _server):
        with pytest.raises(CRCError):
            await bus.slave(1).write_register(4, 9)
    # Not retried, and not reported as "the slave lacks FC 07".
    assert slave.replies_sent == 1
    assert slave.holding_registers[4] == 9


@pytest.mark.anyio
async def test_checksum_valid_unimplemented_function_code_is_unexpected() -> None:
    slave = _ScriptedSlave({0: b"\x07\x00"}, address=1)
    async with client_server_pair(slave, bus_config=_NO_RETRY) as (bus, _server):
        with pytest.raises(UnexpectedResponseError, match="fc 0x07"):
            await bus.slave(1).read_holding_registers(0, count=1)
