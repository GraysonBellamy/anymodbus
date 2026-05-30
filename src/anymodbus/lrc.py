"""LRC checksum for Modbus ASCII (*Modbus over Serial Line v1.02 §6.2*).

The Longitudinal Redundancy Check is the 8-bit two's-complement of the sum of
the binary ``{address || PDU}`` bytes, computed *before* ASCII-hex encoding.
Mirrors :mod:`anymodbus.crc`: ``lrc8`` / ``lrc8_bytes`` / ``verify_lrc`` are
the LRC siblings of ``crc16_modbus`` / ``crc16_modbus_bytes`` / ``verify_crc``.

These functions are importable from ``anymodbus.lrc`` (not the top-level
package surface, matching ``anymodbus.crc``); they are pure and have no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Buffer

# Smallest frame that can carry an LRC: at least one body byte plus 1 LRC byte.
_MIN_LRC_FRAME_LEN = 2


def lrc8(data: Buffer) -> int:
    """Compute the 8-bit two's-complement LRC of ``data``.

    Args:
        data: Any buffer-protocol object (the binary ``{address || PDU}``
            bytes, *before* ASCII-hex encoding).

    Returns:
        The LRC as an integer in [0, 0xFF]: ``(-sum(bytes)) & 0xFF``.
    """
    return (-sum(memoryview(data).cast("B"))) & 0xFF


def lrc8_bytes(data: Buffer) -> bytes:
    """Compute the LRC of ``data`` and return it as a single byte, for appending."""
    return bytes((lrc8(data),))


def verify_lrc(frame: Buffer) -> bool:
    """Return True if the trailing byte of ``frame`` is its valid LRC.

    The sum of the entire frame (body + LRC byte) is ``0 (mod 256)`` when the
    LRC is correct — the two's-complement property — so we sum the whole frame
    and check for zero rather than splitting and recomputing.
    """
    view = memoryview(frame).cast("B")
    if len(view) < _MIN_LRC_FRAME_LEN:
        return False
    return (sum(view) & 0xFF) == 0


__all__ = ["lrc8", "lrc8_bytes", "verify_lrc"]
