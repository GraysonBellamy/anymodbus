"""Tests for :mod:`anymodbus.decoders` — per-type helpers and the dispatcher.

The default ordering (``HIGH_LOW`` + ``BIG``) is asserted to be equivalent
to ``struct.pack(">f"|">d"|">i"|">q", ...)``. The other three combinations
of word_order x byte_order are exercised both by direct wire fixtures and
by round-trip property tests.
"""

from __future__ import annotations

import math
import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anymodbus._types import ByteOrder, RegisterType, WordOrder
from anymodbus.decoders import (
    decode,
    decode_float32,
    decode_float64,
    decode_int16,
    decode_int32,
    decode_int64,
    decode_string,
    encode,
    encode_float32,
    encode_float64,
    encode_int16,
    encode_int32,
    encode_int64,
    encode_string,
    register_count_for,
)

_ORDER_MATRIX = [
    (WordOrder.HIGH_LOW, ByteOrder.BIG),
    (WordOrder.HIGH_LOW, ByteOrder.LITTLE),
    (WordOrder.LOW_HIGH, ByteOrder.BIG),
    (WordOrder.LOW_HIGH, ByteOrder.LITTLE),
]


# ---------------------------------------------------------------------------
# Defaults match struct.pack(">f"|">d"|">i"|">q", ...).
# ---------------------------------------------------------------------------


class TestCanonicalDefaults:
    """HIGH_LOW + BIG must match the simple struct big-endian interpretation."""

    @given(value=st.floats(allow_nan=False, allow_infinity=False, width=32))
    def test_float32_default_matches_struct_be(self, value: float) -> None:
        words = encode_float32(value)
        canonical = struct.pack(f">{len(words)}H", *words)
        assert canonical == struct.pack(">f", value)

    def test_float32_one_default(self) -> None:
        # 1.0 in IEEE 754 = 0x3F800000 → words (0x3F80, 0x0000).
        assert encode_float32(1.0) == (0x3F80, 0x0000)
        assert decode_float32((0x3F80, 0x0000)) == 1.0

    @given(value=st.floats(allow_nan=False, allow_infinity=False))
    def test_float64_default_matches_struct_be(self, value: float) -> None:
        words = encode_float64(value)
        canonical = struct.pack(f">{len(words)}H", *words)
        assert canonical == struct.pack(">d", value)
        assert decode_float64(words) == value

    @given(value=st.integers(min_value=-(2**15), max_value=2**15 - 1))
    def test_int16_signed_default_matches_struct_be(self, value: int) -> None:
        words = encode_int16(value, signed=True)
        canonical = struct.pack(f">{len(words)}H", *words)
        assert canonical == struct.pack(">h", value)

    @given(value=st.integers(min_value=-(2**31), max_value=2**31 - 1))
    def test_int32_signed_default_matches_struct_be(self, value: int) -> None:
        words = encode_int32(value, signed=True)
        canonical = struct.pack(f">{len(words)}H", *words)
        assert canonical == struct.pack(">i", value)

    @given(value=st.integers(min_value=0, max_value=2**32 - 1))
    def test_int32_unsigned_default_matches_struct_be(self, value: int) -> None:
        words = encode_int32(value, signed=False)
        canonical = struct.pack(f">{len(words)}H", *words)
        assert canonical == struct.pack(">I", value)

    @given(value=st.integers(min_value=-(2**63), max_value=2**63 - 1))
    def test_int64_signed_default_matches_struct_be(self, value: int) -> None:
        words = encode_int64(value, signed=True)
        canonical = struct.pack(f">{len(words)}H", *words)
        assert canonical == struct.pack(">q", value)


# ---------------------------------------------------------------------------
# Word/byte-order matrix — round-trip property tests
# ---------------------------------------------------------------------------


