# RTU framing

Modbus RTU is the binary, serial-line variant of Modbus. Each transaction is a single ADU (Application Data Unit) on the wire:

```
| slave_address (1 byte) | PDU (1-253 bytes) | CRC-16 LE (2 bytes) |
```

The PDU is `function_code (1 byte) | body (variable)`. Frame boundaries are signaled by **idle silence on the bus** — at least 3.5 character-times of no activity between frames, and no more than 1.5 character-times of silence within a frame. At baud rates above 19200 the spec clamps both gaps to fixed values (1.75 ms and 0.75 ms respectively).

## Why this matters

Modern operating systems cannot reliably deliver microsecond-accurate idle-gap detection on commodity USB-serial hardware. A naïve framer that watches for "no bytes for N microseconds" will drop frames or fuse them together depending on scheduling jitter.

`anymodbus` solves this two ways:

1. **Tx-side, enforced.** Before sending a request, we sleep until at least 3.5 character-times have passed since the last bus activity. This is honest and deterministic.
2. **Rx-side, length-aware.** For every standard function code, the response length is computable after at most 3 bytes (function code byte tells us if it's an exception, byte count tells us the body length). We read by length, not by timing. The timing-gap fallback fires only for unknown function codes.

See [Length-aware framer](framing.md) for the state machine.

## Wire defaults

- Spec-conformant: 8 data bits, **even parity**, 1 stop bit (8E1).
- Common in real devices: 8N1 (no parity, two stop bits to keep the bit count steady) or 8O1.

There is no portable default. The `open_modbus_rtu(...)` convenience opener takes `baudrate=` and `parity=` as required keyword arguments — you must pick. Mismatched parity silently drops every frame, so making this an explicit choice up front is worth the small ergonomic cost.

## Inter-frame timing reference

| Baud | 3.5 char-times | 1.5 char-times |
|---|---|---|
| 9600 | 4.01 ms | 1.72 ms |
| 19200 | 2.01 ms | 0.86 ms |
| 38400 | **1.75 ms** (clamped) | **0.75 ms** (clamped) |
| 57600 | 1.75 ms | 0.75 ms |
| 115200 | 1.75 ms | 0.75 ms |

(Character time = 11 bits / baud, accounting for start, 8 data, parity, stop.)
