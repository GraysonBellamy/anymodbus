# Migration from minimalmodbus

`minimalmodbus` is sync-only and tightly coupled to `pyserial`. It's a perfectly good choice for sync bench scripts; this page is for users moving to async, multi-slave concurrency, or the rest of the AnyIO ecosystem.

## Side-by-side

```python
# minimalmodbus
import minimalmodbus
inst = minimalmodbus.Instrument("/dev/ttyUSB0", 1)
inst.serial.baudrate = 19_200
inst.serial.parity = "N"
regs = inst.read_registers(0, 4, functioncode=3)
pv = inst.read_float(0x40, functioncode=3, byteorder=minimalmodbus.BYTEORDER_LITTLE_ENDIAN_SWAP)

# anymodbus, async
from anymodbus import WordOrder, open_modbus_rtu

async with await open_modbus_rtu("/dev/ttyUSB0", baudrate=19_200, parity="none") as bus:
    inst = bus.slave(1)
    regs = await inst.read_holding_registers(0, count=4)
    pv = await inst.read_float(0x40, word_order=WordOrder.LOW_HIGH)

# anymodbus, sync
from anymodbus.sync import open_modbus_rtu
with open_modbus_rtu("/dev/ttyUSB0", baudrate=19_200, parity="none") as bus:
    inst = bus.slave(1)
    regs = inst.read_holding_registers(0, count=4, timeout=1.0)
    pv = inst.read_float(0x40, word_order=WordOrder.LOW_HIGH)
```

## API translation table

| `minimalmodbus.Instrument` | `anymodbus` |
|---|---|
| `Instrument(port, addr)` | `bus.slave(addr)` after `open_modbus_rtu(port, ...)` |
| `instrument.serial.baudrate = 19200` | `baudrate=19_200` keyword on the opener |
| `instrument.serial.parity = "N"` | `parity="none"` keyword on the opener |
| `read_bit(addr, fc=2)` | `slave.read_discrete_inputs(addr, count=1)[0]` |
| `read_bit(addr, fc=1)` | `slave.read_coils(addr, count=1)[0]` |
| `read_register(addr, fc=3)` | `slave.read_holding_registers(addr, count=1)[0]` |
| `read_register(addr, fc=4)` | `slave.read_input_registers(addr, count=1)[0]` |
| `read_registers(addr, n, fc=3)` | `slave.read_holding_registers(addr, count=n)` |
| `read_long(addr, signed=True)` | `slave.read_int32(addr, signed=True)` |
| `read_float(addr)` | `slave.read_float(addr)` |
| `write_bit(addr, value)` | `slave.write_coil(addr, on=bool(value))` |
| `write_register(addr, value)` | `slave.write_register(addr, value)` |
| `write_registers(addr, values)` | `slave.write_registers(addr, values)` |
| `write_long(addr, value)` | `slave.write_int32(addr, value)` |
| `write_float(addr, value)` | `slave.write_float(addr, value)` |

## Word / byte ordering

`minimalmodbus` uses magic-number constants:

| `minimalmodbus.BYTEORDER_*` | `anymodbus` |
|---|---|
| `BYTEORDER_BIG` (default) | `WordOrder.HIGH_LOW`, `ByteOrder.BIG` |
| `BYTEORDER_LITTLE` | `WordOrder.LOW_HIGH`, `ByteOrder.LITTLE` |
| `BYTEORDER_BIG_SWAP` | `WordOrder.HIGH_LOW`, `ByteOrder.LITTLE` |
| `BYTEORDER_LITTLE_SWAP` | `WordOrder.LOW_HIGH`, `ByteOrder.BIG` |

The `BYTEORDER_LITTLE_ENDIAN_SWAP` you see in some legacy code is `BYTEORDER_LITTLE_SWAP` — `WordOrder.LOW_HIGH, ByteOrder.BIG`.

## Things you'll like

- **Named enums** instead of magic constants (`WordOrder.LOW_HIGH` vs `BYTEORDER_LITTLE_ENDIAN_SWAP`).
- **Real exception hierarchy.** Catch `IllegalDataAddressError` specifically rather than parsing message strings; integrate cleanly with stdlib (`except TimeoutError:` works for `FrameTimeoutError`).
- **Multi-slave concurrency for free.** Spawn N tasks each calling `slave.read_*` on different addresses; the bus lock serializes them onto the wire correctly.
- **Half-duplex correctness on a shared bus.** `minimalmodbus` doesn't try to do this; you'd have to build it yourself.
- **Strict typing.** mypy-strict and pyright-strict clean.

## Things to watch for

- **No serial-attribute mutation after open.** `inst.serial.baudrate = 19_200` doesn't have an equivalent — configuration is immutable on the bus once opened. Re-open if you need to change baud.
- **Different model: bus + slaves.** `Instrument` keys on `(port, address)`; `anymodbus` separates "the wire" (`Bus`) from "a device on it" (`Slave`). One bus, many slave handles.
- **No implicit broadcast.** `inst.write_register(addr, value)` to a `minimalmodbus.Instrument(port, 0)` broadcasts; `anymodbus.Bus.slave(0)` raises `ConfigurationError`. Use `Bus.broadcast_write_register(addr, value)` explicitly — the asymmetry is deliberate, since the read FCs simply don't have `broadcast_*` variants.
- **Required parity keyword.** `open_modbus_rtu` has no parity default — too many devices in the wild use 8E1 vs 8N1 for any portable default to be safe.
- **Sync wraps async, not the other way around.** `anymodbus.sync` exists for parity, but the async path is the primary surface. Use sync only when an event loop isn't an option.
