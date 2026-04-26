"""Tests for :func:`anymodbus.framer.read_response_adu`.

Drives the length-aware state machine deterministically by feeding pre-canned
byte chunks through a tiny duck-typed ``FakeByteStream``. Each test pins one
branch of the state machine described in *DESIGN.md §6.3*.

The framer's contract is exercised end-to-end via the production
:func:`anymodbus.framer.encode_adu` for the response ADU construction, so
that the tests are agnostic to the CRC value and stay valid if the ADU
encoder evolves.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, cast

import anyio
import anyio.abc
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from anymodbus._types import FunctionCode
from anymodbus.crc import crc16_modbus_bytes
from anymodbus.exceptions import (
    CRCError,
    FrameError,
    IllegalDataAddressError,
    IllegalFunctionError,
    ModbusUnknownExceptionError,
    ModbusUnsupportedFunctionError,
    ProtocolError,
    SlaveDeviceFailureError,
    UnexpectedResponseError,
)
from anymodbus.framer import encode_adu, read_response_adu
from anymodbus.pdu import (
    decode_read_holding_registers_response,
    decode_read_input_registers_response,
    decode_write_single_register_response,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

pytestmark = pytest.mark.anyio


# A relaxed t1.5 gap for the fallback / drain branches: tests don't simulate
# real serial timing, so we use a small but non-trivial value.
_INTER_CHAR_GAP_S = 0.005


class FakeByteStream:
    """Duck-typed :class:`anyio.abc.ByteStream` for framer tests.

    Yields the pre-canned ``items`` from successive ``receive()`` calls,
    splitting individual ``bytes`` items if the framer requests fewer bytes
    than the item holds. ``float`` items in the script are interpreted as a
    ``anyio.sleep(...)`` to perform before the next receive returns — used to
    simulate inter-frame silence so the framer's ``_read_until_idle`` gap
    timer can fire deterministically.

    After the script is exhausted, behaviour depends on ``hold_open``:

    - ``hold_open=False`` (default): subsequent receives raise
      :class:`anyio.EndOfStream`. Use this for the bulk of tests; it lets
      the framer hit EOF cleanly when something goes wrong.
    - ``hold_open=True``: subsequent receives block forever (until the
      enclosing scope cancels). Use this when you want the framer's
      ``_read_until_idle`` gap timer to be the thing that fires.
    """

    def __init__(self, items: Iterable[bytes | float], *, hold_open: bool = False) -> None:
        self._items: deque[bytes | float] = deque(items)
        self._tail: bytearray = bytearray()
        self._hold_open: bool = hold_open

    async def receive(self, max_bytes: int = 65536) -> bytes:
        if self._tail:
            n = min(len(self._tail), max_bytes)
            out = bytes(self._tail[:n])
            del self._tail[:n]
            return out
        # Honour any leading delay items, then return the next bytes chunk.
        # Note: each delay is popped *before* awaiting, so a cancellation
        # mid-sleep (the gap timer firing) doesn't leave the delay queued for
        # the next call — matching real-stream semantics where time has passed.
        while self._items and not isinstance(self._items[0], (bytes, bytearray)):
            delay = self._items.popleft()
            await anyio.sleep(float(delay))
        if not self._items:
            if self._hold_open:
                await anyio.sleep_forever()
            raise anyio.EndOfStream
        chunk = self._items.popleft()
        assert isinstance(chunk, (bytes, bytearray))
        if len(chunk) > max_bytes:
            self._tail.extend(chunk[max_bytes:])
            return bytes(chunk[:max_bytes])
        return bytes(chunk)

    async def send(self, item: bytes) -> None:  # pragma: no cover - unused
        del item

    async def send_eof(self) -> None:  # pragma: no cover - unused
        pass

    async def aclose(self) -> None:
        self._items.clear()
        self._tail.clear()


def _stream(*items: bytes | float, hold_open: bool = False) -> anyio.abc.ByteStream:
    """Build a FakeByteStream from positional bytes/delay items, cast to ByteStream."""
    return cast("anyio.abc.ByteStream", FakeByteStream(items, hold_open=hold_open))


def _adu(slave: int, pdu: bytes) -> bytes:
    """Wrap ``pdu`` into a full ADU using the production encoder."""
    return encode_adu(slave_address=slave, pdu=pdu)


def _flip_last_byte(adu: bytes) -> bytes:
    """Flip the high CRC byte to invalidate the CRC."""
    return adu[:-1] + bytes((adu[-1] ^ 0xFF,))


# ---------------------------------------------------------------------------
# Normal flow — one test per response shape.
# ---------------------------------------------------------------------------


class TestNormalFlow:
    """Round-trip a typical response for each length-table branch."""

    async def test_fc03_byte_count_branch_round_trip(self) -> None:
        # *app §6.3*: 3 registers = 0x022B, 0x0000, 0x0064.
        pdu = bytes([0x03, 0x06, 0x02, 0x2B, 0x00, 0x00, 0x00, 0x64])
        adu = _adu(0x11, pdu)
        slave, returned_pdu = await read_response_adu(
            _stream(adu),
            expected_slave_address=0x11,
            expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x11
        assert returned_pdu == pdu
        assert decode_read_holding_registers_response(returned_pdu) == (0x022B, 0x0000, 0x0064)

    async def test_fc04_byte_count_branch_round_trip(self) -> None:
        pdu = bytes([0x04, 0x02, 0x00, 0x0A])
        adu = _adu(0x01, pdu)
        slave, returned_pdu = await read_response_adu(
            _stream(adu),
            expected_slave_address=0x01,
            expected_function_code=FunctionCode.READ_INPUT_REGISTERS,
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x01
        assert decode_read_input_registers_response(returned_pdu) == (0x000A,)

    async def test_fc06_fixed_tail_round_trip(self) -> None:
        pdu = bytes([0x06, 0x00, 0x01, 0x00, 0x03])
        adu = _adu(0x11, pdu)
        slave, returned_pdu = await read_response_adu(
            _stream(adu),
            expected_slave_address=0x11,
            expected_function_code=FunctionCode.WRITE_SINGLE_REGISTER,
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x11
        assert decode_write_single_register_response(returned_pdu) == (0x0001, 0x0003)

    async def test_fc10_fixed_tail_round_trip(self) -> None:
        pdu = bytes([0x10, 0x00, 0x01, 0x00, 0x02])
        adu = _adu(0x07, pdu)
        slave, returned_pdu = await read_response_adu(
            _stream(adu),
            expected_slave_address=0x07,
            expected_function_code=FunctionCode.WRITE_MULTIPLE_REGISTERS,
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x07
        assert returned_pdu == pdu

    async def test_debug_log_records_rx_hex(self, caplog: pytest.LogCaptureFixture) -> None:
        # The framer logs every received frame at DEBUG; users opt in by
        # configuring the "anymodbus.bus" logger. Pin the format so a future
        # change to the log statement (e.g. removing the hex dump) is loud.
        pdu = bytes([0x06, 0x00, 0x01, 0x00, 0x03])
        adu = _adu(0x11, pdu)
        with caplog.at_level(logging.DEBUG, logger="anymodbus.bus"):
            await read_response_adu(
                _stream(adu),
                expected_slave_address=0x11,
                expected_function_code=FunctionCode.WRITE_SINGLE_REGISTER,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )
        assert any("rx" in r.message and adu.hex() in r.message for r in caplog.records)

    async def test_chunked_arrival_assembled(self) -> None:
        # Same FC 3 response as above but delivered in 2-byte chunks, simulating
        # the kernel-buffer scenario length-aware reads were designed for.
        adu = _adu(0x11, bytes([0x03, 0x06, 0x02, 0x2B, 0x00, 0x00, 0x00, 0x64]))
        chunks = [adu[i : i + 2] for i in range(0, len(adu), 2)]
        slave, _pdu = await read_response_adu(
            _stream(*chunks),
            expected_slave_address=0x11,
            expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x11


# ---------------------------------------------------------------------------
# Exception responses (FC | 0x80).
# ---------------------------------------------------------------------------


class TestExceptionResponses:
    """The FC-with-high-bit-set branch."""

    async def test_illegal_function_response(self) -> None:
        # Slave returns exception 0x01 (ILLEGAL_FUNCTION) for FC 3.
        pdu = bytes([0x03 | 0x80, 0x01])
        adu = _adu(0x01, pdu)
        with pytest.raises(IllegalFunctionError) as ei:
            await read_response_adu(
                _stream(adu),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )
        assert ei.value.function_code == 0x03
        assert ei.value.exception_code == 0x01

    async def test_illegal_data_address_response(self) -> None:
        pdu = bytes([0x06 | 0x80, 0x02])
        adu = _adu(0x05, pdu)
        with pytest.raises(IllegalDataAddressError):
            await read_response_adu(
                _stream(adu),
                expected_slave_address=0x05,
                expected_function_code=FunctionCode.WRITE_SINGLE_REGISTER,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_slave_device_failure_response(self) -> None:
        pdu = bytes([0x03 | 0x80, 0x04])
        adu = _adu(0x01, pdu)
        with pytest.raises(SlaveDeviceFailureError):
            await read_response_adu(
                _stream(adu),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_unassigned_exception_code_returns_unknown_class(self) -> None:
        # 0x07 is the legacy NAK code, intentionally not in the standard table
        # — surfaces as ModbusUnknownExceptionError per DESIGN.md §7.
        pdu = bytes([0x03 | 0x80, 0x07])
        adu = _adu(0x01, pdu)
        with pytest.raises(ModbusUnknownExceptionError) as ei:
            await read_response_adu(
                _stream(adu),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )
        assert ei.value.exception_code == 0x07
        assert ei.value.function_code == 0x03

    async def test_bad_crc_on_exception_raises_crc_error_not_exception(self) -> None:
        # CRC must be checked BEFORE the slave-reported exception code is
        # trusted. A bad CRC surfaces as CRCError (retryable), NOT as the
        # exception code (which would mislead callers about what the slave
        # actually said). DESIGN.md §6.3 explicitly calls this out.
        pdu = bytes([0x03 | 0x80, 0x01])
        adu = _adu(0x01, pdu)
        corrupted = _flip_last_byte(adu)
        with pytest.raises(CRCError):
            await read_response_adu(
                _stream(corrupted),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_exception_with_fc_mismatch(self) -> None:
        # Exception payload echoes a different base FC than we requested.
        pdu = bytes([0x06 | 0x80, 0x02])
        adu = _adu(0x01, pdu)
        with pytest.raises(UnexpectedResponseError, match="exception"):
            await read_response_adu(
                _stream(adu),
                expected_slave_address=0x01,
                # We sent FC 3, but the exception echoes FC 6.
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )


# ---------------------------------------------------------------------------
# Wire-level protocol violations.
# ---------------------------------------------------------------------------


class TestProtocolValidation:
    async def test_fc_zero_rejected(self) -> None:
        # *app §4.1*: function code 0 is not valid.
        # Hand-craft an ADU with FC byte 0; we need a CRC over [slave, 0]
        # because the framer reads 2 bytes first then dispatches on fc==0.
        forged = bytes([0x01, 0x00]) + crc16_modbus_bytes(bytes([0x01, 0x00]))
        with pytest.raises(ProtocolError, match="function code 0"):
            await read_response_adu(
                _stream(forged),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_oversized_byte_count_rejected(self) -> None:
        # *app §4.1*: PDU max = 253 bytes → byte_count for the 1-byte-bc FCs
        # tops out at 250. A slave returning byte_count=251 must be rejected
        # before we attempt the (large, speculative) read.
        # Note: we craft just the head + bc; the framer rejects on bc, so it
        # never reads the (nonexistent) data + CRC.
        forged = bytes([0x01, 0x03, 251])
        with pytest.raises(FrameError, match="exceeds spec max"):
            await read_response_adu(
                _stream(forged),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_max_byte_count_250_accepted(self) -> None:
        # Boundary: bc=250 must NOT be rejected by the size guard. We don't
        # need a real-world response to test this; just make sure the bc
        # check doesn't trip and that the framer actually attempts to read
        # 250 + 2 more bytes. We supply truncated data so the read fails
        # with FrameError from _read_exact, not from the bc guard.
        forged = bytes([0x01, 0x03, 250]) + b"\x00" * 5
        with pytest.raises(FrameError, match="stream closed"):
            await read_response_adu(
                _stream(forged),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_mismatched_fc_raises_unexpected_response(self) -> None:
        # Slave responded with a normal FC 4 frame, but we asked for FC 3.
        pdu = bytes([0x04, 0x02, 0x00, 0x0A])
        adu = _adu(0x01, pdu)
        with pytest.raises(UnexpectedResponseError):
            await read_response_adu(
                _stream(adu),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_bad_crc_on_normal_response(self) -> None:
        pdu = bytes([0x03, 0x02, 0x00, 0x05])
        adu = _adu(0x01, pdu)
        with pytest.raises(CRCError):
            await read_response_adu(
                _stream(_flip_last_byte(adu)),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_truncated_frame_raises_frame_error(self) -> None:
        # Stream delivers head + byte_count but EOF before data.
        with pytest.raises(FrameError, match="stream closed"):
            await read_response_adu(
                _stream(bytes([0x01, 0x03, 0x04])),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_truncated_header_raises_frame_error(self) -> None:
        # Stream delivers only 1 byte then EOF.
        with pytest.raises(FrameError, match=r"0/2|1/2"):
            await read_response_adu(
                _stream(bytes([0x01])),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )


# ---------------------------------------------------------------------------
# Known-but-unsupported FCs — the precise-error branch.
# ---------------------------------------------------------------------------


class TestKnownUnsupportedFunctionCodes:
    """Recognise spec FCs we haven't implemented and fail loudly."""

    @pytest.mark.parametrize(
        "fc",
        [
            0x07,  # Read Exception Status
            0x08,  # Diagnostics
            0x0B,  # Get Comm Event Counter
            0x0C,  # Get Comm Event Log
            0x11,  # Report Server ID
            0x14,  # Read File Record
            0x15,  # Write File Record
            0x18,  # Read FIFO Queue
            0x2B,  # Encapsulated Interface Transport
        ],
    )
    async def test_known_unsupported_fc_raises_precise_error(self, fc: int) -> None:
        # Reachable only when the caller has expected an unsupported FC — i.e.,
        # a future FC was added to the enum but its read branch wasn't (or the
        # caller cast a raw int through). The realistic v0.1 case is FC 0x2B
        # (in the enum, no read branch yet); the others are defensive
        # future-proofing. We use a cast for all of them so the test pins the
        # branch regardless of which FCs end up in the enum.
        #
        # The point of this branch — versus letting it fall into the
        # gap-based fallback — is that the gap fallback could mis-frame the
        # next response on the wire. The precise error stops the bus cleanly.
        forged = bytes([0x01, fc])
        with pytest.raises(ModbusUnsupportedFunctionError, match=f"{fc:#04x}"):
            await read_response_adu(
                _stream(forged),
                expected_slave_address=0x01,
                expected_function_code=cast("FunctionCode", fc),
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_unsupported_fc_returned_when_supported_expected(self) -> None:
        # Realistic-but-rare bug case: we sent FC 3, slave returns FC 0x08
        # (Diagnostics) by mistake. The fc-mismatch check fires first, so the
        # caller sees UnexpectedResponseError, not ModbusUnsupportedFunctionError
        # — the framer doesn't have to reason about what the slave "meant".
        forged = bytes([0x01, 0x08])
        with pytest.raises(UnexpectedResponseError, match="fc 0x08"):
            await read_response_adu(
                _stream(forged),
                expected_slave_address=0x01,
                expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )


# ---------------------------------------------------------------------------
# Stray slave-address drain.
# ---------------------------------------------------------------------------


class TestStraySlaveDrain:
    """Per *serial §2.4.1*, a reply for another slave must not abort us."""

    async def test_stray_frame_then_valid_frame_returns_valid(self) -> None:
        # Slave 0x05 replies first (a stray, addressed to a different request
        # — irrelevant to us). Then slave 0x01, the one we asked, replies
        # with a valid FC 3 frame. We must drain the stray (using the gap
        # timer in _read_until_idle to identify its end) and return the
        # second frame's payload.
        stray_pdu = bytes([0x03, 0x02, 0xDE, 0xAD])
        stray_adu = _adu(0x05, stray_pdu)
        valid_pdu = bytes([0x03, 0x02, 0x00, 0x42])
        valid_adu = _adu(0x01, valid_pdu)
        # Insert an inter-frame silence > inter_char_idle so the drain's
        # gap timer fires between the two frames, just like real RTU traffic
        # respecting the t3.5 inter-frame gap.
        slave, pdu = await read_response_adu(
            _stream(stray_adu, _INTER_CHAR_GAP_S * 4, valid_adu),
            expected_slave_address=0x01,
            expected_function_code=FunctionCode.READ_HOLDING_REGISTERS,
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x01
        assert pdu == valid_pdu


# ---------------------------------------------------------------------------
# FC 0x16 length regression.
# ---------------------------------------------------------------------------


class TestSpecGotchas:
    """Pin the spec corners that are easy to mis-implement."""

    async def test_fc16_uses_8_byte_tail_not_6(self) -> None:
        # *app §6.16*: response is addr(2) + AND(2) + OR(2) + crc(2) = 8 bytes
        # after the 2-byte header — NOT 6 like the other write echoes. If the
        # framer ever lumps it in with FC 0x05/0x06/0x0F/0x10 (all 6 bytes),
        # this test fails because the read stops 2 bytes early and CRC fails.
        # PDU = FC(1) + ref_addr(2) + and_mask(2) + or_mask(2) = 7 bytes.
        pdu = bytes([0x16, 0x00, 0x04, 0x00, 0xF2, 0x00, 0x25])
        adu = _adu(0x01, pdu)
        # ADU = slave(1) + PDU(7) + CRC(2) = 10 bytes; the framer reads a
        # 2-byte head then exactly 8 more bytes (= _FIXED_TAIL[0x16]).
        assert len(adu) == 10
        slave, returned_pdu = await read_response_adu(
            _stream(adu),
            expected_slave_address=0x01,
            expected_function_code=FunctionCode.MASK_WRITE_REGISTER,
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x01
        assert returned_pdu == pdu

    async def test_fc01_through_fc04_dispatch_through_byte_count_branch(self) -> None:
        # Sanity: each of the four read FCs must use the byte_count branch.
        # We exercise this by sending a response whose byte_count says 4 but
        # whose true length is 4 + 2 (data + CRC); the only way the framer
        # parses this correctly is by reading bc first.
        for fc in (
            FunctionCode.READ_COILS,
            FunctionCode.READ_DISCRETE_INPUTS,
            FunctionCode.READ_HOLDING_REGISTERS,
            FunctionCode.READ_INPUT_REGISTERS,
        ):
            pdu = bytes([fc, 0x04, 0x00, 0x00, 0x00, 0x01])
            adu = _adu(0x09, pdu)
            slave, returned_pdu = await read_response_adu(
                _stream(adu),
                expected_slave_address=0x09,
                expected_function_code=fc,
                inter_char_idle=_INTER_CHAR_GAP_S,
            )
            assert slave == 0x09
            assert returned_pdu == pdu


# ---------------------------------------------------------------------------
# Truly unknown FC — gap-based fallback.
# ---------------------------------------------------------------------------


class TestUnknownFcFallback:
    """Vendor-private FCs fall back to the t1.5 idle-gap reader."""

    async def test_unknown_fc_branch_via_internal_dispatch(self) -> None:
        # Reach the unknown-FC fallback by lying through expected_function_code
        # using a raw int cast. This is the only way to drive that branch in
        # v0.1, since the FunctionCode enum doesn't carry vendor FCs and the
        # earlier fc != expected check would otherwise fire.
        body = bytes([0xAA, 0xBB])
        pdu = bytes([0x65]) + body  # FC 0x65 in user-defined range
        adu = _adu(0x01, pdu)
        slave, returned_pdu = await read_response_adu(
            _stream(adu),
            expected_slave_address=0x01,
            expected_function_code=cast("FunctionCode", 0x65),
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x01
        assert returned_pdu == pdu

    async def test_unknown_fc_truncated_after_fc_byte_raises_frame_error(self) -> None:
        # FC 0x65 with only 1 trailing byte (less than the 2-byte CRC).
        forged = bytes([0x01, 0x65, 0xAA])
        with pytest.raises(FrameError, match="truncated"):
            await read_response_adu(
                _stream(forged),
                expected_slave_address=0x01,
                expected_function_code=cast("FunctionCode", 0x65),
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_unknown_fc_idle_gap_terminates_read(self) -> None:
        # Stream delivers head + payload chunked, then holds open. The gap
        # timer in _read_until_idle should fire and let the framer proceed.
        pdu = bytes([0x65, 0xAA, 0xBB])
        adu = _adu(0x01, pdu)
        # Deliver: head (2 bytes), then the payload + CRC as one chunk, then
        # hold the stream open. The framer's first _read_until_idle receive
        # gets the payload+CRC chunk; the second blocks until the gap fires.
        head, rest = adu[:2], adu[2:]
        slave, returned_pdu = await read_response_adu(
            _stream(head, rest, hold_open=True),
            expected_slave_address=0x01,
            expected_function_code=cast("FunctionCode", 0x65),
            inter_char_idle=_INTER_CHAR_GAP_S,
        )
        assert slave == 0x01
        assert returned_pdu == pdu

    async def test_unknown_fc_payload_arrives_in_two_chunks_within_gap(self) -> None:
        # Verify that _read_until_idle assembles bytes across multiple
        # receives that arrive within the gap window — i.e. the loop
        # continues past the first iteration before the gap fires.
        #
        # Timing values here are deliberately larger than the suite-wide
        # _INTER_CHAR_GAP_S because Windows' default timer resolution is
        # ~15.6ms — a sub-millisecond `anyio.sleep` actually runs at the
        # next tick, which on Windows would exceed the 5ms gap and cause
        # the framer to terminate between the two chunks. 200ms gap with
        # a 50ms short delay leaves plenty of headroom on every platform.
        gap = 0.2
        short_delay = 0.05  # well under the gap, also well above any timer resolution
        long_delay = 0.5  # well over the gap
        pdu = bytes([0x65, 0xAA, 0xBB, 0xCC, 0xDD])
        adu = _adu(0x01, pdu)
        head = adu[:2]
        payload_part1 = adu[2:5]
        payload_part2 = adu[5:]
        slave, returned_pdu = await read_response_adu(
            _stream(
                head,
                payload_part1,
                short_delay,
                payload_part2,
                long_delay,
                hold_open=True,
            ),
            expected_slave_address=0x01,
            expected_function_code=cast("FunctionCode", 0x65),
            inter_char_idle=gap,
        )
        assert slave == 0x01
        assert returned_pdu == pdu

    async def test_unknown_fc_immediate_eof_after_head_raises_frame_error(self) -> None:
        # _read_until_idle's first receive hits EndOfStream → returns empty,
        # framer raises FrameError because tail < 2 bytes.
        forged = bytes([0x01, 0x65])  # head only, then EOF
        with pytest.raises(FrameError, match="truncated"):
            await read_response_adu(
                _stream(forged),
                expected_slave_address=0x01,
                expected_function_code=cast("FunctionCode", 0x65),
                inter_char_idle=_INTER_CHAR_GAP_S,
            )

    async def test_unknown_fc_caps_runaway_payload_at_spec_max(self) -> None:
        # A misbehaving slave that drips bytes inside the gap window must
        # not be allowed to inflate memory unboundedly. The max-tail bound
        # is 254 (one full ADU minus the 2-byte head), so we feed 600 bytes
        # in one chunk and assert we get back exactly 254. The CRC check
        # then fails (CRCError), proving we actually stopped reading at
        # the cap rather than letting the gap fire after consuming all.
        head = bytes([0x01, 0x65])
        runaway = b"\xaa" * 600
        with pytest.raises(CRCError):
            await read_response_adu(
                _stream(head, runaway, hold_open=True),
                expected_slave_address=0x01,
                expected_function_code=cast("FunctionCode", 0x65),
                inter_char_idle=_INTER_CHAR_GAP_S,
            )


# ---------------------------------------------------------------------------
# Hypothesis: random valid byte_count responses survive the round trip.
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.sampled_from(
        [
            FunctionCode.READ_HOLDING_REGISTERS,
            FunctionCode.READ_INPUT_REGISTERS,
        ]
    ),
    st.integers(min_value=1, max_value=125),
    st.integers(min_value=0, max_value=247),
)
@pytest.mark.parametrize("anyio_backend", [("asyncio", {"use_uvloop": False})])
async def test_hypothesis_register_response_round_trip(
    fc: FunctionCode, register_count: int, slave_address: int
) -> None:
    """Random FC 3/4 responses round-trip through the framer + decoder."""
    if slave_address == 0:
        # Broadcasts have no response to read.
        return
    registers = tuple(range(register_count))
    body = b"".join(r.to_bytes(2, "big") for r in registers)
    pdu = bytes([int(fc), 2 * register_count]) + body
    adu = _adu(slave_address, pdu)
    slave, returned_pdu = await read_response_adu(
        _stream(adu),
        expected_slave_address=slave_address,
        expected_function_code=fc,
        inter_char_idle=_INTER_CHAR_GAP_S,
    )
    assert slave == slave_address
    if fc is FunctionCode.READ_HOLDING_REGISTERS:
        assert decode_read_holding_registers_response(returned_pdu) == registers
    else:
        assert decode_read_input_registers_response(returned_pdu) == registers
