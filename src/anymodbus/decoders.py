"""Decoders / encoders for register-encoded values.

All helpers operate on **register sequences** (each register is a 16-bit
unsigned integer in [0, 0xFFFF]). The Modbus Application Protocol spec
defines big-endian byte order *within* a single 16-bit register (§4.2) but
does **not** standardize multi-register word ordering — that's vendor-defined.
``HIGH_LOW`` is the most common convention but real devices vary; always
confirm against the device's protocol manual and pass the order explicitly
when in doubt.

Defaults are :attr:`WordOrder.HIGH_LOW` and :attr:`ByteOrder.BIG`, equivalent
to the natural ``struct.pack(">f", ...)`` / ``struct.pack(">i", ...)`` /
``struct.pack(">q", ...)`` layout.

The functions are intentionally pure (no I/O) so they're trivial to
property-test against ``struct``.

In addition to the per-type helpers, :func:`decode` and :func:`encode`
provide a type-dispatched API keyed off :class:`RegisterType`. That's the
recommended entry point for downstream device libraries that load a register
schema from configuration; the per-type helpers stay available for places
where the type is statically known.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, cast

from anymodbus._types import ByteOrder, RegisterType, WordOrder

if TYPE_CHECKING:
    from collections.abc import Sequence

_REGISTERS_PER_16BIT = 1
_REGISTERS_PER_32BIT = 2
_REGISTERS_PER_64BIT = 4
_BYTES_PER_REGISTER = 2
_INT16_SIGNED_MIN = -(2**15)
_INT16_SIGNED_MAX = 2**15 - 1
_INT16_UNSIGNED_MAX = 2**16 - 1
_INT32_SIGNED_MIN = -(2**31)
_INT32_SIGNED_MAX = 2**31 - 1
_INT32_UNSIGNED_MAX = 2**32 - 1
_INT64_SIGNED_MIN = -(2**63)
_INT64_SIGNED_MAX = 2**63 - 1
_INT64_UNSIGNED_MAX = 2**64 - 1
_REGISTER_VALUE_MAX = 0xFFFF


# ---------------------------------------------------------------------------
# Internal helpers — N-word pack/unpack with word + byte ordering.
#
# The shape is: register words on the wire are big-endian per *app §4.2*, so
# the integer values in ``words`` already represent the BE interpretation.
# ``WordOrder`` reorders the registers themselves; ``ByteOrder`` describes
# how the *device* laid the bytes out within each register (BIG = natural
# BE; LITTLE = each register's two bytes were swapped before storage).
# ---------------------------------------------------------------------------


def _validate_words(words: Sequence[int], expected: int) -> None:
    if len(words) != expected:
        msg = f"need exactly {expected} register(s) (got {len(words)})"
        raise ValueError(msg)
    for w in words:
        if not (0 <= w <= _REGISTER_VALUE_MAX):
            msg = f"register value must be in [0, 0xFFFF] (got {w!r})"
            raise ValueError(msg)


def _pack_words(words: Sequence[int], *, word_order: WordOrder, byte_order: ByteOrder) -> bytes:
    """Convert ``words`` into a canonical big-endian byte string.

    After this, the bytes can be unpacked directly with the matching ``>``
    struct format regardless of the source ordering.
    """
    ordered = list(words) if word_order is WordOrder.HIGH_LOW else list(reversed(words))
    fmt = ">" if byte_order is ByteOrder.BIG else "<"
    return struct.pack(f"{fmt}{len(ordered)}H", *ordered)


def _unpack_to_words(
    payload: bytes, *, word_order: WordOrder, byte_order: ByteOrder
) -> tuple[int, ...]:
    """Inverse of :func:`_pack_words`. ``payload`` length must be a multiple of 2."""
    n = len(payload) // _BYTES_PER_REGISTER
    fmt = ">" if byte_order is ByteOrder.BIG else "<"
    words = struct.unpack(f"{fmt}{n}H", payload)
    return words if word_order is WordOrder.HIGH_LOW else tuple(reversed(words))


def _check_int_range(value: int, *, signed: bool, bits: int) -> None:
    """Raise ``ValueError`` if ``value`` doesn't fit in a (signed/unsigned) ``bits``-bit integer."""
    if signed:
        lo = -(1 << (bits - 1))
        hi = (1 << (bits - 1)) - 1
        if not (lo <= value <= hi):
            msg = f"signed int{bits} value must be in [{lo}, {hi}] (got {value!r})"
            raise ValueError(msg)
    else:
        hi = (1 << bits) - 1
        if not (0 <= value <= hi):
            msg = f"unsigned int{bits} value must be in [0, {hi}] (got {value!r})"
            raise ValueError(msg)


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
    _validate_words(words, _REGISTERS_PER_32BIT)
    canonical = _pack_words(words, word_order=word_order, byte_order=byte_order)
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
    return cast(
        "tuple[int, int]",
        _unpack_to_words(canonical, word_order=word_order, byte_order=byte_order),
    )


