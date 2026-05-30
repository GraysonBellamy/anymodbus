"""Validation tests for the convenience openers (no real serial port needed).

The parity / data-bits validation runs *before* the port is opened, so these
assertions never touch hardware. End-to-end opener behaviour is covered by the
hardware-marked tests.
"""

from __future__ import annotations

import pytest

from anymodbus import open_modbus_ascii, open_modbus_rtu
from anymodbus.exceptions import ConfigurationError

pytestmark = pytest.mark.anyio


async def test_rtu_rejects_bad_parity() -> None:
    with pytest.raises(ConfigurationError, match="parity"):
        await open_modbus_rtu("COM_NONEXISTENT", baudrate=19200, parity="bogus")  # type: ignore[arg-type]


async def test_ascii_rejects_bad_parity() -> None:
    with pytest.raises(ConfigurationError, match="parity"):
        await open_modbus_ascii("COM_NONEXISTENT", baudrate=19200, parity="bogus")  # type: ignore[arg-type]


@pytest.mark.parametrize("data_bits", [5, 6, 9, 0])
async def test_ascii_rejects_bad_data_bits(data_bits: int) -> None:
    with pytest.raises(ConfigurationError, match="data_bits"):
        await open_modbus_ascii(
            "COM_NONEXISTENT", baudrate=19200, parity="even", data_bits=data_bits
        )
