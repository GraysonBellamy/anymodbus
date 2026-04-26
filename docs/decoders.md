# Decoders & word order

Modbus registers are 16 bits wide, but real devices store 32-bit integers and IEEE 754 floats by spreading them across two consecutive registers. The vendor chooses the word order — and they don't all agree. `anymodbus` exposes the choice as a named enum so you don't have to remember magic numbers.

## `WordOrder`

```python
from anymodbus import WordOrder

WordOrder.HIGH_LOW   # most-significant word first — equivalent to struct.pack(">f", ...)
WordOrder.LOW_HIGH   # least-significant word first
```

## `ByteOrder`

```python
from anymodbus import ByteOrder

ByteOrder.BIG     # big-endian within each 16-bit register word — Modbus norm
ByteOrder.LITTLE  # little-endian within each word — rare
```

## Defaults

```python
async def read_float(
    self,
    address: int,
    *,
    word_order: WordOrder = WordOrder.HIGH_LOW,
    byte_order: ByteOrder = ByteOrder.BIG,
) -> float: ...
```

The default of `HIGH_LOW` × `BIG` is byte-for-byte equivalent to `struct.pack(">f", value)`. The Modbus Application Protocol spec (§4.2) defines big-endian byte order *within* a single 16-bit register but does **not** standardize multi-register word ordering — `HIGH_LOW` is simply the most common convention. **There is no portable default for real devices** — vendors disagree. Always confirm against your device's protocol manual and pass the order explicitly when in doubt.

## Pure-function helpers

The same logic is exposed as pure functions for tests and downstream codecs:

```python
from anymodbus import WordOrder, ByteOrder
from anymodbus.decoders import decode_float32, encode_float32

# High-word-first, big-endian within word (struct.pack(">f", ...) layout):
value = decode_float32([0x429C, 0x977D])
# → 78.295

# Same bytes, low-word-first:
value = decode_float32(
    [0x977D, 0x429C],
    word_order=WordOrder.LOW_HIGH,
)
# → 78.295
```

## When to wrap this in a downstream library

If you're writing a device driver — say, for a specific temperature controller, drive, or PLC — bake the device's word order into your wrapper:

```python
# In your downstream package:
from anymodbus import Slave, WordOrder

DEVICE_WORD_ORDER = WordOrder.LOW_HIGH  # this device stores LSW first

async def read_setpoint(slave: Slave, address: int) -> float:
    return await slave.read_float(address, word_order=DEVICE_WORD_ORDER)
```

`anymodbus` itself stays vendor-neutral — that's the whole point.
