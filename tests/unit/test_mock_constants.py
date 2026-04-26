"""Pin :class:`MockSlave`'s spec-quantity constants against :mod:`anymodbus.pdu`.

The mock duplicates the per-FC bounds rather than importing from the
private ``pdu`` module — but if the spec ever changes (or the production
constants drift), the mock must move with it. This test fails loudly the
moment the two diverge.
"""

from __future__ import annotations

from anymodbus import pdu
from anymodbus._mock import slave as mock_slave


def test_max_read_bits_matches() -> None:
    assert mock_slave._MAX_READ_BITS == pdu._MAX_READ_BITS  # pyright: ignore[reportPrivateUsage]


def test_max_read_registers_matches() -> None:
    assert mock_slave._MAX_READ_REGISTERS == pdu._MAX_READ_REGISTERS  # pyright: ignore[reportPrivateUsage]


def test_max_write_coils_matches() -> None:
    assert mock_slave._MAX_WRITE_COILS == pdu._MAX_WRITE_COILS  # pyright: ignore[reportPrivateUsage]


def test_max_write_registers_matches() -> None:
    assert mock_slave._MAX_WRITE_REGISTERS == pdu._MAX_WRITE_REGISTERS  # pyright: ignore[reportPrivateUsage]