class TestFloat32OrderMatrix:
    @pytest.mark.parametrize(("word_order", "byte_order"), _ORDER_MATRIX)
    @given(value=st.floats(allow_nan=False, allow_infinity=False, width=32))
    def test_roundtrip_all_orderings(
        self, word_order: WordOrder, byte_order: ByteOrder, value: float
    ) -> None:
        words = encode_float32(value, word_order=word_order, byte_order=byte_order)
        decoded = decode_float32(words, word_order=word_order, byte_order=byte_order)
        if value == 0.0:
            assert decoded == 0.0
        else:
            assert math.isclose(decoded, value, rel_tol=0, abs_tol=0)

    def test_low_high_big_swaps_words(self) -> None:
        assert encode_float32(1.0, word_order=WordOrder.LOW_HIGH) == (0x0000, 0x3F80)
        assert decode_float32((0x0000, 0x3F80), word_order=WordOrder.LOW_HIGH) == 1.0

    def test_high_low_little_swaps_intra_word_bytes(self) -> None:
        words = encode_float32(1.0, byte_order=ByteOrder.LITTLE)
        assert words == (0x803F, 0x0000)
        assert decode_float32(words, byte_order=ByteOrder.LITTLE) == 1.0

    def test_low_high_little_full_reverse(self) -> None:
        words = encode_float32(1.0, word_order=WordOrder.LOW_HIGH, byte_order=ByteOrder.LITTLE)
        assert words == (0x0000, 0x803F)


class TestFloat64OrderMatrix:
    @pytest.mark.parametrize(("word_order", "byte_order"), _ORDER_MATRIX)
    @given(value=st.floats(allow_nan=False, allow_infinity=False))
    def test_roundtrip_all_orderings(
        self, word_order: WordOrder, byte_order: ByteOrder, value: float
    ) -> None:
        words = encode_float64(value, word_order=word_order, byte_order=byte_order)
        decoded = decode_float64(words, word_order=word_order, byte_order=byte_order)
        if value == 0.0:
            assert decoded == 0.0
        else:
            assert decoded == value

    def test_default_one(self) -> None:
        # 1.0 as IEEE 754 double = 0x3FF0000000000000 → words.
        assert encode_float64(1.0) == (0x3FF0, 0x0000, 0x0000, 0x0000)
        assert decode_float64((0x3FF0, 0x0000, 0x0000, 0x0000)) == 1.0


class TestInt16OrderMatrix:
    @given(value=st.integers(min_value=-(2**15), max_value=2**15 - 1))
    def test_signed_roundtrip(self, value: int) -> None:
        words = encode_int16(value, signed=True)
        assert decode_int16(words, signed=True) == value

    @given(value=st.integers(min_value=0, max_value=2**16 - 1))
    def test_unsigned_roundtrip(self, value: int) -> None:
        words = encode_int16(value, signed=False)
        assert decode_int16(words, signed=False) == value

    def test_signed_negative_one(self) -> None:
        assert encode_int16(-1) == (0xFFFF,)
        assert decode_int16((0xFFFF,), signed=True) == -1
        assert decode_int16((0xFFFF,), signed=False) == 0xFFFF

    def test_byte_order_little_swaps(self) -> None:
        # 0x1234 stored byte-swapped within the register reads as 0x3412.
        assert encode_int16(0x1234, byte_order=ByteOrder.LITTLE) == (0x3412,)
        assert decode_int16((0x3412,), byte_order=ByteOrder.LITTLE) == 0x1234

    def test_signed_overflow_rejected(self) -> None:
        with pytest.raises(ValueError, match="signed"):
            encode_int16(2**15)
        with pytest.raises(ValueError, match="signed"):
            encode_int16(-(2**15) - 1)

    def test_unsigned_overflow_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsigned"):
            encode_int16(2**16, signed=False)
        with pytest.raises(ValueError, match="unsigned"):
            encode_int16(-1, signed=False)


class TestInt32OrderMatrix:
    @pytest.mark.parametrize(("word_order", "byte_order"), _ORDER_MATRIX)
    @given(value=st.integers(min_value=-(2**31), max_value=2**31 - 1))
    def test_signed_roundtrip(
        self, word_order: WordOrder, byte_order: ByteOrder, value: int
    ) -> None:
        words = encode_int32(value, signed=True, word_order=word_order, byte_order=byte_order)
        decoded = decode_int32(words, signed=True, word_order=word_order, byte_order=byte_order)
        assert decoded == value

    @pytest.mark.parametrize(("word_order", "byte_order"), _ORDER_MATRIX)
    @given(value=st.integers(min_value=0, max_value=2**32 - 1))
    def test_unsigned_roundtrip(
        self, word_order: WordOrder, byte_order: ByteOrder, value: int
    ) -> None:
        words = encode_int32(value, signed=False, word_order=word_order, byte_order=byte_order)
        decoded = decode_int32(words, signed=False, word_order=word_order, byte_order=byte_order)
        assert decoded == value

    def test_signed_negative_one_default(self) -> None:
        assert encode_int32(-1) == (0xFFFF, 0xFFFF)
        assert decode_int32((0xFFFF, 0xFFFF), signed=True) == -1
        assert decode_int32((0xFFFF, 0xFFFF), signed=False) == 0xFFFFFFFF

    def test_signed_overflow_rejected(self) -> None:
        with pytest.raises(ValueError, match="signed"):
            encode_int32(2**31, signed=True)
        with pytest.raises(ValueError, match="signed"):
            encode_int32(-(2**31) - 1, signed=True)

    def test_unsigned_overflow_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsigned"):
            encode_int32(2**32, signed=False)
        with pytest.raises(ValueError, match="unsigned"):
            encode_int32(-1, signed=False)