# ---------------------------------------------------------------------------
# float64
# ---------------------------------------------------------------------------


def decode_float64(
    words: Sequence[int],
    *,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> float:
    """Decode four 16-bit register words as an IEEE 754 float64."""
    _validate_words(words, _REGISTERS_PER_64BIT)
    canonical = _pack_words(words, word_order=word_order, byte_order=byte_order)
    (value,) = struct.unpack(">d", canonical)
    return cast("float", value)


def encode_float64(
    value: float,
    *,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> tuple[int, int, int, int]:
    """Encode ``value`` as IEEE 754 float64, return four 16-bit words."""
    canonical = struct.pack(">d", value)
    return cast(
        "tuple[int, int, int, int]",
        _unpack_to_words(canonical, word_order=word_order, byte_order=byte_order),
    )


# ---------------------------------------------------------------------------
# int16
# ---------------------------------------------------------------------------


def decode_int16(
    words: Sequence[int],
    *,
    signed: bool = True,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> int:
    """Decode one 16-bit register as a (signed by default) integer.

    ``byte_order`` is rarely needed for single-register integers — the spec
    defines the wire as big-endian inside a register (*app §4.2*). It is
    provided for symmetry with the wider integer types and for the (unusual)
    case of devices that store INT16 values byte-swapped within a register.
    """
    _validate_words(words, _REGISTERS_PER_16BIT)
    canonical = _pack_words(words, word_order=WordOrder.HIGH_LOW, byte_order=byte_order)
    fmt = ">h" if signed else ">H"
    (value,) = struct.unpack(fmt, canonical)
    return cast("int", value)


def encode_int16(
    value: int,
    *,
    signed: bool = True,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> tuple[int]:
    """Encode ``value`` as a 16-bit integer, return a single-register tuple."""
    _check_int_range(value, signed=signed, bits=16)
    fmt = ">h" if signed else ">H"
    canonical = struct.pack(fmt, value)
    return cast(
        "tuple[int]",
        _unpack_to_words(canonical, word_order=WordOrder.HIGH_LOW, byte_order=byte_order),
    )


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
    _validate_words(words, _REGISTERS_PER_32BIT)
    canonical = _pack_words(words, word_order=word_order, byte_order=byte_order)
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
    _check_int_range(value, signed=signed, bits=32)
    fmt = ">i" if signed else ">I"
    canonical = struct.pack(fmt, value)
    return cast(
        "tuple[int, int]",
        _unpack_to_words(canonical, word_order=word_order, byte_order=byte_order),
    )


# ---------------------------------------------------------------------------
# int64
# ---------------------------------------------------------------------------


def decode_int64(
    words: Sequence[int],
    *,
    signed: bool = True,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> int:
    """Decode four 16-bit register words as a 64-bit (signed by default) integer."""
    _validate_words(words, _REGISTERS_PER_64BIT)
    canonical = _pack_words(words, word_order=word_order, byte_order=byte_order)
    fmt = ">q" if signed else ">Q"
    (value,) = struct.unpack(fmt, canonical)
    return cast("int", value)


def encode_int64(
    value: int,
    *,
    signed: bool = True,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> tuple[int, int, int, int]:
    """Encode ``value`` as a 64-bit integer, return four 16-bit words."""
    _check_int_range(value, signed=signed, bits=64)
    fmt = ">q" if signed else ">Q"
    canonical = struct.pack(fmt, value)
    return cast(
        "tuple[int, int, int, int]",
        _unpack_to_words(canonical, word_order=word_order, byte_order=byte_order),
    )


# ---------------------------------------------------------------------------
# string
# ---------------------------------------------------------------------------


def decode_string(
    words: Sequence[int],
    *,
    byte_order: ByteOrder = ByteOrder.BIG,
    encoding: str = "ascii",
    strip_null: bool = True,
) -> str:
    """Decode ``words`` as a string in ``encoding``.

    Each 16-bit register contributes 2 bytes. With ``byte_order=BIG``
    (default, per *app §4.2*), the high byte of each register is laid out
    first; with ``byte_order=LITTLE``, the two bytes are swapped before
    decoding (used by devices that store strings as little-endian-within-
    register pairs). With ``strip_null=True`` (default), trailing null bytes
    — typically used by devices to pad short strings up to the full register
    count — are removed before decoding.
    """
    for w in words:
        if not (0 <= w <= _REGISTER_VALUE_MAX):
            msg = f"register value must be in [0, 0xFFFF] (got {w!r})"
            raise ValueError(msg)
    fmt = ">" if byte_order is ByteOrder.BIG else "<"
    raw = struct.pack(f"{fmt}{len(words)}H", *words)
    if strip_null:
        raw = raw.rstrip(b"\x00")
    return raw.decode(encoding)


def encode_string(
    value: str,
    *,
    register_count: int | None = None,
    byte_count: int | None = None,
    byte_order: ByteOrder = ByteOrder.BIG,
    encoding: str = "ascii",
    pad: bytes = b"\x00",
) -> tuple[int, ...]:
    """Encode ``value`` into 16-bit words.

    Exactly one of ``register_count`` or ``byte_count`` must be supplied.
    ``byte_count`` is convenient for fields specified in bytes whose length
    isn't a multiple of two — the last register is half-padded so the total
    register count is ``ceil(byte_count / 2)``. The encoded byte length must
    not exceed ``register_count * 2`` (or ``byte_count``); shorter encodings
    are right-padded with the single byte ``pad`` (default null).

    With ``byte_order=LITTLE``, each register's two bytes are swapped before
    they hit the wire — for devices that expect strings stored
    little-endian-within-register.
    """
    if (register_count is None) == (byte_count is None):
        msg = "supply exactly one of register_count or byte_count"
        raise ValueError(msg)
    if len(pad) != 1:
        msg = f"pad must be exactly one byte (got {pad!r})"
        raise ValueError(msg)
    if register_count is not None:
        if register_count < 1:
            msg = f"register_count must be at least 1 (got {register_count!r})"
            raise ValueError(msg)
        target_bytes = register_count * _BYTES_PER_REGISTER
        rc = register_count
    else:
        assert byte_count is not None
        if byte_count < 1:
            msg = f"byte_count must be at least 1 (got {byte_count!r})"
            raise ValueError(msg)
        target_bytes = byte_count
        rc = (byte_count + 1) // 2

    raw = value.encode(encoding)
    if len(raw) > target_bytes:
        msg = f"encoded value is {len(raw)} bytes; does not fit in {target_bytes} bytes"
        raise ValueError(msg)
    raw = raw.ljust(rc * _BYTES_PER_REGISTER, pad)
    fmt = ">" if byte_order is ByteOrder.BIG else "<"
    return struct.unpack(f"{fmt}{rc}H", raw)


# ---------------------------------------------------------------------------
# Type-dispatched encode / decode.
#
# Recommended entry point for downstream device libraries that drive a
# register schema from configuration. The per-type helpers above stay
# available for places where the type is statically known.
# ---------------------------------------------------------------------------


_FIXED_REGISTER_COUNTS: dict[RegisterType, int] = {
    RegisterType.INT16: _REGISTERS_PER_16BIT,
    RegisterType.UINT16: _REGISTERS_PER_16BIT,
    RegisterType.INT32: _REGISTERS_PER_32BIT,
    RegisterType.UINT32: _REGISTERS_PER_32BIT,
    RegisterType.INT64: _REGISTERS_PER_64BIT,
    RegisterType.UINT64: _REGISTERS_PER_64BIT,
    RegisterType.FLOAT32: _REGISTERS_PER_32BIT,
    RegisterType.FLOAT64: _REGISTERS_PER_64BIT,
}


def register_count_for(register_type: RegisterType) -> int | None:
    """Return the fixed register count for ``register_type``, or ``None`` for STRING.

    Useful when building a `Slave.read_holding_registers(..., count=N)` call
    from a schema entry: ``count = register_count_for(entry.type) or entry.register_count``.
    """
    return _FIXED_REGISTER_COUNTS.get(register_type)


def decode(  # noqa: PLR0911 — one return per RegisterType branch is the clearest shape
    words: Sequence[int],
    *,
    type: RegisterType,  # noqa: A002 — `type` is the canonical kwarg name in the schema
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
    encoding: str = "ascii",
    strip_null: bool = True,
) -> int | float | str:
    """Type-dispatched decode of ``words`` to a Python value.

    Single-register integer types (``INT16`` / ``UINT16``) ignore
    ``word_order``. ``STRING`` ignores ``word_order`` and uses ``encoding``
    + ``strip_null``. All other kwargs are passed through to the matching
    per-type helper.
    """
    match type:
        case RegisterType.INT16:
            return decode_int16(words, signed=True, byte_order=byte_order)
        case RegisterType.UINT16:
            return decode_int16(words, signed=False, byte_order=byte_order)
        case RegisterType.INT32:
            return decode_int32(words, signed=True, word_order=word_order, byte_order=byte_order)
        case RegisterType.UINT32:
            return decode_int32(words, signed=False, word_order=word_order, byte_order=byte_order)
        case RegisterType.INT64:
            return decode_int64(words, signed=True, word_order=word_order, byte_order=byte_order)
        case RegisterType.UINT64:
            return decode_int64(words, signed=False, word_order=word_order, byte_order=byte_order)
        case RegisterType.FLOAT32:
            return decode_float32(words, word_order=word_order, byte_order=byte_order)
        case RegisterType.FLOAT64:
            return decode_float64(words, word_order=word_order, byte_order=byte_order)
        case RegisterType.STRING:
            return decode_string(
                words, byte_order=byte_order, encoding=encoding, strip_null=strip_null
            )


def encode(  # noqa: PLR0911 — one branch per RegisterType is the clearest shape
    value: int | float | str,
    *,
    type: RegisterType,  # noqa: A002 — `type` is the canonical kwarg name in the schema
    register_count: int | None = None,
    byte_count: int | None = None,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
    encoding: str = "ascii",
    pad: bytes = b"\x00",
) -> tuple[int, ...]:
    """Type-dispatched encode of ``value`` into register words.

    For fixed-width types, ``register_count`` and ``byte_count`` must be
    ``None`` (or, for backwards-compat ergonomics, may match the type's
    natural register count). For ``STRING``, exactly one of
    ``register_count`` / ``byte_count`` must be supplied.
    """
    if type is RegisterType.STRING:
        if not isinstance(value, str):
            msg = f"STRING encode expects str, got {value.__class__.__name__}"
            raise TypeError(msg)
        return encode_string(
            value,
            register_count=register_count,
            byte_count=byte_count,
            byte_order=byte_order,
            encoding=encoding,
            pad=pad,
        )

    expected = _FIXED_REGISTER_COUNTS[type]
    if register_count is not None and register_count != expected:
        msg = (
            f"register_count={register_count} disagrees with the natural "
            f"register count for {type.value} ({expected})"
        )
        raise ValueError(msg)
    if byte_count is not None:
        msg = f"byte_count is only meaningful for STRING (got type={type.value})"
        raise ValueError(msg)

    match type:
        case RegisterType.INT16:
            return encode_int16(int(value), signed=True, byte_order=byte_order)
        case RegisterType.UINT16:
            return encode_int16(int(value), signed=False, byte_order=byte_order)
        case RegisterType.INT32:
            return encode_int32(
                int(value), signed=True, word_order=word_order, byte_order=byte_order
            )
        case RegisterType.UINT32:
            return encode_int32(
                int(value), signed=False, word_order=word_order, byte_order=byte_order
            )
        case RegisterType.INT64:
            return encode_int64(
                int(value), signed=True, word_order=word_order, byte_order=byte_order
            )
        case RegisterType.UINT64:
            return encode_int64(
                int(value), signed=False, word_order=word_order, byte_order=byte_order
            )
        case RegisterType.FLOAT32:
            return encode_float32(float(value), word_order=word_order, byte_order=byte_order)
        case RegisterType.FLOAT64:
            return encode_float64(float(value), word_order=word_order, byte_order=byte_order)


__all__ = [
    "decode",
    "decode_float32",
    "decode_float64",
    "decode_int16",
    "decode_int32",
    "decode_int64",
    "decode_string",
    "encode",
    "encode_float32",
    "encode_float64",
    "encode_int16",
    "encode_int32",
    "encode_int64",
    "encode_string",
    "register_count_for",
]
