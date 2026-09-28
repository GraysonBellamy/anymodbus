"""Validation tests for :mod:`anymodbus.config` frozen dataclasses."""

from __future__ import annotations

import pytest

from anymodbus import BusConfig, RetryPolicy, TimingConfig, estimate_late_reply_window
from anymodbus.exceptions import (
    ConfigurationError,
    CRCError,
    FrameError,
    FrameTimeoutError,
    LRCError,
    ProtocolError,
    UnexpectedResponseError,
)


class TestBusConfig:
    def test_defaults(self) -> None:
        cfg = BusConfig()
        assert cfg.request_timeout == 3.0
        assert cfg.drain_after_send is True
        assert cfg.reset_input_buffer_before_request is True
        assert isinstance(cfg.timing, TimingConfig)
        assert isinstance(cfg.retries, RetryPolicy)

    def test_request_timeout_must_be_positive(self) -> None:
        with pytest.raises(ConfigurationError):
            BusConfig(request_timeout=0)
        with pytest.raises(ConfigurationError):
            BusConfig(request_timeout=-1.0)

    def test_request_timeout_upper_bound(self) -> None:
        with pytest.raises(ConfigurationError):
            BusConfig(request_timeout=120.0)

    def test_configuration_error_is_value_error(self) -> None:
        # Existing ``except ValueError`` blocks still catch us.
        with pytest.raises(ValueError):
            BusConfig(request_timeout=-1.0)

    def test_with_changes_returns_new_instance(self) -> None:
        original = BusConfig()
        updated = original.with_changes(request_timeout=2.5)
        assert original.request_timeout == 3.0
        assert updated.request_timeout == 2.5

    def test_frozen(self) -> None:
        cfg = BusConfig()
        with pytest.raises(AttributeError):
            cfg.request_timeout = 2.0  # type: ignore[misc]


class TestRetryPolicy:
    def test_defaults(self) -> None:
        rp = RetryPolicy()
        assert rp.retries == 1
        assert rp.retry_idempotent_only is True
        assert rp.backoff_base == 0.0
        # Every ProtocolError (checksum, malformed frame, mismatched reply) plus
        # a timeout. Subclasses are retried via isinstance.
        assert rp.retry_on == frozenset({ProtocolError, FrameTimeoutError})
        for cls in (CRCError, LRCError, FrameError, UnexpectedResponseError):
            assert issubclass(cls, ProtocolError)

    def test_retries_negative_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            RetryPolicy(retries=-1)

    def test_retries_no_upper_cap(self) -> None:
        # Caller knows their own tolerance for blocking better than we do.
        rp = RetryPolicy(retries=1000)
        assert rp.retries == 1000

    def test_backoff_negative_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            RetryPolicy(backoff_base=-0.1)

    def test_custom_retry_on(self) -> None:
        rp = RetryPolicy(retry_on=frozenset({CRCError}))
        assert rp.retry_on == frozenset({CRCError})


class TestTimingConfig:
    def test_defaults_use_auto(self) -> None:
        tc = TimingConfig()
        assert tc.inter_frame_idle == "auto"
        assert tc.inter_char_idle == "auto"
        assert tc.post_tx_settle == 0.0
        assert tc.broadcast_turnaround == 0.1
        assert tc.startup_settle == 0.0
        assert tc.late_reply_window == 0.0

    def test_negative_late_reply_window_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            TimingConfig(late_reply_window=-0.001)

    def test_late_reply_window_accepted(self) -> None:
        assert TimingConfig(late_reply_window=0.1).late_reply_window == 0.1

    def test_negative_startup_settle_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            TimingConfig(startup_settle=-0.001)

    def test_startup_settle_accepted(self) -> None:
        assert TimingConfig(startup_settle=0.05).startup_settle == 0.05

    def test_explicit_numeric_accepted(self) -> None:
        tc = TimingConfig(inter_frame_idle=0.005, inter_char_idle=0.002)
        assert tc.inter_frame_idle == 0.005
        assert tc.inter_char_idle == 0.002

    def test_negative_idle_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            TimingConfig(inter_frame_idle=-0.001)

    def test_negative_char_timeout_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            TimingConfig(inter_char_idle=-0.001)

    def test_negative_settle_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            TimingConfig(post_tx_settle=-0.001)

    def test_negative_broadcast_turnaround_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            TimingConfig(broadcast_turnaround=-0.001)


class TestEstimateLateReplyWindow:
    def test_sums_turnaround_transmission_and_latency(self) -> None:
        # 30 ms turnaround + 133 characters at 38400 baud (11 bits each) + 16 ms.
        window = estimate_late_reply_window(
            baudrate=38_400, max_turnaround=0.03, max_reply_bytes=133, latency=0.016
        )
        assert window == pytest.approx(0.03 + 133 * 11 / 38_400 + 0.016)

    def test_default_reply_is_the_largest_rtu_adu(self) -> None:
        window = estimate_late_reply_window(baudrate=19_200, max_turnaround=0.0)
        assert window == pytest.approx(256 * 11 / 19_200)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"baudrate": 0, "max_turnaround": 0.0},
            {"baudrate": 9600, "max_turnaround": -0.1},
            {"baudrate": 9600, "max_turnaround": 0.0, "max_reply_bytes": 0},
            {"baudrate": 9600, "max_turnaround": 0.0, "latency": -0.001},
        ],
    )
    def test_bad_arguments_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ConfigurationError):
            estimate_late_reply_window(**kwargs)  # type: ignore[arg-type]