class TestInt64OrderMatrix:
    @pytest.mark.parametrize(("word_order", "byte_order"), _ORDER_MATRIX)
    @given(value=st.integers(min_value=-(2**63), max_value=2**63 - 1))
    def test_signed_roundtrip(
        self, word_order: WordOrder, byte_order: ByteOrder, value: int
    ) -> None:
        words = encode_int64(value, signed=True, word_order=word_order, byte_order=byte_order)
        decoded = decode_int64(words, signed=True, word_order=word_order, byte_order=byte_order)
        assert decoded == value

    @pytest.mark.parametrize(("word_order", "byte_order"), _ORDER_MATRIX)
    @given(value=st.integers(min_value=0, max_value=2**64 - 1))
    def test_unsigned_roundtrip(
        self, word_order: WordOrder, byte_order: ByteOrder, value: int
    ) -> None:
        words = encode_int64(value, signed=False, word_order=word_order, byte_order=byte_order)
        decoded = decode_int64(words, signed=False, word_order=word_order, byte_order=byte_order)
        assert decoded == value

    def test_signed_negative_one_default(self) -> None:
        assert encode_int64(-1) == (0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF)
        assert decode_int64((0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF), signed=True) == -1
        assert decode_int64((0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF), signed=False) == 0xFFFFFFFFFFFFFFFF

    def test_signed_overflow_rejected(self) -> None:
        with pytest.raises(ValueError, match="signed"):
            encode_int64(2**63, signed=True)
        with pytest.raises(ValueError, match="signed"):
            encode_int64(-(2**63) - 1, signed=True)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_decode_register_count_mismatch(self) -> None:
        with pytest.raises(ValueError, match="exactly 2"):
            decode_float32([0x3F80])
        with pytest.raises(ValueError, match="exactly 2"):
            decode_float32([0x3F80, 0x0000, 0x0000])
        with pytest.raises(ValueError, match="exactly 2"):
            decode_int32([0xFFFF])
        with pytest.raises(ValueError, match="exactly 4"):
            decode_int64([0, 0, 0])
        with pytest.raises(ValueError, match="exactly 4"):
            decode_float64([0, 0, 0])
        with pytest.raises(ValueError, match="exactly 1"):
            decode_int16([0, 0])

    def test_decode_register_value_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="register value"):
            decode_float32([0x10000, 0])
        with pytest.raises(ValueError, match="register value"):
            decode_int32([0, -1])


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------


