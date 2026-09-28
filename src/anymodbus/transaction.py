"""Transaction events: what happened on the wire for each request attempt.

A :class:`Bus` reports every attempt it makes to the observers registered with
:meth:`Bus.add_transaction_observer` (or passed as ``Bus(on_transaction=...)``).
Each report is a :class:`TransactionInfo`: which slave and function code, when
the request went out and when the attempt ended, how it ended, and whether the
bus is about to retry.

Typical uses are timestamping readings with the moment the request actually
left (after the inter-frame gap and any late-reply window), and counting
failures and recovered errors without turning off the bus's own retries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class TransactionOutcome(StrEnum):
    """How one attempt ended.

    ``REPLY``, ``EXCEPTION_REPLY`` and ``BROADCAST_SENT`` are the outcomes whose
    effect on the line is certain. Every other outcome may leave a reply still
    on its way; see :attr:`anymodbus.TimingConfig.late_reply_window`.
    """

    #: A reply that matched the request and decoded cleanly.
    REPLY = "reply"
    #: A checksum-valid Modbus exception response (``ModbusExceptionResponse``).
    EXCEPTION_REPLY = "exception_reply"
    #: A broadcast was sent and its turnaround delay elapsed (no reply expected).
    BROADCAST_SENT = "broadcast_sent"
    #: No complete reply within ``request_timeout`` (``FrameTimeoutError``).
    TIMEOUT = "timeout"
    #: A complete frame whose CRC or LRC did not verify (``ChecksumError``).
    CHECKSUM_ERROR = "checksum_error"
    #: A checksum-valid reply that does not answer this request: another
    #: function code, another register count, or a write echo that differs
    #: (``UnexpectedResponseError``).
    UNEXPECTED_RESPONSE = "unexpected_response"
    #: Any other malformed reply (``FrameError`` or another ``ProtocolError``).
    FRAME_ERROR = "frame_error"
    #: The stream failed or was closed (``ConnectionLostError``, including
    #: ``TransportError``, or ``BusClosedError``).
    CONNECTION_ERROR = "connection_error"
    #: The attempt was cancelled or interrupted before it finished.
    CANCELLED = "cancelled"
    #: Any other exception raised inside the attempt.
    ERROR = "error"


@dataclass(frozen=True, slots=True, kw_only=True)
class TransactionInfo:
    """One attempt at one request, as reported to transaction observers.

    Times come on two clocks. The ``*_at`` fields use the AnyIO clock
    (:func:`anyio.current_time`), which is the clock the bus schedules its
    gaps and deadlines on. The ``*_at_ns`` fields use :func:`time.monotonic_ns`,
    which is comparable with timestamps taken elsewhere in the process (trio's
    AnyIO clock carries an arbitrary offset).

    Attributes:
        request_id: Identifies the call that made this attempt. Every attempt
            of one call (the first try and its retries) shares the same id;
            ids increase per bus.
        attempt: 1-based attempt number within the call.
        max_attempts: Attempts the retry policy allows for this call
            (``RetryPolicy.retries + 1``; 1 for broadcasts).
        will_retry: ``True`` when the bus will send the request again after
            this attempt.
        slave_address: The unit address the request was sent to (0 for a
            broadcast).
        function_code: The request's function code.
        outcome: How the attempt ended.
        error: The exception the attempt ended with, or ``None`` for
            ``REPLY``, ``BROADCAST_SENT`` and ``CANCELLED``.
        started_at: When the attempt began, with the bus lock held and before
            waiting out the inter-frame gap.
        sent_at: When the request had been written and drained (before
            ``post_tx_settle``), or ``None`` if it never finished sending.
            Without a stream that supports ``drain``, this is when ``send``
            returned.
        ended_at: When the attempt ended.
        sent_at_ns: ``sent_at`` on the :func:`time.monotonic_ns` clock.
        ended_at_ns: ``ended_at`` on the :func:`time.monotonic_ns` clock.
        discarded_bytes: Bytes of a late reply to an earlier request that the
            bus read and dropped before sending this request (see
            :attr:`anymodbus.TimingConfig.late_reply_window`).
    """

    request_id: int
    attempt: int
    max_attempts: int
    will_retry: bool
    slave_address: int
    function_code: int
    outcome: TransactionOutcome
    error: Exception | None
    started_at: float
    sent_at: float | None
    ended_at: float
    sent_at_ns: int | None
    ended_at_ns: int
    discarded_bytes: int


#: A transaction observer: a synchronous callable that receives one
#: :class:`TransactionInfo` per attempt. It runs with the bus lock held, so it
#: must be quick and must not block. Exceptions it raises are logged and
#: otherwise ignored.
type TransactionObserver = Callable[[TransactionInfo], None]


__all__ = ["TransactionInfo", "TransactionObserver", "TransactionOutcome"]
