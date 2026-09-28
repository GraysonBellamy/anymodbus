"""Bad arguments raise :class:`ConfigurationError`, which is still a :class:`ValueError`.

One case per call site that validates a caller-supplied argument, across the
PDU codec, both ADU encoders, the register decoders, the broadcast path and
the mock slave. Callers that catch :class:`ModbusError` see these, and callers
that catch :class:`ValueError` keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from anymodbus import (
    Bus,
    BusConfig,
    ConfigurationError,
    ModbusError,
    RegisterType,
)
from anymodbus.decoders import (
    decode_float32,
    decode_int16,
    decode_string,
    encode,
    encode_int16,
    encode_string,
)
from anymodbus.framer import encode_adu
from anymodbus.framer_ascii import encode_ascii_adu
from anymodbus.pdu import (
    decode_read_coils_response,
    encode_diagnostic_loopback_request,
    encode_read_holding_registers_request,
    encode_write_single_register_request,
)
from anymodbus.testing import MockSlave

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeStream:
    """Stand-in stream: the broadcast argument checks run before any I/O."""

    async def aclose(self) -> None:  # pragma: no cover - never called
        pass


def _bus() -> Bus:
    return Bus(_FakeStream(), config=BusConfig())  # type: ignore[arg-type]


_SYNC_CASES: list[tuple[str, Callable[[], object]]] = [
    ("pdu-address", lambda: encode_read_holding_registers_request(-1, 1)),
    ("pdu-register-value", lambda: encode_write_single_register_request(0, 0x1_0000)),
    ("pdu-quantity", lambda: encode_read_holding_registers_request(0, 126)),
    (
        "pdu-expected-count",
        lambda: decode_read_coils_response(b"\x01\x01\x00", expected_count=0),
    ),
    ("pdu-loopback-data", lambda: encode_diagnostic_loopback_request(b"\x00")),
    ("rtu-slave-address", lambda: encode_adu(slave_address=256, pdu=b"\x03")),
    ("rtu-empty-pdu", lambda: encode_adu(slave_address=1, pdu=b"")),
    ("ascii-slave-address", lambda: encode_ascii_adu(slave_address=256, pdu=b"\x03")),
    ("ascii-empty-pdu", lambda: encode_ascii_adu(slave_address=1, pdu=b"")),
    ("decoders-word-count", lambda: decode_float32([1])),
    ("decoders-word-value", lambda: decode_int16([0x1_0000])),
    ("decoders-signed-range", lambda: encode_int16(40_000)),
    ("decoders-unsigned-range", lambda: encode_int16(-1, signed=False)),
    ("decoders-string-word-value", lambda: decode_string([0x1_0000])),
    ("decoders-string-length-args", lambda: encode_string("x")),
    ("decoders-string-pad", lambda: encode_string("x", register_count=1, pad=b"ab")),
    ("decoders-string-register-count", lambda: encode_string("x", register_count=0)),
    ("decoders-string-byte-count", lambda: encode_string("x", byte_count=0)),
    ("decoders-string-too-long", lambda: encode_string("abc", register_count=1)),
    (
        "decoders-dispatch-register-count",
        lambda: encode(1, type=RegisterType.INT32, register_count=1),
    ),
    ("decoders-dispatch-byte-count", lambda: encode(1, type=RegisterType.INT32, byte_count=4)),
    ("mock-slave-address", lambda: MockSlave(address=0)),
]


@pytest.mark.parametrize(("name", "call"), _SYNC_CASES, ids=[name for name, _ in _SYNC_CASES])
def test_bad_argument_raises_configuration_error(name: str, call: Callable[[], object]) -> None:
    with pytest.raises(ConfigurationError) as ei:
        call()
    assert isinstance(ei.value, ModbusError), name
    assert isinstance(ei.value, ValueError), name


@pytest.mark.anyio
@pytest.mark.parametrize(
    "request_pdu",
    [
        pytest.param(b"", id="empty-pdu"),
        pytest.param(b"\x03\x00\x00\x00\x01", id="read-fc"),
    ],
)
async def test_bad_broadcast_raises_configuration_error(request_pdu: bytes) -> None:
    bus = _bus()
    with pytest.raises(ConfigurationError) as ei:
        await bus._broadcast(request_pdu=request_pdu)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(ei.value, ValueError)