class TestStringRoundtrip:
    def test_simple_ascii(self) -> None:
        assert encode_string("Hi", register_count=1) == (0x4869,)
        assert decode_string((0x4869,)) == "Hi"

    def test_padding_and_strip(self) -> None:
        assert encode_string("Hi", register_count=2) == (0x4869, 0x0000)
        assert decode_string((0x4869, 0x0000)) == "Hi"

    def test_no_strip_keeps_nulls(self) -> None:
        assert decode_string((0x4869, 0x0000), strip_null=False) == "Hi\x00\x00"

    def test_value_too_long_rejected(self) -> None:
        with pytest.raises(ValueError, match="does not fit"):
            encode_string("HELLO", register_count=2)

    def test_register_count_must_be_at_least_one(self) -> None:
        with pytest.raises(ValueError, match="register_count"):
            encode_string("", register_count=0)

    def test_pad_must_be_one_byte(self) -> None:
        with pytest.raises(ValueError, match="pad"):
            encode_string("Hi", register_count=2, pad=b"  ")

    def test_must_supply_exactly_one_length(self) -> None:
        with pytest.raises(ValueError, match="register_count or byte_count"):
            encode_string("Hi")
        with pytest.raises(ValueError, match="register_count or byte_count"):
            encode_string("Hi", register_count=1, byte_count=2)

    def test_byte_count_odd_rounds_up(self) -> None:
        # 15-byte field rounds up to 8 registers; last byte is null pad.
        words = encode_string("ABCDEFGHIJKLMNO", byte_count=15)
        assert len(words) == 8
        # Decode keeps the trailing null until rstrip.
        assert decode_string(words) == "ABCDEFGHIJKLMNO"

    def test_byte_order_little_swaps_pairs(self) -> None:
        # "Hi" with LITTLE byte order: the device stores 'H' in the low byte
        # of the register and 'i' in the high byte → register reads 0x6948.
        assert encode_string("Hi", register_count=1, byte_order=ByteOrder.LITTLE) == (0x6948,)
        assert decode_string((0x6948,), byte_order=ByteOrder.LITTLE) == "Hi"

    @given(s=st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=16))
    def test_ascii_roundtrip_property(self, s: str) -> None:
        rc = max(1, (len(s) + 1) // 2)
        assert decode_string(encode_string(s, register_count=rc)) == s

    def test_decode_register_value_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="register value"):
            decode_string([0x10000])


# ---------------------------------------------------------------------------
# Type-dispatched API
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_register_count_for_fixed_types(self) -> None:
        assert register_count_for(RegisterType.INT16) == 1
        assert register_count_for(RegisterType.UINT16) == 1
        assert register_count_for(RegisterType.INT32) == 2
        assert register_count_for(RegisterType.UINT32) == 2
        assert register_count_for(RegisterType.INT64) == 4
        assert register_count_for(RegisterType.UINT64) == 4
        assert register_count_for(RegisterType.FLOAT32) == 2
        assert register_count_for(RegisterType.FLOAT64) == 4
        assert register_count_for(RegisterType.STRING) is None

    def test_decode_dispatch_matches_helper(self) -> None:
        words = encode_float32(3.14)
        assert decode(words, type=RegisterType.FLOAT32) == decode_float32(words)
        assert decode((0x4869,), type=RegisterType.UINT16) == 0x4869
        assert decode((0xFFFF,), type=RegisterType.INT16) == -1
        assert decode((0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF), type=RegisterType.INT64) == -1

    def test_encode_dispatch_matches_helper(self) -> None:
        assert encode(3.14, type=RegisterType.FLOAT32) == encode_float32(3.14)
        assert encode(-1, type=RegisterType.INT16) == (0xFFFF,)
        assert encode(-1, type=RegisterType.INT64) == (0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF)

    def test_encode_string_via_dispatch(self) -> None:
        assert encode("Hi", type=RegisterType.STRING, register_count=1) == (0x4869,)
        assert encode("Hi", type=RegisterType.STRING, byte_count=2) == (0x4869,)
        assert decode((0x4869,), type=RegisterType.STRING) == "Hi"

    def test_encode_string_requires_length(self) -> None:
        with pytest.raises(ValueError, match="register_count or byte_count"):
            encode("Hi", type=RegisterType.STRING)

    def test_encode_fixed_type_rejects_byte_count(self) -> None:
        with pytest.raises(ValueError, match="byte_count is only meaningful for STRING"):
            encode(1, type=RegisterType.INT16, byte_count=2)

    def test_encode_fixed_type_rejects_disagreeing_register_count(self) -> None:
        with pytest.raises(ValueError, match="disagrees"):
            encode(1.0, type=RegisterType.FLOAT32, register_count=4)

    def test_encode_fixed_type_accepts_matching_register_count(self) -> None:
        # Passing the natural count is harmless — useful when a schema row
        # always carries register_count regardless of type.
        assert encode(1.0, type=RegisterType.FLOAT32, register_count=2) == encode_float32(1.0)

    def test_dispatch_word_byte_order_propagates(self) -> None:
        words = encode(
            1.0,
            type=RegisterType.FLOAT32,
            word_order=WordOrder.LOW_HIGH,
            byte_order=ByteOrder.LITTLE,
        )
        assert words == (0x0000, 0x803F)
        decoded = decode(
            words,
            type=RegisterType.FLOAT32,
            word_order=WordOrder.LOW_HIGH,
            byte_order=ByteOrder.LITTLE,
        )
        assert decoded == 1.0
