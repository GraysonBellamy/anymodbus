# Length-aware framer

The rx-side state machine that reads response ADUs without relying on rx-timing for known function codes. This is the technical heart of the library.

> Source: [`anymodbus.framer.read_response_adu`](https://github.com/GraysonBellamy/anymodbus/blob/main/src/anymodbus/framer.py). For the design rationale, see [DESIGN §6.3](https://github.com/GraysonBellamy/anymodbus/blob/main/DESIGN.md#63-rx-length-aware-read-state-machine).

## Why length-aware

The Modbus over Serial Line spec defines an inter-frame idle gap of 3.5 character-times as the framing signal between consecutive ADUs. A pure gap-based reader receives bytes until the bus has been quiet for ≥ t3.5, then declares the frame done.

This works on hardware UARTs with sub-millisecond scheduling. It does **not** work on Linux/macOS userspace, where response bytes routinely arrive in 2–3 ms chunks under normal scheduler jitter — gap-only readers either truncate frames or fuse two frames into one.

`anymodbus` solves this by parsing the response header and looking up the exact remaining length per function code. The 3.5-char gap is still enforced **on the tx side** (so the master never violates the spec), but the rx side never depends on it for known FCs.

## How the state machine reads a response

The full pseudocode lives in `read_response_adu`. The shape:

1. **Read 2 bytes** — the slave-address byte and the function-code byte.
2. **Drain stray frames.** If the slave-address byte does not match the slave we sent the request to, drain the rest of that frame using the t1.5 idle gap and keep waiting under the same enclosing deadline. Per *Modbus over Serial Line v1.02 §2.4.1*, a stray reply does **not** abort the response timeout.
3. **Reject FC 0** as `ProtocolError` per *app §4.1*.
4. **Detect exception responses** (FC high bit set) — read the 3-byte tail (exception code + CRC), verify CRC first, then raise the matching `ModbusExceptionResponse` subclass.
5. **Dispatch by FC** to one of four length branches (see below).
6. **Verify CRC** over the full ADU. CRC mismatch raises `CRCError` (retryable under the default `RetryPolicy`).
7. **Return** `(slave_address, pdu)` — the trailing CRC is stripped before the PDU is handed back.

## The four length branches

All four are driven by tables in [framer.py](https://github.com/GraysonBellamy/anymodbus/blob/main/src/anymodbus/framer.py) — single source of truth, asserted directly against spec fixtures in `tests/unit/test_framer.py`.

### 1. Fixed-tail FCs (`_FIXED_TAIL`)

| FC | Bytes after header | Spec |
|----|-------------------|------|
| 0x05 Write Single Coil | 6 | *app §6.5* |
| 0x06 Write Single Register | 6 | *app §6.6* |
| 0x0F Write Multiple Coils | 6 | *app §6.11* |
| 0x10 Write Multiple Registers | 6 | *app §6.12* |
| 0x16 Mask Write Register | **8** | *app §6.16* |

> **Spec gotcha:** FC 0x16's response is `addr(2) + AND(2) + OR(2) + crc(2)` = 8 bytes, **not** 6. Lumping it in with the other writes mis-frames the next response on the bus.

### 2. One-byte byte_count FCs (`_BYTE_COUNT_1B`)

FCs 0x01, 0x02, 0x03, 0x04, 0x17 carry a 1-byte byte_count immediately after the FC byte. The reader pulls that byte, then reads `byte_count + 2` more bytes (data + CRC).

A `byte_count` > 250 raises `FrameError` immediately. *app §4.1* caps the PDU at 253 bytes; a malformed slave returning `0xFF` would otherwise force a ~257-byte speculative read.

### 3. Known-but-unsupported FCs (`_KNOWN_UNSUPPORTED`)

FCs 0x07, 0x08, 0x0B, 0x0C, 0x11, 0x14, 0x15, 0x18, 0x2B are defined by the spec but not implemented in v0.1. The framer recognises them and raises `ModbusUnsupportedFunctionError` rather than letting them corrupt the stream by falling into the gap-based fallback.

### 4. Truly unknown FCs (gap-based fallback)

For vendor-private FCs (user-defined ranges 65–72, 100–110, or anything else not in the above tables), the only option is the t1.5-character idle-gap reader. This is the **only** path that depends on rx timing, and it's the only path that can mis-frame under userspace scheduler jitter — but it never fires for any standard FC.

## Exception responses

Exception responses (FC | 0x80) are handled inline:

1. Read the 3-byte tail (exception code + CRC).
2. **Verify CRC first** — a corrupted exception ADU surfaces as `CRCError` (retryable), not as the slave-reported exception code (which we'd have no business trusting).
3. Confirm the echoed FC matches the request (mismatch raises `UnexpectedResponseError`).
4. Translate the exception code via [`code_to_exception`](https://github.com/GraysonBellamy/anymodbus/blob/main/src/anymodbus/exceptions.py) and raise the matching `ModbusExceptionResponse` subclass.

See [exceptions.md](exceptions.md) for the full code → class mapping.

## Tests

The framer state machine has 100% branch coverage in [tests/unit/test_framer.py](https://github.com/GraysonBellamy/anymodbus/blob/main/tests/unit/test_framer.py), including the regression cases called out above:

- FC 0x16 length-8 vs length-6.
- `byte_count` > 250 → `FrameError`, no large allocation.
- Stray slave address followed by valid frame → discards stray, returns valid.
- Bad CRC on exception response → `CRCError`, not the slave's exception.
- FC 0 → `ProtocolError`.
- Each `_KNOWN_UNSUPPORTED` FC → `ModbusUnsupportedFunctionError`.
