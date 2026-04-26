"""Immutable, validated configuration for :class:`anymodbus.Bus`.

All configuration is expressed as frozen dataclasses so it can be hashed,
compared, and shared safely across tasks. Validation runs in
``__post_init__`` — the only supported way to change a config at runtime is
:meth:`BusConfig.with_changes`, which returns a new instance.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal, Self

from anymodbus.exceptions import (
    ConfigurationError,
    CRCError,
    FrameTimeoutError,
    ModbusError,
)

_DEFAULT_REQUEST_TIMEOUT = 3.0
_DEFAULT_BROADCAST_TURNAROUND = 0.1  # 100 ms — Serial Line spec §2.4.1 minimum
_MAX_REQUEST_TIMEOUT = 60.0

#: Sentinel for "compute from baud" timing fields. The string value lets
#: existing equality checks (``cfg.timing.inter_frame_idle == "auto"``) keep
#: working without anyone having to import the sentinel.
AutoTiming = Literal["auto"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TimingConfig:
    """Timing knobs for the RTU bus.

    Attributes:
        inter_frame_idle: Pre-tx idle gap in seconds. ``"auto"`` computes
            ``max(3.5 * 11 / baudrate, 0.00175)`` from the stream's current
            baud (looked up via :class:`anyserial.SerialStreamAttribute`); a
            float value is taken literally. Defaults to ``"auto"``.
        inter_char_idle: Maximum idle gap **within** a frame, used only on
            the unknown-FC fallback rx path. Defaults to ``"auto"``
            (1.5 character-times).
        post_tx_settle: Optional fixed wait after ``send`` returns and before
            we start reading. Default 0; some RS-485 transceivers benefit
            from a tiny settling delay.
        broadcast_turnaround: Idle wait after a broadcast tx (slave_address=0)
            before the next transaction is allowed. The Serial Line spec
            §2.4.1 calls for a "Turnaround delay" long enough for every slave
            to finish processing — typically 100-200 ms. Default 100 ms.
    """

    inter_frame_idle: float | AutoTiming = "auto"
    inter_char_idle: float | AutoTiming = "auto"
    post_tx_settle: float = 0.0
    broadcast_turnaround: float = _DEFAULT_BROADCAST_TURNAROUND

    def __post_init__(self) -> None:
        """Validate numeric timings."""
        if isinstance(self.inter_frame_idle, (int, float)) and self.inter_frame_idle < 0:
            raise ConfigurationError(
                f"inter_frame_idle must be >= 0 (got {self.inter_frame_idle!r})"
            )
        if isinstance(self.inter_char_idle, (int, float)) and self.inter_char_idle < 0:
            raise ConfigurationError(f"inter_char_idle must be >= 0 (got {self.inter_char_idle!r})")
        if self.post_tx_settle < 0:
            raise ConfigurationError(f"post_tx_settle must be >= 0 (got {self.post_tx_settle!r})")
        if self.broadcast_turnaround < 0:
            raise ConfigurationError(
                f"broadcast_turnaround must be >= 0 (got {self.broadcast_turnaround!r})"
            )


_DEFAULT_RETRY_ON: frozenset[type[ModbusError]] = frozenset({CRCError, FrameTimeoutError})


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """Retry policy for transient transport errors.

    Attributes:
        retries: Number of additional attempts after the first. Default 1.
            Must be >= 0; no upper cap (the caller knows their tolerance for
            blocking better than we do).
        retry_on: Exception classes that count as "transient" and trigger a
            retry. Default ``{CRCError, FrameTimeoutError}``. Modbus exception
            responses (``IllegalFunctionError`` etc.) are NEVER retried — the
            slave told us no, retrying won't change that.
        retry_idempotent_only: If True (default), only read function codes
            (FC 1-4, see :func:`anymodbus.is_idempotent_function`) are
            retried; writes raise on the first transport error to avoid
            silent double-writes if the request landed but the response was
            lost. Set False to retry every FC.
        backoff_base: Extra seconds added after each retry, on top of the
            mandatory inter-frame idle gap. Default 0.
    """

    retries: int = 1
    retry_on: frozenset[type[ModbusError]] = field(default_factory=lambda: _DEFAULT_RETRY_ON)
    retry_idempotent_only: bool = True
    backoff_base: float = 0.0

    def __post_init__(self) -> None:
        """Validate ranges."""
        if self.retries < 0:
            raise ConfigurationError(f"retries must be >= 0 (got {self.retries!r})")
        if self.backoff_base < 0:
            raise ConfigurationError(f"backoff_base must be >= 0 (got {self.backoff_base!r})")


@dataclass(frozen=True, slots=True, kw_only=True)
class BusConfig:
    """Full :class:`anymodbus.Bus` configuration.

    Construct and validate up front; pass to ``Bus(stream, config=...)``. All
    fields have sensible defaults. Use :meth:`with_changes` to derive a new
    config without mutating an existing one.
    """

    request_timeout: float = _DEFAULT_REQUEST_TIMEOUT
    timing: TimingConfig = field(default_factory=TimingConfig)
    retries: RetryPolicy = field(default_factory=RetryPolicy)
    drain_after_send: bool = True
    reset_input_buffer_before_request: bool = True

    def __post_init__(self) -> None:
        """Validate top-level fields. Sub-configs validate themselves."""
        if not (0 < self.request_timeout <= _MAX_REQUEST_TIMEOUT):
            raise ConfigurationError(
                f"request_timeout must be in (0, {_MAX_REQUEST_TIMEOUT}] "
                f"(got {self.request_timeout!r})"
            )

    def with_changes(self, **changes: Any) -> Self:
        """Return a copy of this config with the given fields replaced.

        ``dataclasses.replace`` re-runs ``__post_init__``, so validation
        covers the new values.
        """
        return dataclasses.replace(self, **changes)


__all__ = [
    "AutoTiming",
    "BusConfig",
    "RetryPolicy",
    "TimingConfig",
]
