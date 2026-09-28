"""Tests for the reply checks and the server-side half of :mod:`anymodbus.pdu`.

Covers ``expected_count`` on the register read decoders, the request
decoders (inverse of the request encoders) and the response encoders
(inverse of the response decoders), including hypothesis round trips.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anymodbus.exceptions import ConfigurationError, ProtocolError, UnexpectedResponseError
from anymodbus.pdu import (
    ReadRequest,
    WriteMultipleCoilsRequest,
    WriteMultipleRegistersRequest,
    WriteSingleCoilRequest,
    WriteSingleRegisterRequest,
    decode_diagnostic_loopback_request,
    decode_diagnostic_loopback_response,
    decode_read_coils_request,
    decode_read_coils_response,
    decode_read_discrete_inputs_request,
    decode_read_discrete_inputs_response,
    decode_read_holding_registers_request,
    decode_read_holding_registers_response,
    decode_read_input_registers_request,
    decode_read_input_registers_response,
    decode_write_multiple_coils_request,
    decode_write_multiple_coils_response,
    decode_write_multiple_registers_request,
    decode_write_multiple_registers_response,
    decode_write_single_coil_request,
    decode_write_single_coil_response,
    decode_write_single_register_request,
    decode_write_single_register_response,
    encode_diagnostic_loopback_request,
    encode_diagnostic_loopback_response,
    encode_exception_response,
    encode_read_coils_request,
    encode_read_coils_response,
    encode_read_discrete_inputs_request,
    encode_read_discrete_inputs_response,
    encode_read_holding_registers_request,
    encode_read_holding_registers_response,
    encode_read_input_registers_request,
    encode_read_input_registers_response,
    encode_write_multiple_coils_request,
    encode_write_multiple_coils_response,
    encode_write_multiple_registers_request,
    encode_write_multiple_registers_response,
    encode_write_single_coil_request,
    encode_write_single_coil_response,
    encode_write_single_register_request,
    encode_write_single_register_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_addresses = st.integers(min_value=0, max_value=0xFFFF)
_words = st.integers(min_value=0, max_value=0xFFFF)

# ---------------------------------------------------------------------------
# expected_count on the register read decoders (a reply's quantity must match).
# ---------------------------------------------------------------------------


class TestExpectedRegisterCount:
    def test_matching_count_decodes(self) -> None:
        body = bytes((4,)) + struct.pack(">2H", 0x1234, 0x5678)
        holding = decode_read_holding_registers_response(b"\x03" + body, expected_count=2)
        inputs = decode_read_input_registers_response(b"\x04" + body, expected_count=2)
        assert holding == inputs == (0x1234, 0x5678)

    @pytest.mark.parametrize("returned", [1, 3], ids=["one-short", "one-long"])
    def test_wrong_count_raises_unexpected_response(self, returned: int) -> None:
        pdu = bytes((0x03, 2 * returned)) + struct.pack(f">{returned}H", *range(returned))
        with pytest.raises(UnexpectedResponseError, match=f"carries {returned} register"):
            decode_read_holding_registers_response(pdu, expected_count=2)

    def test_wrong_count_on_fc04_raises_unexpected_response(self) -> None:
        pdu = bytes((0x04, 2)) + struct.pack(">H", 7)
        with pytest.raises(UnexpectedResponseError):
            decode_read_input_registers_response(pdu, expected_count=2)

    def test_without_expected_count_any_length_decodes(self) -> None:
        pdu = bytes((0x03, 6)) + struct.pack(">3H", 1, 2, 3)
        assert decode_read_holding_registers_response(pdu) == (1, 2, 3)

    @pytest.mark.parametrize("bad", [0, 126])
    def test_expected_count_out_of_range_is_a_configuration_error(self, bad: int) -> None:
        pdu = bytes((0x03, 2)) + struct.pack(">H", 7)
        with pytest.raises(ConfigurationError):
            decode_read_holding_registers_response(pdu, expected_count=bad)

    def test_coil_byte_count_mismatch_is_unexpected_response(self) -> None:
        # 9 coils need 2 bytes; the reply carries 1: a reply to another request.
        with pytest.raises(UnexpectedResponseError):
            decode_read_coils_response(bytes((0x01, 1, 0xFF)), expected_count=9)


# ---------------------------------------------------------------------------
# Request decoders.
# ---------------------------------------------------------------------------


class TestRequestDecoders:
    @pytest.mark.parametrize(
        ("encode", "decode"),
        [
            (encode_read_coils_request, decode_read_coils_request),
            (encode_read_discrete_inputs_request, decode_read_discrete_inputs_request),
            (encode_read_holding_registers_request, decode_read_holding_registers_request),
            (encode_read_input_registers_request, decode_read_input_registers_request),
        ],
    )
    def test_read_requests(
        self, encode: Callable[[int, int], bytes], decode: Callable[[bytes], ReadRequest]
    ) -> None:
        assert decode(encode(0x0102, 10)) == ReadRequest(address=0x0102, count=10)

    def test_write_single_coil(self) -> None:
        on = decode_write_single_coil_request(encode_write_single_coil_request(5, on=True))
        off = decode_write_single_coil_request(encode_write_single_coil_request(5, on=False))
        assert on == WriteSingleCoilRequest(address=5, on=True)
        assert off == WriteSingleCoilRequest(address=5, on=False)

    def test_write_single_register(self) -> None:
        pdu = encode_write_single_register_request(7, 0xCAFE)
        assert decode_write_single_register_request(pdu) == WriteSingleRegisterRequest(
            address=7, value=0xCAFE
        )

    def test_write_multiple_coils(self) -> None:
        values = (True, False, True, True, False, False, True, False, True)
        pdu = encode_write_multiple_coils_request(3, values)
        assert decode_write_multiple_coils_request(pdu) == WriteMultipleCoilsRequest(
            address=3, values=values
        )

    def test_write_multiple_registers(self) -> None:
        pdu = encode_write_multiple_registers_request(9, [1, 2, 0xFFFF])
        assert decode_write_multiple_registers_request(pdu) == WriteMultipleRegistersRequest(
            address=9, values=(1, 2, 0xFFFF)
        )

    def test_diagnostic_loopback(self) -> None:
        assert decode_diagnostic_loopback_request(
            encode_diagnostic_loopback_request(b"\xab\xcd")
        ) == (b"\xab\xcd")

    @pytest.mark.parametrize(
        ("decode", "pdu"),
        [
            pytest.param(
                decode_read_holding_registers_request,
                struct.pack(">BHH", 0x03, 0, 0),
                id="fc03-count-zero",
            ),
            pytest.param(
                decode_read_holding_registers_request,
                struct.pack(">BHH", 0x03, 0, 126),
                id="fc03-count-over-spec",
            ),
            pytest.param(
                decode_read_coils_request,
                struct.pack(">BHH", 0x01, 0, 2001),
                id="fc01-count-over-spec",
            ),
            pytest.param(
                decode_read_holding_registers_request,
                struct.pack(">BH", 0x03, 0),
                id="fc03-truncated",
            ),
            pytest.param(
                decode_read_holding_registers_request,
                struct.pack(">BHH", 0x04, 0, 1),
                id="wrong-fc",
            ),
        ],
    )
    def test_bad_read_request_raises_protocol_error(
        self, decode: Callable[[bytes], ReadRequest], pdu: bytes
    ) -> None:
        with pytest.raises(ProtocolError):
            decode(pdu)

    def test_bad_coil_value_raises_protocol_error(self) -> None:
        with pytest.raises(ProtocolError, match="0xFF00"):
            decode_write_single_coil_request(struct.pack(">BHH", 0x05, 0, 0x1234))

    @pytest.mark.parametrize(
        "pdu",
        [
            pytest.param(
                struct.pack(">BHHB", 0x10, 0, 2, 3) + b"\x00\x01\x02", id="odd-byte-count"
            ),
            pytest.param(struct.pack(">BHHB", 0x10, 0, 2, 4) + b"\x00\x01", id="data-short"),
            pytest.param(struct.pack(">BHHB", 0x10, 0, 124, 248) + bytes(248), id="over-spec"),
            pytest.param(struct.pack(">BHH", 0x10, 0, 2), id="no-byte-count"),
        ],
    )
    def test_bad_write_multiple_registers_raises_protocol_error(self, pdu: bytes) -> None:
        with pytest.raises(ProtocolError):
            decode_write_multiple_registers_request(pdu)

    def test_bad_write_multiple_coils_byte_count(self) -> None:
        # 9 coils need 2 bytes.
        pdu = struct.pack(">BHHB", 0x0F, 0, 9, 1) + b"\xff"
        with pytest.raises(ProtocolError):
            decode_write_multiple_coils_request(pdu)

    def test_loopback_other_subfunction_raises_protocol_error(self) -> None:
        with pytest.raises(ProtocolError, match="sub-function"):
            decode_diagnostic_loopback_request(struct.pack(">BH", 0x08, 0x0004) + b"\x00\x00")


# ---------------------------------------------------------------------------
# Response encoders.
# ---------------------------------------------------------------------------


class TestResponseEncoders:
    def test_read_registers(self) -> None:
        assert encode_read_holding_registers_response([0x1234, 0x0001]) == (
            b"\x03\x04\x12\x34\x00\x01"
        )
        assert encode_read_input_registers_response([7]) == b"\x04\x02\x00\x07"

    def test_read_bits(self) -> None:
        # Coil 0 in bit 0 of byte 0 (app §6.1).
        assert encode_read_coils_response([True, False, True]) == b"\x01\x01\x05"
        assert encode_read_discrete_inputs_response([False] * 8 + [True]) == b"\x02\x02\x00\x01"

    def test_write_echoes(self) -> None:
        assert encode_write_single_coil_response(3, on=True) == (
            encode_write_single_coil_request(3, on=True)
        )
        assert encode_write_single_register_response(3, 9) == (
            encode_write_single_register_request(3, 9)
        )
        assert encode_write_multiple_coils_response(3, 9) == b"\x0f\x00\x03\x00\x09"
        assert encode_write_multiple_registers_response(3, 2) == b"\x10\x00\x03\x00\x02"
        assert encode_diagnostic_loopback_response(b"\x01\x02") == (
            encode_diagnostic_loopback_request(b"\x01\x02")
        )

    def test_exception_response(self) -> None:
        assert encode_exception_response(0x03, 0x02) == b"\x83\x02"

    @pytest.mark.parametrize(
        ("function_code", "exception_code"),
        [(0, 1), (0x80, 1), (3, -1), (3, 256)],
    )
    def test_exception_response_bad_arguments(
        self, function_code: int, exception_code: int
    ) -> None:
        with pytest.raises(ConfigurationError):
            encode_exception_response(function_code, exception_code)

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda: encode_read_holding_registers_response([]), id="no-registers"),
            pytest.param(
                lambda: encode_read_holding_registers_response([0] * 126), id="too-many-registers"
            ),
            pytest.param(
                lambda: encode_read_input_registers_response([0x1_0000]), id="register-value"
            ),
            pytest.param(lambda: encode_read_coils_response([False] * 2001), id="too-many-coils"),
            pytest.param(lambda: encode_write_multiple_coils_response(0, 0), id="coil-count"),
            pytest.param(
                lambda: encode_write_multiple_registers_response(0x1_0000, 1), id="address"
            ),
        ],
    )
    def test_bad_arguments_raise_configuration_error(self, call: Callable[[], bytes]) -> None:
        with pytest.raises(ConfigurationError):
            call()


# ---------------------------------------------------------------------------
# Round trips.
# ---------------------------------------------------------------------------


@given(_addresses, st.integers(min_value=1, max_value=125))
def test_register_read_request_round_trip(address: int, count: int) -> None:
    pdu = encode_read_holding_registers_request(address, count)
    assert decode_read_holding_registers_request(pdu) == ReadRequest(address=address, count=count)


@given(_addresses, st.integers(min_value=1, max_value=2000))
def test_bit_read_request_round_trip(address: int, count: int) -> None:
    pdu = encode_read_discrete_inputs_request(address, count)
    assert decode_read_discrete_inputs_request(pdu) == ReadRequest(address=address, count=count)


@given(_addresses, st.lists(_words, min_size=1, max_size=123))
def test_write_registers_request_round_trip(address: int, values: list[int]) -> None:
    pdu = encode_write_multiple_registers_request(address, values)
    request = decode_write_multiple_registers_request(pdu)
    assert request == WriteMultipleRegistersRequest(address=address, values=tuple(values))


@given(_addresses, st.lists(st.booleans(), min_size=1, max_size=1968))
def test_write_coils_request_round_trip(address: int, values: list[bool]) -> None:
    pdu = encode_write_multiple_coils_request(address, values)
    request = decode_write_multiple_coils_request(pdu)
    assert request == WriteMultipleCoilsRequest(address=address, values=tuple(values))


@given(_addresses, _words)
def test_write_single_register_round_trips(address: int, value: int) -> None:
    request_pdu = encode_write_single_register_request(address, value)
    assert decode_write_single_register_request(request_pdu) == WriteSingleRegisterRequest(
        address=address, value=value
    )
    response_pdu = encode_write_single_register_response(address, value)
    assert decode_write_single_register_response(response_pdu) == (address, value)


@given(_addresses, st.booleans())
def test_write_single_coil_response_round_trip(address: int, on: bool) -> None:
    assert decode_write_single_coil_response(encode_write_single_coil_response(address, on=on)) == (
        address,
        on,
    )


@given(st.lists(_words, min_size=1, max_size=125))
def test_register_read_response_round_trip(values: list[int]) -> None:
    holding = encode_read_holding_registers_response(values)
    assert decode_read_holding_registers_response(holding, expected_count=len(values)) == tuple(
        values
    )
    inputs = encode_read_input_registers_response(values)
    assert decode_read_input_registers_response(inputs, expected_count=len(values)) == tuple(values)


@given(st.lists(st.booleans(), min_size=1, max_size=2000))
def test_bit_read_response_round_trip(values: list[bool]) -> None:
    coils = encode_read_coils_response(values)
    assert decode_read_coils_response(coils, expected_count=len(values)) == tuple(values)
    inputs = encode_read_discrete_inputs_response(values)
    assert decode_read_discrete_inputs_response(inputs, expected_count=len(values)) == tuple(values)


@given(_addresses, st.integers(min_value=1, max_value=123))
def test_write_multiple_registers_response_round_trip(address: int, count: int) -> None:
    pdu = encode_write_multiple_registers_response(address, count)
    assert decode_write_multiple_registers_response(pdu) == (address, count)


@given(_addresses, st.integers(min_value=1, max_value=1968))
def test_write_multiple_coils_response_round_trip(address: int, count: int) -> None:
    pdu = encode_write_multiple_coils_response(address, count)
    assert decode_write_multiple_coils_response(pdu) == (address, count)


@given(st.binary(min_size=2, max_size=2))
def test_loopback_round_trip(data: bytes) -> None:
    assert decode_diagnostic_loopback_request(encode_diagnostic_loopback_request(data)) == data
    assert decode_diagnostic_loopback_response(encode_diagnostic_loopback_response(data)) == data
