# Testing

`anymodbus.testing` exposes everything needed to write integration tests for protocol-layer code without hardware.

```python
from anymodbus.testing import (
    FaultPlan,
    MockServer,
    MockSlave,
    QuantityLimits,
    ServerException,
    client_server_pair,
    client_slave_pair,
)
```

## In-memory bus + slave pair

```python
async with client_slave_pair(slave_address=1) as (bus, mock):
    mock.holding_registers[0:4] = [10, 20, 30, 40]

    regs = await bus.slave(1).read_holding_registers(0, count=4)
    assert regs == (10, 20, 30, 40)

    await bus.slave(1).write_register(7, 0xCAFE)
    assert mock.holding_registers[7] == 0xCAFE
```

The pair is built on `anyserial.testing.serial_port_pair`, whose ends are real `anyserial.SerialPort`s, so:

- The framer, CRC, length-aware reader, and timing path all run unchanged.
- The bus sees the configured `baudrate` for its auto timing, and uses the ports' drain and input reset.
- Bytes flow through the same byte-stream API the real bus uses.
- Tests exercising scheduler-jitter behaviour (chunked receives, idle-gap drain) are real, not stubbed.

The mock slave runs in its own task within the context-managed task group; on exit, the slave is cancelled and both ends of the pair are closed.

## Mutable register banks

```python
class MockSlave:
    address: int
    coils: bytearray
    discrete_inputs: bytearray
    holding_registers: list[int]
    input_registers: list[int]
```

All four banks are mutable mid-test — useful for "the slave updated a sensor reading" patterns. Sizes default to 256 entries each; override via `register_count=` / `coil_count=` on `client_slave_pair`.

## Disabling specific FCs

To simulate a slave that doesn't support a function code (so probe tests get `Capability.UNSUPPORTED`):

```python
async with client_slave_pair(
    disabled_function_codes=frozenset({0x02, 0x04}),
) as (bus, mock):
    ...  # FC 2 / 4 against this mock raise IllegalFunctionError
```

## Quantity limits

Real devices often cap a request below the spec maximum. `QuantityLimits` makes a mock slave refuse larger requests with `IllegalDataValueError`, as such a device would:

```python
async with client_slave_pair(limits=QuantityLimits(read_registers=64)) as (bus, mock):
    await bus.slave(1).read_holding_registers(0, count=64)  # fine
    await bus.slave(1).read_holding_registers(0, count=65)  # IllegalDataValueError
```

## Simulating a device: override `handle`

`MockSlave.handle(request_pdu) -> response_pdu` answers one request. Override it to simulate a particular device, and call `super().handle(...)` for the requests the register banks should answer. Raise `ServerException(code)` to answer with a Modbus exception response:

```python
from anymodbus import ExceptionCode
from anymodbus.pdu import decode_read_input_registers_request


class Analyzer(MockSlave):
    def handle(self, request_pdu: bytes) -> bytes:
        if request_pdu[0] == 0x04:
            request = decode_read_input_registers_request(request_pdu)
            if request.address >= 0x0100:
                raise ServerException(ExceptionCode.ILLEGAL_DATA_ADDRESS)
        return super().handle(request_pdu)
```

A malformed request, or one whose quantity is outside the spec range, is answered with `ILLEGAL_DATA_VALUE`; a function code the slave doesn't implement with `ILLEGAL_FUNCTION`.

## Several slaves on one line

`MockSlave.serve()` owns its stream, so two slaves serving the same stream would compete for bytes. `MockServer` owns the stream instead, reads each request once, and routes it by address: an absent address gets no reply, a request with a bad checksum is dropped, and a broadcast is applied by every slave and answered by none. `client_server_pair` wires one to a bus:

```python
one, two = Analyzer(address=1), MockSlave(address=2)
async with client_server_pair(one, two) as (bus, server):
    await bus.slave(1).read_input_registers(0, count=4)
    await bus.slave(2).write_register(0, 7)
```

It accepts `on_request=callback`, called with `(address, request_pdu)` for every valid request — handy for asserting what went out on the line and when.

## Writing your own server

The pieces `MockServer` is built from are public, for a simulator that doesn't fit `MockSlave`:

- `anymodbus.framing.get_framer(framing).read_request_adu(stream, inter_char_idle=...)` reads one request frame for any address and returns `(address, request_pdu)`, raising `ChecksumError` for a bad checksum (the frame is consumed) and `anyio.EndOfStream` when the stream closes between frames.
- `anymodbus.pdu` has a request decoder per function code (`decode_read_holding_registers_request`, `decode_write_multiple_registers_request`, …, returning small frozen dataclasses) and a response encoder per function code (`encode_read_holding_registers_response`, …, and `encode_exception_response`).
- `MockSlave.send_response(stream, response_pdu)` sends a reply as that slave, with its `FaultPlan` applied.

## Fault injection

```python
plan = FaultPlan(
    corrupt_crc_after_n=2,  # 3rd response gets a flipped CRC bit
    delay_response_seconds=0.5,  # all responses delayed 500 ms
    wrong_slave_address=42,  # responses echo the wrong address byte
    drop_response_after_n=10,  # 11th response is dropped entirely
)

async with client_slave_pair(faults=plan) as (bus, mock):
    ...
```

The `*_after_n` fields name one response by its 0-based index among the responses the slave sends, and fire only on that response. `MockSlave.response_count` is the index of the next one.

Faults compose. Use them to exercise:

- **CRC corruption** → verifies the retry loop kicks in for `CRCError`.
- **Response delay** → verifies `request_timeout` and outer-scope cancellation behave correctly.
- **Wrong slave address** → verifies the unexpected-slave-drain branch in the framer keeps waiting under the same deadline (per *serial §2.4.1*).
- **Dropped response** → verifies `FrameTimeoutError` raises after the deadline.

## Hardware-gated tests

Tests that need real hardware are marked `@pytest.mark.hardware` and deselected by default. Opt in with environment variables and the `hardware` marker:

```bash
ANYMODBUS_TEST_PORT=/dev/ttyUSB0 \
ANYMODBUS_TEST_SLAVE_ADDRESS=1 \
    pytest -m hardware
```

The fixture for hardware tests reads those env vars; missing-port skips the test rather than failing.

## Choosing the test backend

`anymodbus`'s own test suite parametrises across asyncio, asyncio+uvloop (when installed), and trio via the AnyIO pytest plugin. Downstream device libraries that import `anymodbus.testing` get the same matrix for free if they configure the `anyio_backend` fixture identically; otherwise they default to asyncio.

## When to mock vs hit hardware

- **Mock first.** Every protocol-layer assertion (FC encoding, exception mapping, retry behaviour, broadcast turnaround) can be made with `client_slave_pair`. These tests are fast, deterministic, and run in CI.
- **Hardware second.** Reserve hardware-marked tests for things that genuinely depend on the wire — parity validation against the actual UART, RTS toggle timing, vendor-specific quirks. Don't gate basic FC coverage on hardware.
