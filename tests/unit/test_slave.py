"""Tests for the per-slave handle (:class:`anymodbus.Slave`).

These tests pin down the construction-time validation and the read-only
``address`` property; full I/O coverage lives in the integration suite.
"""

from __future__ import annotations

import pytest

from anymodbus import Bus, BusConfig, Slave
from anymodbus.exceptions import ConfigurationError


class _FakeStream:
    """Minimal stand-in for an anyio ByteStream — Slave construction touches none of it."""

    async def aclose(self) -> None:  # pragma: no cover - never called in these tests
        pass


def _make_bus() -> Bus:
    # Bypass the AnyIO ByteStream type check at runtime; we never call I/O.
    return Bus(_FakeStream(), config=BusConfig())  # type: ignore[arg-type]


class TestSlaveAddressValidation:
    """Per *Modbus over Serial Line v1.02 §2.2*: only 1-247 are valid unicast addresses."""

    @pytest.mark.parametrize("addr", [1, 2, 100, 246, 247])
    def test_unicast_range_accepted(self, addr: int) -> None:
        Slave(_make_bus(), addr)

    def test_address_zero_rejected(self) -> None:
        # Broadcast address; routes through Bus.broadcast_* methods only.
        with pytest.raises(ConfigurationError, match="broadcast"):
            Slave(_make_bus(), 0)

    @pytest.mark.parametrize("addr", [248, 250, 254, 255])
    def test_reserved_range_rejected(self, addr: int) -> None:
        # 248-255 are reserved by the spec.
        with pytest.raises(ConfigurationError, match="reserved"):
            Slave(_make_bus(), addr)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            Slave(_make_bus(), -1)

    def test_too_large_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            Slave(_make_bus(), 256)


class TestSlaveAddressIsReadOnly:
    """``Slave.address`` is exposed as a read-only property post-construction."""

    def test_address_returns_constructor_value(self) -> None:
        s = Slave(_make_bus(), 17)
        assert s.address == 17

    def test_address_cannot_be_reassigned(self) -> None:
        s = Slave(_make_bus(), 17)
        with pytest.raises(AttributeError):
            s.address = 18  # type: ignore[misc]
