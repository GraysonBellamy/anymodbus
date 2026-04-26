"""The :class:`Bus` — single-master, half-duplex Modbus RTU client.

A :class:`Bus` owns the underlying byte stream, an :class:`anyio.Lock` that
serializes transactions, and the timing/retry state. Calls to
:meth:`Slave.read_*` / :meth:`Slave.write_*` flow through ``Bus._txn``,
which:

1. Acquires the bus lock.
2. Enforces the inter-frame idle gap (3.5 char-times).
3. Optionally flushes the rx buffer.
4. Writes the ADU and (optionally) drains.
5. Reads the response with the length-aware framer.
6. Decodes the PDU and returns the domain payload.
7. On transient transport errors (CRC/timeout), applies the retry policy.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

import anyio
import anyio.abc

try:
    from anyserial import SerialPort, SerialStreamAttribute
except ImportError:  # pragma: no cover — anyserial is a hard dep, but be defensive.
    SerialPort = None  # type: ignore[assignment, misc]
    SerialStreamAttribute = None  # type: ignore[assignment, misc]

from anymodbus._types import FunctionCode, is_idempotent_function
from anymodbus.config import BusConfig
from anymodbus.exceptions import (
    BusClosedError,
    ConfigurationError,
    ConnectionLostError,
    FrameTimeoutError,
    ModbusError,
)
from anymodbus.framer import encode_adu, read_response_adu
from anymodbus.pdu import (
    encode_write_multiple_coils_request,
    encode_write_multiple_registers_request,
    encode_write_single_coil_request,
    encode_write_single_register_request,
)
from anymodbus.slave import Slave

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

_LOGGER = logging.getLogger("anymodbus.bus")

# Fallback baudrate when the underlying stream is not a serial port (e.g.
# a memory-stream test pair). 19200 is the spec's reference baud and yields
# the documented 1.75 ms / 0.75 ms timing floors.
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


def _t35_for_baud(baudrate: int) -> float:
    """Return the t3.5 inter-frame idle gap in seconds for ``baudrate``."""
    return max(3.5 * _BITS_PER_CHARACTER / baudrate, _T35_FLOOR_SECONDS)


def _t15_for_baud(baudrate: int) -> float:
    """Return the t1.5 inter-character idle gap in seconds for ``baudrate``."""
    return max(1.5 * _BITS_PER_CHARACTER / baudrate, _T15_FLOOR_SECONDS)


def _stream_baudrate(stream: anyio.abc.ByteStream) -> int:
    """Look up the stream's current baudrate via the AnyIO typed-attribute API.

    Falls back to :data:`_FALLBACK_BAUD_FOR_TIMING` when the stream isn't a
    serial port (test pairs, future TCP). Tests cover the fallback path; on
    real hardware the lookup always succeeds because :class:`SerialPort`
    publishes :class:`SerialStreamAttribute.config`.
    """
    if SerialStreamAttribute is None:  # pragma: no cover — hard dep.
        return _FALLBACK_BAUD_FOR_TIMING
    cfg = stream.extra(SerialStreamAttribute.config, default=None)  # noqa: S610 — anyio TypedAttribute lookup, not Django ORM
    if cfg is None:
        return _FALLBACK_BAUD_FOR_TIMING
    return int(cfg.baudrate)


class Bus:
    """Single-master, half-duplex Modbus RTU client over an arbitrary byte stream.

    Construction does not touch the wire. Use :func:`anymodbus.open_modbus_rtu`
    when you also want to open a serial port; instantiate directly when you
    have a stream already (test pair, future TCP, etc.).

    The :class:`Bus` is an async context manager — entering yields ``self``,
    exiting closes the underlying stream. Concurrent transactions on the same
    bus serialize via an internal :class:`anyio.Lock`; concurrent buses
    (different streams) run in parallel as expected.
    """

    __slots__ = (
        "_closed",
        "_config",
        "_inter_char_idle",
        "_inter_frame_idle",
        "_last_io_monotonic",
        "_lock",
        "_stream",
        "_timing_resolved",
    )

    def __init__(
        self,
        stream: anyio.abc.ByteStream,
        *,
        config: BusConfig | None = None,
    ) -> None:
        self._stream = stream
        self._config: BusConfig = config if config is not None else BusConfig()
        self._lock = anyio.Lock()
        self._last_io_monotonic: float = 0.0
        self._closed = False
        # Lazy-resolved on first use; the stream may not have its config set
        # at __init__ time (e.g. it gets reconfigured before first I/O).
        self._inter_frame_idle: float = 0.0
        self._inter_char_idle: float = 0.0
        self._timing_resolved = False

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

    async def _txn(
        self,
        *,
        slave_address: int,
        request_pdu: bytes,
        expected_function_code: FunctionCode,
    ) -> bytes:
        """Run one full request/response transaction. Returns the response PDU.

        Holds the bus lock for the full duration; concurrent callers wait.
        Applies inter-frame timing, retry policy, and length-aware framing.
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

        async with self._lock:
            self._ensure_timing_resolved()
            last_error: ModbusError | None = None
            for attempt in range(max_attempts):
                try:
                    return await self._one_txn(
                        slave_address=slave_address,
                        request_pdu=request_pdu,
                        expected_function_code=expected_function_code,
                    )
                except ModbusError as exc:
                    if not self._should_retry(
                        exc,
                        retry_on_classes,
                        expected_function_code,
                        attempt,
                        max_attempts,
                    ):
                        raise
                    last_error = exc
                    _LOGGER.warning(
                        "Transient error on attempt %d/%d (fc=0x%02x slave=0x%02x): %s",
                        attempt + 1,
                        max_attempts,
                        int(expected_function_code),
                        slave_address,
                        exc,
                    )
                    await anyio.sleep(self._inter_frame_idle + retry_policy.backoff_base)
            # Loop exits only via successful return or re-raise above; this
            # is unreachable but mypy/pyright want a terminator.
            assert last_error is not None
            raise last_error

    async def _broadcast(self, *, request_pdu: bytes) -> None:
        """Send a broadcast request (slave address 0). No response expected."""
        if self._closed:
            msg = "bus is closed"
            raise BusClosedError(msg)
        if not request_pdu:
            msg = "request_pdu must not be empty"
            raise ValueError(msg)
        fc_byte = request_pdu[0]
        if fc_byte not in _BROADCAST_ELIGIBLE_FCS:
            # *serial §2.1*: broadcasts are write-only. Reads, mask write,
            # and read/write multiple are caller errors caught synchronously.
            msg = (
                f"FC {fc_byte:#04x} is not broadcast-eligible; only "
                f"FC 0x05/0x06/0x0F/0x10 may be broadcast"
            )
            raise ValueError(msg)

        adu = encode_adu(slave_address=_BROADCAST_ADDRESS, pdu=request_pdu)
        async with self._lock:
            self._ensure_timing_resolved()
            await self._await_inter_frame_gap()
            try:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("tx (broadcast) %s", adu.hex())
                await self._stream.send(adu)
                await self._maybe_drain()
            except anyio.BrokenResourceError as e:
                msg = f"stream disconnected during broadcast tx: {e}"
                raise ConnectionLostError(msg) from e
            except anyio.ClosedResourceError as e:
                self._closed = True
                msg = "bus stream was closed during broadcast tx"
                raise BusClosedError(msg) from e

            # *serial §2.4.1*: the master must hold the bus idle for the
            # turnaround delay so every slave finishes processing before the
            # next transaction. The lock is held across the sleep, blocking
            # any unicast follow-up that might otherwise preempt slaves.
            await anyio.sleep(self._config.timing.broadcast_turnaround)
            self._last_io_monotonic = anyio.current_time()

    # ------------------------------------------------------------------
    # Private helpers.
    # ------------------------------------------------------------------

    def _ensure_timing_resolved(self) -> None:
        """Resolve ``"auto"`` timing fields against the stream's current baud.

        Cached after the first call to avoid repeated typed-attribute
        lookups on every transaction.
        """
        if self._timing_resolved:
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
        self._timing_resolved = True

    async def _await_inter_frame_gap(self) -> None:
        """Sleep until at least ``inter_frame_idle`` seconds since the last I/O."""
        if self._last_io_monotonic == 0.0:
            # First transaction on this bus — assume the wire has been idle
            # for far longer than t3.5 already.
            return
        elapsed = anyio.current_time() - self._last_io_monotonic
        if elapsed < self._inter_frame_idle:
            await anyio.sleep(self._inter_frame_idle - elapsed)

    async def _maybe_drain(self) -> None:
        """If the stream is a :class:`SerialPort`, await its kernel-output drain.

        Important for RS-485 RTS-toggle correctness: we need the kernel to
        have actually pushed every byte before we start listening. For
        non-serial streams (e.g. test pairs) this is a no-op.
        """
        if (
            self._config.drain_after_send
            and SerialPort is not None
            and isinstance(self._stream, SerialPort)
        ):
            await self._stream.drain()

    async def _maybe_reset_input(self) -> None:
        """Discard any junk in the rx buffer left over from a previous error."""
        if (
            self._config.reset_input_buffer_before_request
            and SerialPort is not None
            and isinstance(self._stream, SerialPort)
        ):
            await self._stream.reset_input_buffer()

    async def _one_txn(
        self,
        *,
        slave_address: int,
        request_pdu: bytes,
        expected_function_code: FunctionCode,
    ) -> bytes:
        """One request/response attempt. Caller holds the lock."""
        await self._await_inter_frame_gap()
        await self._maybe_reset_input()

        adu = encode_adu(slave_address=slave_address, pdu=request_pdu)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("tx %s", adu.hex())

        try:
            await self._stream.send(adu)
            await self._maybe_drain()
            self._last_io_monotonic = anyio.current_time()
            # Some RS-485 transceivers need a settling delay between RTS
            # de-assert and starting to listen; honour it before the rx wait.
            if self._config.timing.post_tx_settle > 0:
                await anyio.sleep(self._config.timing.post_tx_settle)
            try:
                with anyio.fail_after(self._config.request_timeout):
                    _, response_pdu = await read_response_adu(
                        self._stream,
                        expected_slave_address=slave_address,
                        expected_function_code=expected_function_code,
                        inter_char_idle=self._inter_char_idle,
                    )
            except TimeoutError as e:
                msg = (
                    f"no response from slave 0x{slave_address:02x} for fc "
                    f"0x{int(expected_function_code):02x} within "
                    f"{self._config.request_timeout}s"
                )
                raise FrameTimeoutError(msg) from e
        except anyio.BrokenResourceError as e:
            msg = f"stream disconnected during transaction: {e}"
            raise ConnectionLostError(msg) from e
        except anyio.ClosedResourceError as e:
            self._closed = True
            msg = "bus stream was closed during transaction"
            raise BusClosedError(msg) from e

        self._last_io_monotonic = anyio.current_time()
        return response_pdu

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
