"""Tests for the small enums and predicate helpers in :mod:`anymodbus._types`."""

from __future__ import annotations

import pytest

from anymodbus import (
    FunctionCode,
    is_idempotent_function,
    is_read_function,
    is_write_function,
)


class TestFunctionPredicates:
    @pytest.mark.parametrize("fc", [0x01, 0x02, 0x03, 0x04])
    def test_reads_are_read_and_idempotent(self, fc: int) -> None:
        assert is_read_function(fc)
        assert is_idempotent_function(fc)
        assert not is_write_function(fc)

    @pytest.mark.parametrize("fc", [0x05, 0x06, 0x0F, 0x10, 0x16])
    def test_writes_are_write_and_not_idempotent(self, fc: int) -> None:
        assert is_write_function(fc)
        assert not is_idempotent_function(fc)
        assert not is_read_function(fc)

    def test_read_write_multiple_is_neither_pure_read_nor_idempotent(self) -> None:
        # FC 0x17 has a write half — must not be auto-retried.
        assert not is_read_function(FunctionCode.READ_WRITE_MULTIPLE_REGISTERS)
        assert not is_write_function(FunctionCode.READ_WRITE_MULTIPLE_REGISTERS)
        assert not is_idempotent_function(FunctionCode.READ_WRITE_MULTIPLE_REGISTERS)

    def test_unknown_codes_are_not_anything(self) -> None:
        assert not is_read_function(0xFE)
        assert not is_write_function(0xFE)
        assert not is_idempotent_function(0xFE)
