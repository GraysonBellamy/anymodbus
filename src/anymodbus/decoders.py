"""Decoders / encoders for multi-register types.

All helpers operate on **register sequences** (each register is a 16-bit
unsigned integer in [0, 0xFFFF]). The hot path is float32 / int32 spread
across two consecutive registers, where word and byte ordering vary by
device.

Defaults are :attr:`WordOrder.HIGH_LOW` and :attr:`ByteOrder.BIG`, equivalent
to ``struct.pack(">f", ...)``. The Modbus Application Protocol spec defines
big-endian byte order *within* a single 16-bit register (§4.2) but does
**not** standardize multi-register word ordering — that's vendor-defined.
``HIGH_LOW`` is the most common convention but real devices vary; always
confirm against the device's protocol manual and pass the order explicitly
when in doubt.

The functions are intentionally pure (no I/O) so they're trivial to
property-test against ``struct``.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, cast

from anymodbus._types import ByteOrder, WordOrder

if TYPE_CHECKING:
    from collections.abc import Sequence

_REGISTERS_PER_32BIT = 2
_BYTES_PER_REGISTER = 2
_INT32_SIGNED_MIN = -(2**31)
_INT32_SIGNED_MAX = 2**31 - 1
_INT32_UNSIGNED_MAX = 2**32 - 1
_REGISTER_VALUE_MAX = 0xFFFF


def _word_struct_fmt(byte_order: ByteOrder) -> str:
    """Struct format code for a 16-bit word in the given intra-word byte order.

    The Modbus wire encodes each register as big-endian (*app §4.2*), so the
    integer values in ``words`` already represent the BE interpretation. The
    ``ByteOrder`` parameter on these helpers describes how the *device* laid
    out a 32-bit value's bytes: BIG means the natural order; LITTLE means
    each register's two bytes were swapped before storage.
    """
    return ">HH" if byte_order == ByteOrder.BIG else "<HH"


def _validate_two_registers(words: Sequence[int]) -> None:
    if len(words) != _REGISTERS_PER_32BIT:
        msg = f"need exactly {_REGISTERS_PER_32BIT} registers for a 32-bit value (got {len(words)})"
        raise ValueError(msg)
    for w in words:
        if not (0 <= w <= _REGISTER_VALUE_MAX):
            msg = f"register value must be in [0, 0xFFFF] (got {w!r})"
            raise ValueError(msg)


def _pack_two_registers(
    words: Sequence[int], word_order: WordOrder, byte_order: ByteOrder
) -> bytes:
    """Convert two register words into the canonical 4-byte big-endian layout.

    After this, the bytes can be unpacked directly with ``struct.unpack(">f", ...)``
    or ``struct.unpack(">i", ...)`` regardless of the source ordering.
    """
    _validate_two_registers(words)
    msw, lsw = (words[0], words[1]) if word_order == WordOrder.HIGH_LOW else (words[1], words[0])
    return struct.pack(_word_struct_fmt(byte_order), msw, lsw)


def _unpack_to_two_registers(
    payload: bytes, word_order: WordOrder, byte_order: ByteOrder
) -> tuple[int, int]:
    """Inverse of :func:`_pack_two_registers`. ``payload`` must be exactly 4 bytes.

    All call sites pass a freshly-produced ``struct.pack(">f"|">i"|">I", ...)``
    — always 4 bytes — so we don't validate length here.
    """
    msw, lsw = struct.unpack(_word_struct_fmt(byte_order), payload)
    return (msw, lsw) if word_order == WordOrder.HIGH_LOW else (lsw, msw)


# ---------------------------------------------------------------------------
# float32
# ---------------------------------------------------------------------------


def decode_float32(
    words: Sequence[int],
    *,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> float:
    """Decode two 16-bit register words as an IEEE 754 float32."""
    canonical = _pack_two_registers(words, word_order, byte_order)
    (value,) = struct.unpack(">f", canonical)
    return cast("float", value)


def encode_float32(
    value: float,
    *,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> tuple[int, int]:
    """Encode ``value`` as IEEE 754 float32, return two 16-bit words."""
    canonical = struct.pack(">f", value)
    return _unpack_to_two_registers(canonical, word_order, byte_order)


# ---------------------------------------------------------------------------
# int32
# ---------------------------------------------------------------------------


def decode_int32(
    words: Sequence[int],
    *,
    signed: bool = True,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> int:
    """Decode two 16-bit register words as a 32-bit (signed by default) integer."""
    canonical = _pack_two_registers(words, word_order, byte_order)
    fmt = ">i" if signed else ">I"
    (value,) = struct.unpack(fmt, canonical)
    return cast("int", value)


def encode_int32(
    value: int,
    *,
    signed: bool = True,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> tuple[int, int]:
    """Encode ``value`` as a 32-bit integer, return two 16-bit words."""
    if signed:
        if not (_INT32_SIGNED_MIN <= value <= _INT32_SIGNED_MAX):
            msg = (
                f"signed int32 value must be in [{_INT32_SIGNED_MIN}, {_INT32_SIGNED_MAX}] "
                f"(got {value!r})"
            )
            raise ValueError(msg)
        canonical = struct.pack(">i", value)
    else:
        if not (0 <= value <= _INT32_UNSIGNED_MAX):
            msg = f"unsigned int32 value must be in [0, {_INT32_UNSIGNED_MAX}] (got {value!r})"
            raise ValueError(msg)
        canonical = struct.pack(">I", value)
    return _unpack_to_two_registers(canonical, word_order, byte_order)


# ---------------------------------------------------------------------------
# string
# ---------------------------------------------------------------------------


def decode_string(
    words: Sequence[int],
    *,
    encoding: str = "ascii",
    strip_null: bool = True,
) -> str:
    """Decode ``words`` (big-endian within each word) as a string in ``encoding``.

    Each 16-bit register contributes 2 bytes; the high byte is laid out
    first, per *app §4.2*. With ``strip_null=True`` (the default), trailing
    null bytes — typically used by devices to pad short strings up to the
    full register count — are removed before decoding.
    """
    for w in words:
        if not (0 <= w <= _REGISTER_VALUE_MAX):
            msg = f"register value must be in [0, 0xFFFF] (got {w!r})"
            raise ValueError(msg)
    raw = struct.pack(f">{len(words)}H", *words)
    if strip_null:
        raw = raw.rstrip(b"\x00")
    return raw.decode(encoding)


def encode_string(
    value: str,
    *,
    register_count: int,
    encoding: str = "ascii",
    pad: bytes = b"\x00",
) -> tuple[int, ...]:
    """Encode ``value`` into ``register_count`` 16-bit words (big-endian within word).

    The encoded byte length must not exceed ``register_count * 2``; shorter
    encodings are right-padded with the single byte ``pad`` (default null).
    """
    if register_count < 1:
        msg = f"register_count must be at least 1 (got {register_count!r})"
        raise ValueError(msg)
    if len(pad) != 1:
        msg = f"pad must be exactly one byte (got {pad!r})"
        raise ValueError(msg)
    raw = value.encode(encoding)
    target_len = register_count * _BYTES_PER_REGISTER
    if len(raw) > target_len:
        msg = (
            f"encoded value is {len(raw)} bytes; does not fit in {register_count} "
            f"registers ({target_len} bytes)"
        )
        raise ValueError(msg)
    raw = raw.ljust(target_len, pad)
    return struct.unpack(f">{register_count}H", raw)


__all__ = [
    "decode_float32",
    "decode_int32",
    "decode_string",
    "encode_float32",
    "encode_int32",
    "encode_string",
]
