# Quickstart

!!! warning
    The snippets below show the **planned** v0.1 API. They will not run until the implementation lands. Track progress in the [changelog](changelog.md).

## Install

```bash
uv add anymodbus
# or
pip install anymodbus
```

For trio support:

```bash
uv add "anymodbus[trio]"
```

## Async, single slave

```python
import anyio
from anymodbus import open_modbus_rtu


async def main() -> None:
    async with await open_modbus_rtu(
        "/dev/ttyUSB0", baudrate=19_200, parity="even"
    ) as bus:
        slave = bus.slave(address=1)
        regs = await slave.read_holding_registers(0x0040, count=2)
        print(regs)


anyio.run(main)
```

## Sync (for scripts)

```python
from anymodbus.sync import open_modbus_rtu

with open_modbus_rtu("/dev/ttyUSB0", baudrate=19_200, parity="even") as bus:
    slave = bus.slave(1)
    regs = slave.read_holding_registers(0, count=4, timeout=1.0)
    print(regs)
```

## Reading a 32-bit float across two registers

Word order varies by device. The default matches the Modbus Application Protocol spec's worked example (high-word-first, big-endian within word) — equivalent to `struct.pack(">f", ...)`.

```python
async with await open_modbus_rtu(
    "/dev/ttyUSB0", baudrate=19_200, parity="even"
) as bus:
    slave = bus.slave(address=1)
    value = await slave.read_float(0x0040)  # WordOrder.HIGH_LOW default
    print(f"value = {value:.2f}")
```

If your device stores the low word first, pass it explicitly:

```python
from anymodbus import WordOrder
value = await slave.read_float(0x0040, word_order=WordOrder.LOW_HIGH)
```

There is no portable default for Modbus float layout — always check your device's protocol manual. See [Decoders & word order](decoders.md) for the full matrix.

## Wrapping an existing serial port (RS-485)

Most USB-RS485 adapters need explicit RS-485 configuration. Use `anyserial` directly to open the port with the settings you want, then hand it to `Bus`:

```python
from anyserial import open_serial_port, SerialConfig, RS485Config, Parity
from anymodbus import Bus

port = await open_serial_port(
    "/dev/ttyUSB0",
    SerialConfig(
        baudrate=19_200,
        parity=Parity.EVEN,
        rs485=RS485Config(
            enabled=True,
            rts_on_send=True,
            rts_after_send=False,
        ),
    ),
)
async with Bus(port) as bus:
    regs = await bus.slave(1).read_holding_registers(0, count=4)
```

## Concurrent fan-out across many buses

One event loop handles N independent buses concurrently. Same-bus requests serialize automatically through the bus lock:

```python
import anyio
from anymodbus import open_modbus_rtu


async def poll_one(path: str, results: dict[str, tuple[int, ...]]) -> None:
    async with await open_modbus_rtu(path, baudrate=19_200, parity="even") as bus:
        results[path] = await bus.slave(1).read_holding_registers(0, count=4)


async def main() -> None:
    paths = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"]
    results: dict[str, tuple[int, ...]] = {}
    async with anyio.create_task_group() as tg:
        for p in paths:
            tg.start_soon(poll_one, p, results)
    for path, regs in results.items():
        print(path, regs)


anyio.run(main)
```

## Testing without hardware

```python
from anymodbus.testing import client_slave_pair

async with client_slave_pair(slave_address=1) as (bus, mock):
    mock.holding_registers[0:4] = [10, 20, 30, 40]
    regs = await bus.slave(1).read_holding_registers(0, count=4)
    assert regs == (10, 20, 30, 40)
```

## Cancellation

Standard AnyIO scopes work with no library-side timer:

```python
with anyio.move_on_after(0.5):
    regs = await slave.read_holding_registers(0, count=2)
```

Outer scopes preempt the bus's per-request timeout (`BusConfig.request_timeout`), so a tight outer deadline always wins.
