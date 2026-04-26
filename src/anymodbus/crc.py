"""CRC-16/Modbus computation.

The Modbus CRC-16 uses polynomial ``0xA001`` (reflected form of ``0x8005``)
with an initial value of ``0xFFFF``, no final XOR, and reflected input/output
bytes — i.e., the result is appended to the wire frame in little-endian byte
order.

Implementation uses a 256-entry precomputed table for hot-path performance;
the table is built lazily on import (small, deterministic, ~30 µs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Buffer

_POLY = 0xA001
_INIT = 0xFFFF
# Smallest frame that can carry a CRC: at least one body byte plus 2 CRC bytes.
_MIN_CRC_FRAME_LEN = 3


def _build_table() -> tuple[int, ...]:
    table: list[int] = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_TABLE: tuple[int, ...] = _build_table()


def crc16_modbus(data: Buffer) -> int:
    """Compute the CRC-16/Modbus of ``data``.

    Args:
        data: Any buffer-protocol object (bytes, bytearray, memoryview, ...).

    Returns:
        The 16-bit CRC value as an integer in [0, 0xFFFF]. To append it to a
        Modbus RTU frame, write the low byte first then the high byte.
    """
    crc = _INIT
    for byte in memoryview(data).cast("B"):
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc


def crc16_modbus_bytes(data: Buffer) -> bytes:
    """Compute the CRC-16/Modbus of ``data`` and return it as 2 little-endian bytes.

    Convenience for the common case of appending CRC to a frame:
    ``frame += crc16_modbus_bytes(frame)``.
    """
    crc = crc16_modbus(data)
    return crc.to_bytes(2, "little")


def verify_crc(frame: Buffer) -> bool:
    """Return True if the trailing 2 bytes of ``frame`` are its valid CRC.

    The CRC of the entire frame (body + CRC bytes) is zero when the CRC is
    correct — a property of CRC-16. We compute over the whole frame and
    check for zero rather than splitting and recomputing, which is both
    faster and harder to get wrong.
    """
    view = memoryview(frame).cast("B")
    if len(view) < _MIN_CRC_FRAME_LEN:
        return False
    return crc16_modbus(view) == 0


__all__ = ["crc16_modbus", "crc16_modbus_bytes", "verify_crc"]
