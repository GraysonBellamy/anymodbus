# Testing

`anymodbus.testing` exposes everything needed to write integration tests for protocol-layer code without hardware.

```python
from anymodbus.testing import FaultPlan, MockSlave, client_slave_pair
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

The pair is built on `anyserial.testing.serial_port_pair`, so:

- The framer, CRC, length-aware reader, and timing path all run unchanged.
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

## Fault injection

```python
plan = FaultPlan(
    corrupt_crc_after_n=2,        # 3rd response gets a flipped CRC bit
    delay_response_seconds=0.5,   # all responses delayed 500 ms
    wrong_slave_address=42,       # responses echo the wrong address byte
    drop_response_after_n=10,     # 11th response is dropped entirely
)

async with client_slave_pair(faults=plan) as (bus, mock):
    ...
```

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
