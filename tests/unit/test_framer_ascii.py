"""Tests for the Modbus-ASCII framer (:mod:`anymodbus.framer_ascii`).

Covers the worked vector from the servomex handoff (``:1E0400000001DD\\r\\n``),
the raw-frame reader's no-LRC-judgment contract (D2), stray-skip consistency
with RTU, the no-over-read property (critical for the shared-port consumer),
and the malformed-frame failure modes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import anyio
import anyio.abc
import pytest

from anymodbus.exceptions import FrameError, LRCError
from anymodbus.framer_ascii import (
    ASCII_FRAMER,
    encode_ascii_adu,
    read_ascii_frame,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

pytestmark = pytest.mark.anyio

_INTER_CHAR_GAP_S = 0.005


class FakeByteStream:
    """Minimal duck-typed ByteStream yielding pre-canned bytes, EOF when drained."""

    def __init__(self, items: Iterable[bytes]) -> None:
        self._tail = bytearray(b"".join(items))

    async def receive(self, max_bytes: int = 65536) -> bytes:
        if not self._tail:
            raise anyio.EndOfStream
        n = min(len(self._tail), max_bytes)
        out = bytes(self._tail[:n])
        del self._tail[:n]
        return out

    async def send(self, item: bytes) -> None:  # pragma: no cover - unused
        del item

    async def aclose(self) -> None:  # pragma: no cover - unused
        self._tail.clear()

    @property
    def remaining(self) -> bytes:
        return bytes(self._tail)


def _stream(*items: bytes) -> anyio.abc.ByteStream:
    return cast("anyio.abc.ByteStream", FakeByteStream(items))


# ---------------------------------------------------------------------------
# encode_ascii_adu — exact wire bytes.
# ---------------------------------------------------------------------------


def test_encode_known_vector() -> None:
    # FC04, slave 0x1E, addr 0x0000, count 0x0001 -> LRC 0xDD.
    pdu = bytes.fromhex("0400000001")
    adu = encode_ascii_adu(slave_address=0x1E, pdu=pdu)
    assert adu == b":1E0400000001DD\r\n"


def test_encode_is_uppercase() -> None:
    adu = encode_ascii_adu(slave_address=0xAB, pdu=bytes.fromhex("04ffee0001"))
    assert adu == adu.upper()
    assert adu.startswith(b":")
    assert adu.endswith(b"\r\n")


def test_encode_rejects_empty_pdu() -> None:
    with pytest.raises(ValueError, match="pdu must not be empty"):
        encode_ascii_adu(slave_address=0x01, pdu=b"")


def test_encode_rejects_out_of_range_address() -> None:
    with pytest.raises(ValueError, match="slave_address"):
        encode_ascii_adu(slave_address=0x100, pdu=b"\x04\x00")


# ---------------------------------------------------------------------------
# read_ascii_frame — raw reader, does NOT judge the LRC (D2).
# ---------------------------------------------------------------------------


async def test_read_ascii_frame_returns_raw_bytes() -> None:
    frame = b":1E0400000001DD\r\n"
    raw = await read_ascii_frame(_stream(frame))
    assert raw == bytes.fromhex("1E0400000001DD")


async def test_read_ascii_frame_does_not_judge_lrc() -> None:
    # A bad LRC still returns the raw bytes; the caller's verify_lrc decides.
    body = bytes.fromhex("1E0400000001")
    bad = body + b"\x00"  # wrong LRC (correct is 0xDD)
    frame = b":" + bad.hex().upper().encode() + b"\r\n"
    raw = await read_ascii_frame(_stream(frame))
    assert raw == bad


async def test_read_accepts_lowercase_hex() -> None:
    raw = await read_ascii_frame(_stream(b":1e0400000001dd\r\n"))
    assert raw == bytes.fromhex("1E0400000001DD")


async def test_read_skips_leading_garbage_until_colon() -> None:
    raw = await read_ascii_frame(_stream(b"junk\r\n:1E0400000001DD\r\n"))
    assert raw == bytes.fromhex("1E0400000001DD")


async def test_non_hex_body_raises_frame_error() -> None:
    with pytest.raises(FrameError, match="non-hex"):
        await read_ascii_frame(_stream(b":1E04ZZ\r\n"))


async def test_cr_without_lf_raises_frame_error() -> None:
    with pytest.raises(FrameError, match="CR not followed by LF"):
        await read_ascii_frame(_stream(b":1E0400000001DD\r\x00"))


async def test_odd_length_body_raises_frame_error() -> None:
    # 13 hex chars -> odd -> un-de-hexable.
    with pytest.raises(FrameError):
        await read_ascii_frame(_stream(b":1E0400000001D\r\n"))


async def test_eof_mid_frame_raises_frame_error() -> None:
    with pytest.raises(FrameError, match="closed mid"):
        await read_ascii_frame(_stream(b":1E04000000"))


async def test_too_short_frame_raises_frame_error() -> None:
    # De-hexes to 2 raw bytes (< addr+fc+lrc minimum of 3).
    with pytest.raises(FrameError, match="too short"):
        await read_ascii_frame(_stream(b":0102\r\n"))


async def test_oversized_frame_without_crlf_raises_frame_error() -> None:
    # More hex than the max ADU allows, never terminated.
    oversized = b":" + b"AB" * 300
    with pytest.raises(FrameError, match="exceeds maximum length"):
        await read_ascii_frame(_stream(oversized))


async def test_no_over_read_two_concatenated_frames() -> None:
    """Reader consumes exactly one frame; the next remains for the next reader."""
    first = b":1E0400000001DD\r\n"
    second = b":050300000001F7\r\n"
    fake = FakeByteStream([first + second])
    stream = cast("anyio.abc.ByteStream", fake)
    raw = await read_ascii_frame(stream)
    assert raw == bytes.fromhex("1E0400000001DD")
    # The entire second frame must still be readable, untouched.
    assert fake.remaining == second
    raw2 = await read_ascii_frame(stream)
    assert raw2[0] == 0x05


# ---------------------------------------------------------------------------
# AsciiFramer.read_adu — stray-skip before LRC, LRCError only for our frame.
# ---------------------------------------------------------------------------


async def test_read_adu_returns_pdu_for_our_address() -> None:
    frame = encode_ascii_adu(slave_address=0x1E, pdu=bytes.fromhex("0400000001"))
    slave, pdu = await ASCII_FRAMER.read_adu(
        _stream(frame), expected_slave_address=0x1E, inter_char_idle=_INTER_CHAR_GAP_S
    )
    assert slave == 0x1E
    assert pdu == bytes.fromhex("0400000001")


async def test_read_adu_raises_lrc_error_for_our_bad_frame() -> None:
    body = bytes.fromhex("1E0400000001")
    bad = body + b"\x00"
    frame = b":" + bad.hex().upper().encode() + b"\r\n"
    with pytest.raises(LRCError):
        await ASCII_FRAMER.read_adu(
            _stream(frame), expected_slave_address=0x1E, inter_char_idle=_INTER_CHAR_GAP_S
        )


async def test_read_adu_skips_stray_then_returns_good() -> None:
    # A corrupt (bad-LRC) frame addressed to ANOTHER slave must be skipped, not
    # raised — matching RTU stray-drain (D2). Then our good frame is returned.
    stray_body = bytes.fromhex("050300000001")
    stray = b":" + (stray_body + b"\x00").hex().upper().encode() + b"\r\n"  # bad LRC, addr 0x05
    good = encode_ascii_adu(slave_address=0x1E, pdu=bytes.fromhex("0400000001"))
    slave, pdu = await ASCII_FRAMER.read_adu(
        _stream(stray + good), expected_slave_address=0x1E, inter_char_idle=_INTER_CHAR_GAP_S
    )
    assert slave == 0x1E
    assert pdu == bytes.fromhex("0400000001")
