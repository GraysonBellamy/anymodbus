"""Tests for :mod:`anymodbus.decoders` — float32 / int32 / string helpers.

The default ordering (``HIGH_LOW`` + ``BIG``) is asserted to be equivalent
to ``struct.pack(">f", ...)`` / ``struct.pack(">i", ...)``. The other three
combinations of word_order x byte_order are exercised both by direct wire
fixtures and by round-trip property tests.
"""

from __future__ import annotations

import math
import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anymodbus._types import ByteOrder, WordOrder
from anymodbus.decoders import (
    decode_float32,
    decode_int32,
    decode_string,
    encode_float32,
    encode_int32,
    encode_string,
)

_ORDER_MATRIX = [
    (WordOrder.HIGH_LOW, ByteOrder.BIG),
    (WordOrder.HIGH_LOW, ByteOrder.LITTLE),
    (WordOrder.LOW_HIGH, ByteOrder.BIG),
    (WordOrder.LOW_HIGH, ByteOrder.LITTLE),
]


# ---------------------------------------------------------------------------
# Defaults match struct.pack(">f", ...) / struct.pack(">i", ...).
# ---------------------------------------------------------------------------


class TestCanonicalDefaults:
    """HIGH_LOW + BIG must match the simple struct big-endian interpretation."""

    @given(value=st.floats(allow_nan=False, allow_infinity=False, width=32))
    def test_float_default_matches_struct_be(self, value: float) -> None:
        words = encode_float32(value)
        canonical = struct.pack(f">{len(words)}H", *words)
        assert canonical == struct.pack(">f", value)

    def test_float_one_default(self) -> None:
        # 1.0 in IEEE 754 = 0x3F800000 → words (0x3F80, 0x0000).
        assert encode_float32(1.0) == (0x3F80, 0x0000)
        assert decode_float32((0x3F80, 0x0000)) == 1.0

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


# ---------------------------------------------------------------------------
# Word/byte-order matrix — round-trip property test
# ---------------------------------------------------------------------------


class TestFloat32OrderMatrix:
    @pytest.mark.parametrize(("word_order", "byte_order"), _ORDER_MATRIX)
    @given(value=st.floats(allow_nan=False, allow_infinity=False, width=32))
    def test_roundtrip_all_orderings(
        self, word_order: WordOrder, byte_order: ByteOrder, value: float
    ) -> None:
        words = encode_float32(value, word_order=word_order, byte_order=byte_order)
        decoded = decode_float32(words, word_order=word_order, byte_order=byte_order)
        # width=32 keeps `value` exactly representable as float32, so the
        # round-trip is bit-exact (modulo signed-zero, which we treat as equal).
        if value == 0.0:
            assert decoded == 0.0
        else:
            assert math.isclose(decoded, value, rel_tol=0, abs_tol=0)

    def test_low_high_big_swaps_words(self) -> None:
        # 1.0 → canonical bytes 3F 80 00 00. With LOW_HIGH the LSW (0x0000)
        # is first, MSW (0x3F80) second.
        assert encode_float32(1.0, word_order=WordOrder.LOW_HIGH) == (0x0000, 0x3F80)
        assert decode_float32((0x0000, 0x3F80), word_order=WordOrder.LOW_HIGH) == 1.0

    def test_high_low_little_swaps_intra_word_bytes(self) -> None:
        # 1.0 → canonical 3F 80 00 00. With LITTLE within each word, the
        # high word's bytes go (0x80, 0x3F) on the wire → register reads
        # back as 0x803F. Low word stays 0x0000.
        words = encode_float32(1.0, byte_order=ByteOrder.LITTLE)
        assert words == (0x803F, 0x0000)
        assert decode_float32(words, byte_order=ByteOrder.LITTLE) == 1.0

    def test_low_high_little_full_reverse(self) -> None:
        # 1.0 with both orderings flipped is equivalent to little-endian on
        # the entire 4-byte payload.
        words = encode_float32(1.0, word_order=WordOrder.LOW_HIGH, byte_order=ByteOrder.LITTLE)
        assert words == (0x0000, 0x803F)


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
        # -1 as signed 32-bit = 0xFFFFFFFF → registers (0xFFFF, 0xFFFF).
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


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_decode_requires_two_registers(self) -> None:
        with pytest.raises(ValueError, match="exactly 2"):
            decode_float32([0x3F80])
        with pytest.raises(ValueError, match="exactly 2"):
            decode_float32([0x3F80, 0x0000, 0x0000])
        with pytest.raises(ValueError, match="exactly 2"):
            decode_int32([0xFFFF])

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
        # "Hi" → bytes 0x48, 0x69 → register 0x4869.
        assert encode_string("Hi", register_count=1) == (0x4869,)
        assert decode_string((0x4869,)) == "Hi"

    def test_padding_and_strip(self) -> None:
        # "Hi" packed into 2 registers → 'H' 'i' '\x00' '\x00' → 0x4869, 0x0000.
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

    @given(s=st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=16))
    def test_ascii_roundtrip_property(self, s: str) -> None:
        # Round up to the smallest register count that fits.
        rc = max(1, (len(s) + 1) // 2)
        assert decode_string(encode_string(s, register_count=rc)) == s

    def test_decode_register_value_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="register value"):
            decode_string([0x10000])
