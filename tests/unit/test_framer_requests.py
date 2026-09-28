"""Tests for ``Framer.read_request_adu`` — reading one request frame, server side.

The RTU reader frames by the *request* length of each function code, verifies
the CRC, drains a damaged frame so the next read starts at a boundary, and does
not filter by slave address. The ASCII reader frames by ``:``..CRLF and
verifies the LRC.
"""

from __future__ import annotations

import struct
from collections import deque
from typing import cast

import anyio
import anyio.abc
import pytest

from anymodbus import Framing
from anymodbus.exceptions import CRCError, FrameError, LRCError
from anymodbus.framer import encode_adu
from anymodbus.framer_ascii import encode_ascii_adu
from anymodbus.framing import get_framer
from anymodbus.pdu import (
    encode_diagnostic_loopback_request,
    encode_read_coils_request,
    encode_read_holding_registers_request,
    encode_write_multiple_coils_request,
    encode_write_multiple_registers_request,
    encode_write_single_coil_request,
    encode_write_single_register_request,
)

pytestmark = pytest.mark.anyio

_GAP = 0.005


class _ScriptStream:
    """Returns scripted chunks from ``receive``; a float item sleeps first.

    After the script, ``receive`` raises :class:`anyio.EndOfStream`, or blocks
    forever with ``hold_open=True``.
    """

    def __init__(self, *items: bytes | float, hold_open: bool = False) -> None:
        self._items: deque[bytes | float] = deque(items)
        self._pending = bytearray()
        self._hold_open = hold_open

    async def receive(self, max_bytes: int = 65536) -> bytes:
        if not self._pending:
            delay = 0.0
            while self._items and isinstance(self._items[0], float):
                delay += cast("float", self._items.popleft())
            if delay:
                await anyio.sleep(delay)
            if not self._items:
                if self._hold_open:
                    await anyio.sleep_forever()
                raise anyio.EndOfStream
            self._pending.extend(cast("bytes", self._items.popleft()))
        out = bytes(self._pending[:max_bytes])
        del self._pending[:max_bytes]
        return out


def _stream(*items: bytes | float, hold_open: bool = False) -> anyio.abc.ByteStream:
    return cast("anyio.abc.ByteStream", _ScriptStream(*items, hold_open=hold_open))


async def _read(stream: anyio.abc.ByteStream) -> tuple[int, bytes]:
    return await get_framer(Framing.RTU).read_request_adu(stream, inter_char_idle=_GAP)


@pytest.mark.parametrize(
    "pdu",
    [
        pytest.param(encode_read_coils_request(0, 9), id="fc01"),
        pytest.param(encode_read_holding_registers_request(0x10, 3), id="fc03"),
        pytest.param(encode_write_single_coil_request(4, on=True), id="fc05"),
        pytest.param(encode_write_single_register_request(4, 0xBEEF), id="fc06"),
        pytest.param(encode_diagnostic_loopback_request(b"\x12\x34"), id="fc08"),
        pytest.param(encode_write_multiple_coils_request(2, [True] * 11), id="fc0f"),
        pytest.param(encode_write_multiple_registers_request(2, [1, 2, 3]), id="fc10"),
        pytest.param(struct.pack(">BHHH", 0x16, 1, 0xFF00, 0x0012), id="fc16"),
        pytest.param(struct.pack(">BHHHHB", 0x17, 0, 2, 8, 1, 2) + b"\x00\x07", id="fc17"),
    ],
)
async def test_reads_each_request_by_length(pdu: bytes) -> None:
    # Two requests back to back in one chunk: the first read must stop exactly
    # at the first frame's end.
    first = encode_adu(slave_address=3, pdu=pdu)
    second = encode_adu(slave_address=4, pdu=encode_read_holding_registers_request(0, 1))
    stream = _stream(first + second)
    assert await _read(stream) == (3, pdu)
    assert await _read(stream) == (4, encode_read_holding_registers_request(0, 1))


async def test_any_address_is_returned() -> None:
    for address in (0, 1, 200, 247):
        pdu = encode_read_holding_registers_request(0, 1)
        assert await _read(_stream(encode_adu(slave_address=address, pdu=pdu))) == (address, pdu)


async def test_unknown_function_code_is_read_to_the_idle_gap() -> None:
    pdu = bytes((0x41, 0x01, 0x02, 0x03))  # user-defined FC 65
    stream = _stream(encode_adu(slave_address=1, pdu=pdu), hold_open=True)
    assert await _read(stream) == (1, pdu)


async def test_bad_crc_raises_and_resyncs_at_the_next_frame() -> None:
    good = encode_adu(slave_address=1, pdu=encode_read_holding_registers_request(0, 2))
    damaged = good[:-1] + bytes((good[-1] ^ 0xFF,))
    stream = _stream(damaged, _GAP * 4, good)
    with pytest.raises(CRCError):
        await _read(stream)
    assert await _read(stream) == (1, encode_read_holding_registers_request(0, 2))


async def test_impossible_byte_count_raises_frame_error() -> None:
    head = struct.pack(">BBHHB", 1, 0x10, 0, 2, 251)
    with pytest.raises(FrameError, match="byte_count"):
        await _read(_stream(head, hold_open=True))


async def test_close_between_frames_is_end_of_stream() -> None:
    with pytest.raises(anyio.EndOfStream):
        await _read(_stream())


async def test_close_mid_frame_is_frame_error() -> None:
    with pytest.raises(FrameError):
        await _read(_stream(b"\x01"))


async def test_ascii_request() -> None:
    pdu = encode_write_single_register_request(4, 0xBEEF)
    stream = _stream(encode_ascii_adu(slave_address=9, pdu=pdu))
    framer = get_framer(Framing.ASCII)
    assert await framer.read_request_adu(stream, inter_char_idle=_GAP) == (9, pdu)


async def test_ascii_request_bad_lrc() -> None:
    frame = bytearray(encode_ascii_adu(slave_address=9, pdu=b"\x03\x00\x00\x00\x01"))
    # Change the last LRC hex digit (just before CRLF).
    frame[-3] = ord("0") if frame[-3] != ord("0") else ord("1")
    framer = get_framer(Framing.ASCII)
    with pytest.raises(LRCError):
        await framer.read_request_adu(_stream(bytes(frame)), inter_char_idle=_GAP)
