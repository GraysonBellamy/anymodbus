"""The :class:`Bus` — single-master, half-duplex Modbus RTU client.

A :class:`Bus` owns the underlying byte stream, an :class:`anyio.Lock` that
serializes transactions, and the timing/retry state. Calls to
:meth:`Slave.read_*` / :meth:`Slave.write_*` flow through ``Bus._txn``,
which, for each attempt:

1. Acquires the bus lock.
2. After an attempt whose outcome was uncertain, waits out the late-reply
   window, reading and discarding anything that arrives.
3. Enforces the inter-frame idle gap (3.5 char-times).
4. Optionally flushes the rx buffer.
5. Writes the ADU and (optionally) drains.
6. Reads the response with the length-aware framer.
7. Decodes the PDU and checks that it answers the request.
8. Reports the attempt to the transaction observers.
9. On transient errors (checksum, malformed or mismatched reply, timeout),
   applies the retry policy.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, cast

import anyio
import anyio.abc

try:
    from anyserial import SerialStreamAttribute
except ImportError:  # pragma: no cover — anyserial is a hard dep, but be defensive.
    SerialStreamAttribute = None  # type: ignore[assignment, misc]

from anymodbus._types import Framing, FunctionCode, is_idempotent_function
from anymodbus.config import BusConfig
from anymodbus.exceptions import (
    BusClosedError,
    ChecksumError,
    ConfigurationError,
    ConnectionLostError,
    FrameTimeoutError,
    ModbusError,
    ModbusExceptionResponse,
    ProtocolError,
    TransportError,
    UnexpectedResponseError,
)
from anymodbus.framing import Framer, get_framer, interpret_response_pdu
from anymodbus.pdu import (
    encode_write_multiple_coils_request,
    encode_write_multiple_registers_request,
    encode_write_single_coil_request,
    encode_write_single_register_request,
)
from anymodbus.slave import Slave
from anymodbus.transaction import TransactionInfo, TransactionOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType

    from anymodbus.stream import SupportsDrain, SupportsResetInputBuffer
    from anymodbus.transaction import TransactionObserver

_LOGGER = logging.getLogger("anymodbus.bus")

# Fallback baudrate when the stream does not publish
# ``SerialStreamAttribute.config`` (e.g. a memory-stream test pair, or a
# wrapper that does not forward its port's typed attributes). 19200 is the
# spec's reference baud and yields the documented 1.75 ms / 0.75 ms timing
# floors.
_FALLBACK_BAUD_FOR_TIMING = 19_200

# Per *serial §2.5.1.1*: at baud > 19200 the per-character interrupt load
# becomes prohibitive, so the spec pins fixed minimum gaps.
_T35_FLOOR_SECONDS = 0.001_75
_T15_FLOOR_SECONDS = 0.000_75

# 1 start + 8 data + (parity OR extra stop) + 1 stop. Always 11 for compliant
# 8E1 / 8O1 / 8N2 framing. 8N1 is non-compliant per spec but exists in the
# wild — using 11 here over-estimates the gap by ~10% on 8N1 (harmless).
_BITS_PER_CHARACTER = 11

# Broadcasts only carry write FCs (*serial §2.1*: "broadcast requests are
# necessarily writing commands").
_BROADCAST_ELIGIBLE_FCS: frozenset[int] = frozenset(
    {
        FunctionCode.WRITE_SINGLE_COIL,
        FunctionCode.WRITE_SINGLE_REGISTER,
        FunctionCode.WRITE_MULTIPLE_COILS,
        FunctionCode.WRITE_MULTIPLE_REGISTERS,
    }
)

_BROADCAST_ADDRESS = 0

# Largest single read while discarding a late reply: one full RTU ADU.
_DISCARD_CHUNK_BYTES = 256

# Exceptions a stream raises for a failed port. They are translated into
# ``ModbusError`` subclasses by ``Bus._stream_error``. ``OSError`` covers
# ``anyserial.SerialError`` and its subclasses.
_STREAM_ERRORS: Final = (
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    OSError,
)


def _t35_for_baud(baudrate: int) -> float:
    """Return the t3.5 inter-frame idle gap in seconds for ``baudrate``."""
    return max(3.5 * _BITS_PER_CHARACTER / baudrate, _T35_FLOOR_SECONDS)


def _t15_for_baud(baudrate: int) -> float:
    """Return the t1.5 inter-character idle gap in seconds for ``baudrate``."""
    return max(1.5 * _BITS_PER_CHARACTER / baudrate, _T15_FLOOR_SECONDS)


def _stream_baudrate(stream: anyio.abc.ByteStream) -> int:
    """Look up the stream's current baudrate via the AnyIO typed-attribute API.

    Falls back to :data:`_FALLBACK_BAUD_FOR_TIMING` when the stream does not
    publish :class:`SerialStreamAttribute.config` (test pairs over memory
    streams, wrappers that do not forward ``extra_attributes``, future TCP).
    A :class:`SerialPort` always publishes it.
    """
    if SerialStreamAttribute is None:  # pragma: no cover — hard dep.
        return _FALLBACK_BAUD_FOR_TIMING
    cfg = stream.extra(SerialStreamAttribute.config, default=None)  # noqa: S610 — anyio TypedAttribute lookup, not Django ORM
    if cfg is None:
        return _FALLBACK_BAUD_FOR_TIMING
    return int(cfg.baudrate)


def _has_async_method(obj: object, name: str) -> bool:
    """Return whether ``obj.<name>`` exists and is a coroutine function."""
    return inspect.iscoroutinefunction(getattr(obj, name, None))


def _classify(error: BaseException | None, *, broadcast: bool) -> TransactionOutcome:
    """Map how an attempt ended onto a :class:`TransactionOutcome`."""
    if error is None:
        return TransactionOutcome.BROADCAST_SENT if broadcast else TransactionOutcome.REPLY
    if not isinstance(error, Exception):
        # Cancellation (asyncio.CancelledError, trio.Cancelled) and
        # KeyboardInterrupt / SystemExit are BaseException, not Exception.
        return TransactionOutcome.CANCELLED
    outcome = TransactionOutcome.ERROR
    if isinstance(error, ModbusExceptionResponse):
        outcome = TransactionOutcome.EXCEPTION_REPLY
    elif isinstance(error, FrameTimeoutError):
        outcome = TransactionOutcome.TIMEOUT
    elif isinstance(error, ChecksumError):
        outcome = TransactionOutcome.CHECKSUM_ERROR
    elif isinstance(error, UnexpectedResponseError):
        outcome = TransactionOutcome.UNEXPECTED_RESPONSE
    elif isinstance(error, ProtocolError):
        outcome = TransactionOutcome.FRAME_ERROR
    elif isinstance(error, (ConnectionLostError, BusClosedError)):
        outcome = TransactionOutcome.CONNECTION_ERROR
    return outcome


def _call_observer(observer: TransactionObserver, info: TransactionInfo) -> None:
    """Call one observer, logging and swallowing anything it raises."""
    try:
        observer(info)
    except Exception:
        _LOGGER.exception("Transaction observer %r raised; ignoring it", observer)


@dataclass(slots=True)
class _Attempt:
    """What is known about one attempt so far; filled in as it progresses."""

    started_at: float
    request_on_wire: bool = False
    sent_at: float | None = None
    sent_at_ns: int | None = None
    ended_at: float = 0.0
    ended_at_ns: int = 0
    discarded_bytes: int = 0


class Bus:
    """Single-master, half-duplex Modbus RTU client over an arbitrary byte stream.

    Construction does not touch the wire. Use :func:`anymodbus.open_modbus_rtu`
    when you also want to open a serial port; instantiate directly when you
    have a stream already (test pair, future TCP, etc.).

    The :class:`Bus` is an async context manager — entering yields ``self``,
    exiting closes the underlying stream. Concurrent transactions on the same
    bus serialize via an internal :class:`anyio.Lock`; concurrent buses
    (different streams) run in parallel as expected.

    The stream may be any :class:`anyio.abc.ByteStream`. When it also has an
    async ``drain()`` and/or ``reset_input_buffer()`` method (see
    :class:`anymodbus.stream.SupportsDrain` and
    :class:`anymodbus.stream.SupportsResetInputBuffer`), the bus uses them, so
    a wrapper around a serial port keeps drain-after-send and the input reset
    by forwarding them.

    Args:
        stream: The byte stream to drive.
        config: Bus configuration. Defaults to :class:`BusConfig()`.
        framing: Wire framing, RTU (default) or ASCII.
        on_transaction: Optional transaction observer, registered as if by
            :meth:`add_transaction_observer`.
    """

    __slots__ = (
        "_can_drain",
        "_can_reset_input",
        "_closed",
        "_config",
        "_framer",
        "_framing",
        "_inter_char_idle",
        "_inter_frame_idle",
        "_last_io_monotonic",
        "_lock",
        "_observers",
        "_quiet_until",
        "_request_count",
        "_startup_settled",
        "_stream",
        "_stream_resolved",
    )

    def __init__(
        self,
        stream: anyio.abc.ByteStream,
        *,
        config: BusConfig | None = None,
        framing: Framing = Framing.RTU,
        on_transaction: TransactionObserver | None = None,
    ) -> None:
        self._stream = stream
        self._config: BusConfig = config if config is not None else BusConfig()
        self._framing: Framing = framing
        self._framer: Framer = get_framer(framing)
        self._lock = anyio.Lock()
        self._last_io_monotonic: float = 0.0
        self._startup_settled = False
        self._closed = False
        # End of the late-reply window opened by the last uncertain attempt
        # (AnyIO clock), or None when no window is pending.
        self._quiet_until: float | None = None
        self._request_count = 0
        self._observers: list[TransactionObserver] = []
        if on_transaction is not None:
            self._observers.append(on_transaction)
        # Lazy-resolved on first use; the stream may not have its config set
        # at __init__ time (e.g. it gets reconfigured before first I/O).
        self._inter_frame_idle: float = 0.0
        self._inter_char_idle: float = 0.0
        self._can_drain = False
        self._can_reset_input = False
        self._stream_resolved = False

    @property
    def stream(self) -> anyio.abc.ByteStream:
        """The underlying byte stream this bus drives. Read-only inspection only.

        Exposed for diagnostics (logging, attribute lookups, type checks).
        Do **not** call :meth:`send` / :meth:`receive` on it directly: that
        bypasses the bus lock, the inter-frame timing, and the framer, and
        will corrupt any concurrent transaction. Use the high-level
        :class:`Slave` and broadcast methods for I/O.
        """
        return self._stream

    @property
    def config(self) -> BusConfig:
        """Active :class:`BusConfig`."""
        return self._config

    @property
    def framing(self) -> Framing:
        """The wire framing this bus speaks (:attr:`Framing.RTU` or ``ASCII``)."""
        return self._framing

    @property
    def is_open(self) -> bool:
        """Whether :meth:`aclose` has been called on this bus.

        ``True`` does **not** guarantee the underlying stream is still
        connected — a serial cable can be unplugged mid-session, in which
        case the next transaction raises :class:`ConnectionLostError`. Use
        this to detect explicit ``close``, not liveness.
        """
        return not self._closed

    def slave(self, address: int) -> Slave:
        """Return a per-slave handle for ``address`` (1-247 for unicast).

        Address 0 is the broadcast address — broadcasts go through
        :meth:`broadcast_write_coil` / :meth:`broadcast_write_register` /
        :meth:`broadcast_write_coils` / :meth:`broadcast_write_registers`,
        which guarantee callers can't accidentally broadcast a read FC.
        Addresses 248-255 are reserved by the spec.
        """
        return Slave(self, address)

    def add_transaction_observer(self, observer: TransactionObserver) -> Callable[[], None]:
        """Call ``observer`` with a :class:`TransactionInfo` after every attempt.

        One call per attempt: a request that is retried reports each attempt,
        and a broadcast reports once. It runs synchronously, with the bus lock
        held, as each attempt ends (also when it ends by cancellation), so it
        must be quick and must not block or wait on the bus. Anything it
        raises is logged and ignored. Observers run in the order they were
        added.

        Calls that end before an attempt starts are not reported: a closed
        bus, a bad argument, or cancellation while waiting for the lock.

        Returns:
            A function that removes ``observer`` again. Calling it more than
            once is harmless.
        """
        self._observers.append(observer)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._observers.remove(observer)

        return remove

    async def aclose(self) -> None:
        """Close the bus and the underlying stream. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            await self._stream.aclose()

    async def __aenter__(self) -> Self:
        """Return ``self`` so ``async with`` expressions can bind the bus."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the bus on exit from the ``async with`` block."""
        await self.aclose()

    # ------------------------------------------------------------------
    # Public broadcast methods. Per DESIGN §6.6, only write FCs broadcast.
    # ------------------------------------------------------------------

    async def broadcast_write_coil(self, address: int, *, on: bool) -> None:
        """FC 0x05 — Broadcast Write Single Coil to slave address 0."""
        pdu = encode_write_single_coil_request(address, on=on)
        await self._broadcast(request_pdu=pdu)

    async def broadcast_write_register(self, address: int, value: int) -> None:
        """FC 0x06 — Broadcast Write Single Register to slave address 0."""
        pdu = encode_write_single_register_request(address, value)
        await self._broadcast(request_pdu=pdu)

    async def broadcast_write_coils(self, address: int, values: Sequence[bool]) -> None:
        """FC 0x0F — Broadcast Write Multiple Coils to slave address 0."""
        pdu = encode_write_multiple_coils_request(address, values)
        await self._broadcast(request_pdu=pdu)

    async def broadcast_write_registers(self, address: int, values: Sequence[int]) -> None:
        """FC 0x10 — Broadcast Write Multiple Registers to slave address 0."""
        pdu = encode_write_multiple_registers_request(address, values)
        await self._broadcast(request_pdu=pdu)

    # ------------------------------------------------------------------
    # Internal — invoked by Slave methods, not user code.
    # ------------------------------------------------------------------

    async def _txn[T](
        self,
        *,
        slave_address: int,
        request_pdu: bytes,
        expected_function_code: FunctionCode,
        decode: Callable[[bytes], T],
    ) -> T:
        """Run one full request/response transaction; return ``decode(response_pdu)``.

        Holds the bus lock for the full duration; concurrent callers wait.
        Applies inter-frame timing, the late-reply window, the retry policy,
        and length-aware framing. ``decode`` runs inside each attempt, so a
        reply it rejects (for example a register count that differs from the
        request's) is retried, opens a late-reply window, and is reported to
        observers like any other bad reply.
        """
        if self._closed:
            msg = "bus is closed"
            raise BusClosedError(msg)
        if slave_address == _BROADCAST_ADDRESS:
            # Broadcasts have no response; routing them through _txn would
            # block on rx forever. Force the caller to use the broadcast API.
            msg = (
                "slave_address=0 is the broadcast address; use Bus.broadcast_* "
                "methods instead of routing through Slave"
            )
            raise ConfigurationError(msg)

        retry_policy = self._config.retries
        max_attempts = retry_policy.retries + 1
        # ``isinstance`` requires a tuple of classes; ``retry_on`` is a
        # frozenset on the public API, so cast once outside the loop.
        retry_on_classes: tuple[type[ModbusError], ...] = tuple(retry_policy.retry_on)
        adu = self._framer.encode_adu(slave_address=slave_address, pdu=request_pdu)

        async with self._lock:
            self._ensure_stream_resolved()
            request_id = self._next_request_id()
            for attempt in range(max_attempts):
                record = _Attempt(started_at=anyio.current_time())
                error: BaseException | None = None
                will_retry = False
                try:
                    return await self._one_txn(
                        adu=adu,
                        slave_address=slave_address,
                        expected_function_code=expected_function_code,
                        decode=decode,
                        record=record,
                    )
                except ModbusError as exc:
                    error = exc
                    will_retry = self._should_retry(
                        exc,
                        retry_on_classes,
                        expected_function_code,
                        attempt,
                        max_attempts,
                    )
                    if not will_retry:
                        raise
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    self._finish_attempt(
                        record,
                        request_id=request_id,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        slave_address=slave_address,
                        function_code=int(expected_function_code),
                        error=error,
                        will_retry=will_retry,
                    )
                _LOGGER.warning(
                    "Transient error on attempt %d/%d (fc=0x%02x slave=0x%02x): %s",
                    attempt + 1,
                    max_attempts,
                    int(expected_function_code),
                    slave_address,
                    error,
                )
                await anyio.sleep(self._inter_frame_idle + retry_policy.backoff_base)
            # The loop ends only by returning or re-raising above; this is
            # unreachable but mypy/pyright want a terminator.
            msg = "retry loop ended without a result"  # pragma: no cover
            raise AssertionError(msg)  # pragma: no cover

    async def _broadcast(self, *, request_pdu: bytes) -> None:
        """Send a broadcast request (slave address 0). No response expected."""
        if self._closed:
            msg = "bus is closed"
            raise BusClosedError(msg)
        if not request_pdu:
            msg = "request_pdu must not be empty"
            raise ConfigurationError(msg)
        fc_byte = request_pdu[0]
        if fc_byte not in _BROADCAST_ELIGIBLE_FCS:
            # *serial §2.1*: broadcasts are write-only. Reads, mask write,
            # and read/write multiple are caller errors caught synchronously.
            msg = (
                f"FC {fc_byte:#04x} is not broadcast-eligible; only "
                f"FC 0x05/0x06/0x0F/0x10 may be broadcast"
            )
            raise ConfigurationError(msg)

        adu = self._framer.encode_adu(slave_address=_BROADCAST_ADDRESS, pdu=request_pdu)
        async with self._lock:
            self._ensure_stream_resolved()
            request_id = self._next_request_id()
            record = _Attempt(started_at=anyio.current_time())
            error: BaseException | None = None
            try:
                await self._send_broadcast(adu=adu, record=record)
            except BaseException as exc:
                error = exc
                raise
            finally:
                self._finish_attempt(
                    record,
                    request_id=request_id,
                    attempt=0,
                    max_attempts=1,
                    slave_address=_BROADCAST_ADDRESS,
                    function_code=fc_byte,
                    error=error,
                    will_retry=False,
                    broadcast=True,
                )

    # ------------------------------------------------------------------
    # Private helpers.
    # ------------------------------------------------------------------

    def _ensure_stream_resolved(self) -> None:
        """Resolve ``"auto"`` timing and the stream's optional capabilities.

        Cached after the first call to avoid repeated typed-attribute lookups
        on every transaction. Only *whether* the stream has ``drain`` /
        ``reset_input_buffer`` is cached; the methods themselves are looked up
        on each use.
        """
        if self._stream_resolved:
            return
        timing = self._config.timing
        baud = _stream_baudrate(self._stream)
        if isinstance(timing.inter_frame_idle, (int, float)):
            self._inter_frame_idle = float(timing.inter_frame_idle)
        else:
            self._inter_frame_idle = _t35_for_baud(baud)
        if isinstance(timing.inter_char_idle, (int, float)):
            self._inter_char_idle = float(timing.inter_char_idle)
        else:
            self._inter_char_idle = _t15_for_baud(baud)
        self._can_drain = _has_async_method(self._stream, "drain")
        self._can_reset_input = _has_async_method(self._stream, "reset_input_buffer")
        self._stream_resolved = True

    def _next_request_id(self) -> int:
        self._request_count += 1
        return self._request_count

    async def _await_line_idle(self) -> int:
        """Wait until the next frame may be sent; return the late bytes discarded meanwhile.

        Waits out, in order: the late-reply window left by an uncertain
        attempt (reading and dropping anything that arrives), the one-shot
        startup settle before the very first frame, and the inter-frame idle
        gap since the last I/O.
        """
        discarded = 0
        if self._quiet_until is not None:
            discarded = await self._discard_late_bytes(self._quiet_until)
            self._quiet_until = None
        await self._await_inter_frame_gap()
        return discarded

    async def _discard_late_bytes(self, quiet_until: float) -> int:
        """Read and drop bytes until ``quiet_until`` has passed and the line is idle.

        The line counts as idle once nothing has arrived for the inter-frame
        gap (at least the spec's 1.75 ms floor), watched from when this call
        starts, so bytes that arrived before it (a late reply to a request
        that timed out long ago) are read and dropped too. The wait is capped
        at ``request_timeout`` past the later of ``quiet_until`` and the start
        of this call: a line that is still busy then is logged and the
        request goes out anyway.
        """
        idle = max(self._inter_frame_idle, _T35_FLOOR_SECONDS)
        quiet_since = anyio.current_time()
        limit = max(quiet_until, quiet_since) + self._config.request_timeout
        discarded = bytearray()
        while True:
            now = anyio.current_time()
            until = min(max(quiet_until, quiet_since + idle), limit)
            if now >= until:
                break
            chunk = b""
            with anyio.move_on_after(until - now):
                chunk = await self._stream.receive(_DISCARD_CHUNK_BYTES)
            if chunk:
                discarded += chunk
                quiet_since = anyio.current_time()
                self._last_io_monotonic = quiet_since
        if discarded:
            _LOGGER.info("Discarded %d late byte(s) before the next request", len(discarded))
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("rx (discarded) %s", discarded.hex())
        if quiet_since + idle > limit:
            _LOGGER.warning(
                "Line still busy %.3fs after the late-reply window; sending anyway",
                self._config.request_timeout,
            )
        return len(discarded)

    async def _await_inter_frame_gap(self) -> None:
        """Sleep until at least ``inter_frame_idle`` seconds since the last I/O."""
        if self._last_io_monotonic == 0.0:
            # First transaction on this bus. The wire has been idle for far
            # longer than t3.5 already, so no inter-frame gap is needed — but a
            # freshly-opened RS485 link may want a one-shot startup settle to
            # absorb adapter / receiver warm-up before the first frame.
            if not self._startup_settled:
                self._startup_settled = True
                startup = self._config.timing.startup_settle
                if startup > 0:
                    await anyio.sleep(startup)
            return
        elapsed = anyio.current_time() - self._last_io_monotonic
        if elapsed < self._inter_frame_idle:
            await anyio.sleep(self._inter_frame_idle - elapsed)

    async def _maybe_drain(self) -> None:
        """If the stream can drain, await its kernel-output drain.

        Important for RS-485 RTS-toggle correctness: we need the kernel to
        have actually pushed every byte before we start listening. For
        streams without an async ``drain()`` (e.g. memory-stream test pairs)
        this is a no-op.
        """
        if self._config.drain_after_send and self._can_drain:
            await cast("SupportsDrain", self._stream).drain()

    async def _maybe_reset_input(self) -> None:
        """Discard any junk in the rx buffer left over from a previous error."""
        if self._config.reset_input_buffer_before_request and self._can_reset_input:
            await cast("SupportsResetInputBuffer", self._stream).reset_input_buffer()

    def _stamp_end(self, record: _Attempt) -> None:
        """Record the end of an attempt, however it ended.

        The next frame's idle gap runs from here: after a reply, an exception
        response, a checksum or framing error, a timeout, or a cancellation.
        """
        now = anyio.current_time()
        self._last_io_monotonic = now
        record.ended_at = now
        record.ended_at_ns = time.monotonic_ns()

    def _stream_error(self, exc: BaseException, *, phase: str) -> ModbusError:
        """Translate an exception the stream raised into the matching :class:`ModbusError`."""
        if isinstance(exc, anyio.ClosedResourceError):
            # Includes anyserial's SerialClosedError.
            self._closed = True
            return BusClosedError(f"bus stream was closed while {phase}")
        if isinstance(exc, anyio.BrokenResourceError):
            # Includes anyserial's SerialDisconnectedError.
            return ConnectionLostError(f"stream disconnected while {phase}: {exc}")
        if isinstance(exc, anyio.EndOfStream):
            return ConnectionLostError(f"stream ended while {phase}")
        msg = f"stream failed while {phase}: {exc}"
        if isinstance(exc, OSError) and exc.errno is not None:
            return TransportError(exc.errno, msg)
        return TransportError(msg)

    async def _one_txn[T](
        self,
        *,
        adu: bytes,
        slave_address: int,
        expected_function_code: FunctionCode,
        decode: Callable[[bytes], T],
        record: _Attempt,
    ) -> T:
        """One request/response attempt. Caller holds the lock."""
        phase = "waiting for the line to be idle"
        try:
            record.discarded_bytes = await self._await_line_idle()
            phase = "resetting the input buffer"
            await self._maybe_reset_input()
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("tx %s", adu.hex())
            phase = "sending the request"
            record.request_on_wire = True
            await self._stream.send(adu)
            phase = "draining the output"
            await self._maybe_drain()
            record.sent_at = anyio.current_time()
            record.sent_at_ns = time.monotonic_ns()
            # Some RS-485 transceivers need a settling delay between RTS
            # de-assert and starting to listen; honour it before the rx wait.
            if self._config.timing.post_tx_settle > 0:
                await anyio.sleep(self._config.timing.post_tx_settle)
            phase = "reading the reply"
            try:
                with anyio.fail_after(self._config.request_timeout):
                    # Framer reads one raw frame; the shared interpreter applies
                    # framing-agnostic FC semantics (D1), and ``decode`` checks
                    # the reply against the request. Both run outside the
                    # read's fail_after (no I/O) but inside the attempt.
                    slave, raw_pdu = await self._framer.read_adu(
                        self._stream,
                        expected_slave_address=slave_address,
                        inter_char_idle=self._inter_char_idle,
                    )
            except TimeoutError as e:
                msg = (
                    f"no response from slave 0x{slave_address:02x} for fc "
                    f"0x{int(expected_function_code):02x} within "
                    f"{self._config.request_timeout}s"
                )
                raise FrameTimeoutError(msg) from e
            _, response_pdu = interpret_response_pdu(
                slave_address=slave,
                pdu=raw_pdu,
                expected_function_code=expected_function_code,
            )
            return decode(response_pdu)
        except ModbusError:
            # Already translated. FrameTimeoutError is a TimeoutError, and so
            # an OSError: it must not reach the clause below.
            raise
        except _STREAM_ERRORS as exc:
            raise self._stream_error(exc, phase=phase) from exc
        finally:
            self._stamp_end(record)

    async def _send_broadcast(self, *, adu: bytes, record: _Attempt) -> None:
        """Send one broadcast ADU and hold the turnaround delay. Caller holds the lock."""
        phase = "waiting for the line to be idle"
        try:
            record.discarded_bytes = await self._await_line_idle()
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("tx (broadcast) %s", adu.hex())
            phase = "sending the broadcast"
            record.request_on_wire = True
            await self._stream.send(adu)
            phase = "draining the output"
            await self._maybe_drain()
            record.sent_at = anyio.current_time()
            record.sent_at_ns = time.monotonic_ns()
            # *serial §2.4.1*: the master must hold the bus idle for the
            # turnaround delay so every slave finishes processing before the
            # next transaction. The lock is held across the sleep, blocking
            # any unicast follow-up that might otherwise preempt slaves.
            phase = "holding the broadcast turnaround"
            await anyio.sleep(self._config.timing.broadcast_turnaround)
        except ModbusError:
            raise
        except _STREAM_ERRORS as exc:
            raise self._stream_error(exc, phase=phase) from exc
        finally:
            # Stamp even when cancelled mid-send or mid-turnaround, so the
            # next frame still gets its idle gap.
            self._stamp_end(record)

    def _finish_attempt(
        self,
        record: _Attempt,
        *,
        request_id: int,
        attempt: int,
        max_attempts: int,
        slave_address: int,
        function_code: int,
        error: BaseException | None,
        will_retry: bool,
        broadcast: bool = False,
    ) -> None:
        """Close out one attempt: open a late-reply window if needed, then notify observers.

        A reply that decoded cleanly, or a Modbus exception response, leaves
        the line in a known state. Anything else, once the request has
        started going out, may leave a reply on its way, so the next frame
        waits for :attr:`TimingConfig.late_reply_window`. Broadcasts get no
        reply and open no window.
        """
        window = self._config.timing.late_reply_window
        certain = error is None or isinstance(error, ModbusExceptionResponse)
        if window > 0 and not broadcast and not certain and record.request_on_wire:
            until = record.ended_at + window
            if self._quiet_until is None or until > self._quiet_until:
                self._quiet_until = until
        if not self._observers:
            return
        outcome = _classify(error, broadcast=broadcast)
        info = TransactionInfo(
            request_id=request_id,
            attempt=attempt + 1,
            max_attempts=max_attempts,
            will_retry=will_retry,
            slave_address=slave_address,
            function_code=function_code,
            outcome=outcome,
            error=error if isinstance(error, Exception) else None,
            started_at=record.started_at,
            sent_at=record.sent_at,
            ended_at=record.ended_at,
            sent_at_ns=record.sent_at_ns,
            ended_at_ns=record.ended_at_ns,
            discarded_bytes=record.discarded_bytes,
        )
        for observer in tuple(self._observers):
            _call_observer(observer, info)

    def _should_retry(
        self,
        exc: ModbusError,
        retry_on_classes: tuple[type[ModbusError], ...],
        function_code: FunctionCode,
        attempt: int,
        max_attempts: int,
    ) -> bool:
        """Decide whether ``exc`` warrants another attempt.

        Honors :attr:`RetryPolicy.retry_on` (passed as a pre-built tuple in
        ``retry_on_classes`` so the hot path doesn't rebuild it every loop)
        and :attr:`RetryPolicy.retry_idempotent_only`. Modbus exception
        responses are intentionally absent from the default ``retry_on`` set
        — the slave told us no, retrying won't change its mind.
        """
        if attempt + 1 >= max_attempts:
            return False
        if not isinstance(exc, retry_on_classes):
            return False
        retry = self._config.retries
        return not (retry.retry_idempotent_only and not is_idempotent_function(function_code))


__all__ = ["Bus"]
