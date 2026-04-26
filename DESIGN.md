# anymodbus — Design Plan

**Status:** Draft v0.1 (post spec audit)
**Target Python:** 3.13+
**Target AnyIO:** ≥ 4.13
**Transport floor:** `anyserial >= 0.1.1` (also accepts any `anyio.abc.ByteStream`)
**License:** MIT
**Author:** Grayson Bellamy
**Last updated:** 2026-04-25

---

## 1. Mission

A small, async-native, half-duplex-correct Modbus RTU **client** for Python, built on `anyserial` and AnyIO. It is intentionally protocol-only — no servers, no device-specific knowledge. It serves as the substrate for downstream device libraries that wrap a single vendor's register map and conventions on top.

The niche it fills: nothing in the ecosystem today is AnyIO-native, half-duplex-correct on a shared bus, *and* length-aware in its rx framer. `anymodbus` is.

## 2. Goals / Non-goals

### Goals

1. RTU client over any AnyIO `ByteStream` (concrete default: `anyserial.SerialPort`).
2. Half-duplex single-master correctness — one outstanding transaction on the bus, period.
3. Length-aware frame reader with timing-gap fallback only for unknown function codes.
4. Honest 3.5-character pre-tx idle gap (tx-side, not relying on rx timing).
5. Pure-function PDU codecs (encode/decode separated from I/O), property-tested.
6. Per-slave handle (`Slave(bus, address)`) for ergonomic multi-drop access.
7. Exception hierarchy that multi-inherits from stdlib + AnyIO bases (matches anyserial's idiom) and maps Modbus exception codes 1–11 to specific subclasses.
8. Explicit word/byte-order enums for 32-bit values, with the most-common `HIGH_LOW` default (`struct.pack(">f", ...)` equivalent) and `LOW_HIGH` opt-in. (Word order is *not* spec-defined; only intra-register byte order is. See §11.)
9. Sync wrapper with the same shared-portal pattern as `anyserial.sync`.
10. `anymodbus.testing` exposing a `MockSlave` register bank + `client_slave_pair()` helper + `FaultPlan`.
11. Capability model (`SUPPORTED`/`UNSUPPORTED`/`UNKNOWN`) for function codes, populated from probe results.
12. `mypy --strict` + `pyright --strict` clean, full PEP 561.

### Non-goals (v1.x)

- Modbus servers (slave-side). Out of scope.
- Modbus ASCII transport.
- Modbus TCP / RTU-over-TCP. (Architectural seam preserved — re-add later as `anymodbus.tcp` without breaking the `Bus` API.)
- File-record FCs (0x14/0x15) — defer to v0.3+ unless a user asks.
- Serial-line-only diagnostic FCs: 0x07 Read Exception Status, 0x08 Diagnostics, 0x0B Get Comm Event Counter, 0x0C Get Comm Event Log, 0x11 Report Server ID, 0x18 Read FIFO Queue. Out of scope for v0.1; add per user demand. The framer recognizes them as known-but-unsupported and raises `ModbusUnsupportedFunctionError` rather than mis-framing on the wire. Special case to keep on the radar when these are added: FC 0x08 sub-function 0x04 (Force Listen Only Mode) returns no response — must be handled like a broadcast.
- Encapsulated Interface Transport (FC 0x2B). MEI Type 0x0E (Read Device Identification) is planned for v0.2; MEI Type 0x0D (CANopen General Reference) is out of scope.
- Built-in device drivers beyond a tiny `examples/` directory. Vendor-specific register maps and quirks live in their own downstream packages.

## 3. Architecture overview

Two crisp layers: **transport-agnostic codec** (pure functions, easy to test) and **bus client** (owns the lock, timing, framing, and stream).

```
┌───────────────────────────────────────────────┐
│   anymodbus.Bus(stream, *, options)           │  ← single-master, lock-protected
│   ├─ anyio.Lock + tx-gap timer                │
│   ├─ Framer.read_response(stream, expect_fc)  │  ← length-aware
│   └─ Codec (pure)                             │  ← encode/decode PDU
└───────────────────────────────────────────────┘
        ▲                            ▲
        │                            │
   anyserial.SerialPort        any anyio.ByteStream
   (RTU on real hardware)      (test pair, future TCP, ...)
```

**Key invariant:** the `Bus` holds an `anyio.Lock`. Concurrent `await slave.read(...)` calls from any task graph serialize through it — at most one outstanding transaction on the wire at a time, by construction.

## 4. Module layout (src-layout, mirrors anyserial)

```
src/anymodbus/
├── __init__.py            # Public re-exports + __version__
├── _version.py            # hatch-vcs generated
├── py.typed
├── _types.py              # FunctionCode, ExceptionCode, WordOrder, ByteOrder enums + small dataclasses
├── exceptions.py          # ModbusError tree + code_to_exception translator
├── capabilities.py        # ModbusCapabilities + ModbusStreamAttribute
├── config.py              # BusConfig, RetryPolicy, TimingConfig (frozen dataclasses)
├── crc.py                 # crc16_modbus(data) — 256-entry table-precomputed
├── pdu.py                 # PDU encode/decode pure functions
├── framer.py              # Length-aware ADU reader + ADU writer; uses crc + pdu
├── bus.py                 # Bus class — owns stream, lock, transactions
├── slave.py               # Slave handle bound to (bus, address); high-level methods
├── decoders.py            # int16/32/64 + float32/64 + string helpers, plus type-dispatched decode/encode
├── stream.py              # open_modbus_rtu(path, ...) convenience opener
├── sync.py                # Blocking wrappers; reuses anyserial portal hook
├── testing.py             # MockSlave, client_slave_pair, FaultPlan re-export
└── _mock/                 # Private: mock slave register bank, fault injection
    ├── __init__.py
    ├── slave.py
    ├── pair.py
    └── faults.py
```

Naming: `Modbus*` prefix on every public class; no `Master`/`Slave` ambiguity at the public surface — `Bus` and `Slave` are the user-facing nouns, and the `Slave` name is correct because the *device* is the slave (RTU vocabulary).

## 5. Public API sketch

### 5.1 Opening a bus

```python
from anymodbus import Bus, BusConfig, RetryPolicy, open_modbus_rtu

# Convenience opener: opens the serial port and wraps in a Bus.
# baudrate and parity are required — Modbus RTU defaults vary too widely
# for any portable default to be safe. Consult your device's manual.
bus = await open_modbus_rtu(
    "/dev/ttyUSB0",
    baudrate=19_200,
    parity="even",
    config=BusConfig(
        request_timeout=3.0,                    # default
        retries=RetryPolicy(retries=1),         # default
        # timing.inter_frame_idle defaults to "auto" — computes 3.5 char-times from baud
    ),
)

# Or wrap an existing stream (e.g., test pair, future TCP):
from anyserial import open_serial_port, SerialConfig, Parity
port = await open_serial_port(
    "/dev/ttyUSB0",
    SerialConfig(baudrate=19_200, parity=Parity.EVEN),
)
bus = Bus(port, config=BusConfig())
```

### 5.2 Per-slave handle

```python
slave = bus.slave(address=1)            # Slave handle is cheap, no I/O.

# Standard FCs:
coils = await slave.read_coils(0, count=8)                      # FC 0x01
disc  = await slave.read_discrete_inputs(0, count=16)           # FC 0x02
regs  = await slave.read_holding_registers(0x0040, count=2)     # FC 0x03
ins   = await slave.read_input_registers(0, count=4)            # FC 0x04
await slave.write_coil(0, on=True)                              # FC 0x05
await slave.write_register(0x0080, value=2500)                  # FC 0x06
await slave.write_coils(0, [True, False, True, True])           # FC 0x0F
await slave.write_registers(0x0040, [0x977D, 0x429C])           # FC 0x10

# v0.2:
await slave.mask_write_register(addr, and_mask=..., or_mask=...) # FC 0x16
await slave.read_write_registers(read_addr, read_count,
                                 write_addr, write_values)       # FC 0x17

# Per-type helpers + dispatcher (in anymodbus.decoders, also exposed on Slave for the common cases):
hv = await slave.read_float(0x0040)                              # high_low default
lv = await slave.read_float(0x0044, word_order="low_high")
await slave.write_float(0x0040, 78.295, word_order="high_low")
```

**Address validation.** `bus.slave(address)` validates per *serial §2.2*:

- `1..247` are unicast addresses. `bus.slave(addr)` returns a normal slave handle.
- `0` is the broadcast address; *not* valid for `bus.slave()`. Calling `bus.slave(0)` raises `ConfigurationError("Use Bus.broadcast_* methods for broadcasts; bus.slave() is unicast only")`. Broadcasts go through dedicated `Bus.broadcast_write_coil(...)`, `Bus.broadcast_write_register(...)`, `Bus.broadcast_write_coils(...)`, `Bus.broadcast_write_registers(...)` methods (see §6.6).
- `248..255` are reserved by the spec; `bus.slave(248)` raises `ConfigurationError("Slave address must be 1-247. Address 0 is broadcast (see Bus.broadcast_*); 248-255 are reserved by the spec.")`.

**Mask Write semantics.** `mask_write_register` applies, on the slave side, the formula from *Modbus Application Protocol v1.1b3 §6.16*:

```
Result = (Current AND and_mask) OR (or_mask AND NOT and_mask)
```

Practically: a 1-bit in `and_mask` preserves the corresponding bit of the current register; a 0-bit clears it. After that masking, a 1-bit in `or_mask` *at a position where `and_mask` is 0* sets that bit. Common patterns:

| Intent | `and_mask` | `or_mask` |
|---|---|---|
| Set bits | `0xFFFF` ANDed with positions you want preserved (i.e. `~bits_to_set`) | `bits_to_set` |
| Clear bits | `~bits_to_clear` | `0x0000` |
| Leave register unchanged | `0xFFFF` | `0x0000` |

### 5.3 Cancellation / timeouts

Standard AnyIO scopes work, with no library-side timer:

```python
with anyio.move_on_after(0.5):
    regs = await slave.read_holding_registers(0, count=2)
```

The `request_timeout` in `BusConfig` is the *default* per-call deadline that wraps each transaction. Outer cancel scopes still preempt.

### 5.4 Sync wrapper (mirrors `anyserial.sync`)

```python
from anymodbus.sync import open_modbus_rtu

with open_modbus_rtu("/dev/ttyUSB0", baudrate=19200, parity="even") as bus:
    slave = bus.slave(1)
    regs = slave.read_holding_registers(0, count=4, timeout=1.0)
```

Reuses anyserial's process-wide `BlockingPortalProvider` — does not spawn a second event-loop thread. Document the integration; export `configure_portal` as a re-export of `anyserial.sync.configure_portal` for users who want to set the AnyIO backend.

## 6. RTU framing & timing — the technical heart

**Source documents.** Function codes, PDU layout, and exception semantics come from *Modbus Application Protocol Specification v1.1b3* (mirrored at [docs/modbusprotocolspecification.md](docs/modbusprotocolspecification.md)). Wire-level framing (3.5/1.5 char-time gaps, CRC byte order, slave address range, broadcast rules, turnaround delay, RTU character format) comes from *Modbus over Serial Line Specification and Implementation Guide v1.02* (mirrored at [docs/modbusoverserial.md](docs/modbusoverserial.md)). Section references in this document are written as `app §X.Y` and `serial §X.Y` respectively to disambiguate.

### 6.1 Tx-side: enforced 3.5-char pre-tx gap

`Bus._txn` records `_last_io_monotonic`. Before each tx:

```python
gap = self.timing.inter_frame_idle  # seconds
elapsed = anyio.current_time() - self._last_io_monotonic
if elapsed < gap:
    await anyio.sleep(gap - elapsed)
```

Per *serial §2.5.1.1*:

- **t3.5 (inter-frame idle)** — at least 3.5 character times of silence between frames. Default `inter_frame_idle = max(3.5 * 11 / baudrate, 0.00175)`. The 1.75 ms floor is the spec-recommended fixed value for baud > 19200, where the per-character interrupt load otherwise becomes prohibitive.
- **t1.5 (inter-character idle)** — at most 1.5 character times of silence within a single frame. Exposed as `inter_char_idle = max(1.5 * 11 / baudrate, 0.00075)` (750 µs spec floor at baud > 19200). Used by the unknown-FC fallback gap-based reader (§6.3) and the unexpected-slave drain logic.

The `11` in both formulas is the on-wire bit count per character: 1 start + 8 data + (1 parity OR 1 extra stop) + 1 stop = 11 bits. This holds for compliant 8E1, 8O1, and 8N2 wire formats. 8N1 (10 bits/char) is non-compliant per spec but exists in the wild — we err on the safe (longer) side by always assuming 11 bits, so the gap is ~10% longer than the wire actually requires under 8N1, which is harmless.

Both timing knobs are configurable as floats or the sentinel `"auto"`. `"auto"` recomputes from the stream's current baud (looked up via the `SerialStreamAttribute.config` typed attribute, with a fallback default if the stream isn't a serial port).

**Broadcast turnaround delay.** After sending a broadcast (slave address 0; see §6.6) the master holds the bus idle for `timing.broadcast_turnaround` seconds before releasing the lock — distinct from `request_timeout` (which only applies to unicast since broadcasts have no response) and from t3.5 (which is wire-level). Default 0.100 s; spec recommends 100–200 ms (*serial §2.4.1*: "Typically the Response time-out is from 1s to several seconds at 9600 bps; and the Turnaround delay is from 100 ms to 200 ms").

### 6.2 Tx: write & flush

1. `await stream.send(adu_bytes)`.
2. If the stream is a `SerialPort` and `BusConfig.drain_after_send=True` (default), `await port.drain()` to ensure the kernel has handed off bytes — important for RS-485 RTS-toggle correctness even when the kernel handles RTS, and nearly free otherwise.

### 6.3 Rx: length-aware read state machine

The framer uses a per-FC response-length table as the single source of truth, kept in `framer.py` and asserted directly in unit tests against the spec:

```python
# Bytes to read after the 2-byte (slave_addr + fc) header, EXCLUDING those 2.
# CRC (2 bytes) is included in each value.
_FIXED_TAIL: Final[Mapping[int, int]] = {
    # fc:  start_addr(2) + value-or-quantity(2) + crc(2)
    0x05: 6,
    0x06: 6,
    0x0F: 6,
    0x10: 6,
    # fc:  ref_addr(2) + and_mask(2) + or_mask(2) + crc(2)
    0x16: 8,
}

# FCs whose response carries a 1-byte byte_count immediately after the FC byte.
_BYTE_COUNT_1B: Final[frozenset[int]] = frozenset({0x01, 0x02, 0x03, 0x04, 0x17})

# FCs known to the spec but not implemented by this version. Recognized so
# the framer can fail with a precise error rather than mis-frame.
_KNOWN_UNSUPPORTED: Final[frozenset[int]] = frozenset({
    0x07,  # Read Exception Status (serial line only)
    0x08,  # Diagnostics
    0x0B,  # Get Comm Event Counter
    0x0C,  # Get Comm Event Log
    0x11,  # Report Server ID
    0x14,  # Read File Record
    0x15,  # Write File Record
    0x18,  # Read FIFO Queue (note: 2-byte byte_count when implemented)
    0x2B,  # Encapsulated Interface Transport (MEI 0x0E planned for v0.2)
})
```

State machine:

```
S0 (await response within deadline):
  while not deadline_expired:
      buf = await read_exact(stream, 2, deadline)   # slave-addr + fc

      if buf[0] != expected_slave_address:
          # serial §2.4.1: a reply addressed to a different slave does NOT
          # abort the transaction. Keep the deadline running and keep
          # listening. Drain the stray frame using a t1.5 idle gap, then loop.
          await read_until_idle(stream, gap=timing.inter_char_idle)
          log.info("Discarded stray frame from slave 0x%02x", buf[0])
          continue
      break
  else:
      raise FrameTimeoutError

  fc = buf[1]
  if fc == 0:
      # app §4.1: function code 0 is not valid.
      raise ProtocolError("Slave returned function code 0 (invalid)")

  if fc & 0x80:
      # Exception response: total ADU = 5 bytes (slave + fc + ec + crc16).
      remaining = await read_exact(stream, 3, deadline)
      verify_crc(buf + remaining)
      base_fc = fc & 0x7F
      if base_fc != expected_fc:
          raise UnexpectedResponseError(
              f"exception echoes fc 0x{base_fc:02x}, expected 0x{expected_fc:02x}"
          )
      raise code_to_exception(remaining[0])(code=remaining[0])

  if fc != expected_fc:
      raise UnexpectedResponseError(
          f"slave returned fc 0x{fc:02x}, expected 0x{expected_fc:02x}"
      )

  if fc in _BYTE_COUNT_1B:
      bc_byte = await read_exact(stream, 1, deadline)
      bc = bc_byte[0]
      # app §4.1: PDU max = 253 bytes (so a 1-byte-bc payload tops out at
      # fc(1) + bc(1) + 250 data + crc(2) = 254-byte PDU on the wire). Reject
      # early to defend against a malformed slave inducing oversized reads.
      if bc > 250:
          raise FrameError(f"byte_count={bc} exceeds spec max of 250")
      data_and_crc = await read_exact(stream, bc + 2, deadline)
      payload = bc_byte + data_and_crc
  elif fc in _FIXED_TAIL:
      payload = await read_exact(stream, _FIXED_TAIL[fc], deadline)
  elif fc in _KNOWN_UNSUPPORTED:
      raise ModbusUnsupportedFunctionError(
          f"FC 0x{fc:02x} is defined by the Modbus spec but not implemented "
          f"by this version of anymodbus"
      )
  else:
      # Truly unknown FC (user-defined ranges 65-72, 100-110, or vendor
      # private). Fall back to gap-based read; imprecise but the only
      # option without per-FC length knowledge.
      payload = await read_until_idle(stream, gap=timing.inter_char_idle)

  verify_crc(buf + payload)
  return decode_pdu(fc, payload)
```

**Notable framer behaviors:**

- **FC 0x16 (Mask Write Register)** has its own length entry of 8 bytes, *not* 6. The response is `addr(2) + AND(2) + OR(2) + crc(2)` = 8 bytes after the 2-byte header (*app §6.16*). Grouping it with FC 0x05/0x06/0x0F/0x10 (which are 6 bytes) would mis-frame the next response on the bus.
- **Unexpected slave addresses do not abort.** Per *serial §2.4.1*, a reply addressed to a different slave keeps the response timeout running and the master continues to wait. This implementation drains the stray frame using a t1.5 idle gap and loops within the same deadline.
- **PDU max sanity check.** A malformed slave that returns `byte_count = 0xFF` would otherwise induce a ~257-byte read — the explicit `bc > 250` guard surfaces that as `FrameError` immediately and avoids large speculative allocations.
- **FC 0 is rejected** with `ProtocolError`, since *app §4.1* declares "Function code 0 is not valid".
- **Known-but-unsupported FCs** (Diagnostics family, FIFO, file record, MEI transport) raise `ModbusUnsupportedFunctionError` instead of falling into the generic gap-based fallback. This stops a slave that supports more than we do from corrupting the stream just because we didn't know how to length-bound its response.
- **Exception responses are CRC-verified before raising.** A bad CRC on an exception ADU surfaces as `CRCError` (retryable per RetryPolicy), not as the slave-reported exception code (which we'd then have no business trusting).

**Why length-aware reads:** they survive Linux/macOS scheduling jitter where response bytes arrive in 2–3 ms chunks. The 1.5-char gap fallback fires only for genuinely unknown FCs; standard FCs never exercise it. (`pymodbus`'s rx framer uses the same length-aware approach via per-PDU `calculateRtuFrameSize`.)

`read_exact` is implemented over `stream.receive` with a buffered approach (bytearray accumulator, `receive_into` if the stream is an `anyserial.SerialPort` for zero-allocation reads).

### 6.4 CRC failure / resync

On CRC mismatch:

1. `await stream.reset_input_buffer()` if the stream supports it (anyserial does).
2. `await anyio.sleep(timing.inter_frame_idle)` — let the bus go idle for 3.5 char-times.
3. Raise `ModbusCRCError`. The retry loop in `Bus._txn` decides whether to redrive the request.

### 6.5 Retry policy

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    retries: int = 1
    retry_on: frozenset[type[ModbusError]] = frozenset({CRCError, FrameTimeoutError})
    retry_idempotent_only: bool = True   # see is_idempotent_function
    backoff_base: float = 0.0            # extra wait beyond inter_frame_idle
```

`retry_idempotent_only=True` blocks silent re-writes — the failure mode being avoided is "the slave received and acted on the write but the response was lost in transit, so we retry and double-fire". Only function codes for which `is_idempotent_function(fc) is True` are retried under that policy — currently FC 1-4. FC 23 (Read/Write Multiple) is **not** automatically retried because of its write half; FC 22 (Mask Write) depends on the slave's current value and is also not auto-retried. Writes only retry when the caller explicitly opts in (`retry_idempotent_only=False`). Modbus exception responses (`IllegalFunctionError` etc.) are never retried — the slave told us no, retrying won't change that — so they are intentionally absent from the default `retry_on` set. `pymodbus` exposes a single `retries: int` applied to all FCs and does not make this distinction.

### 6.6 Broadcast handling

Per *serial §2.1*: "The broadcast requests are necessarily writing commands. All devices must accept the broadcast for writing function." Per *serial §2.2*, slave address 0 is reserved as the broadcast address.

Broadcasts have a different transaction shape from unicast — no reply, plus a turnaround delay — so they get their own typed API on `Bus` rather than overloading `Slave`:

```python
class Bus:
    async def broadcast_write_coil(self, address: int, *, on: bool) -> None: ...        # FC 0x05
    async def broadcast_write_register(self, address: int, value: int) -> None: ...     # FC 0x06
    async def broadcast_write_coils(self, address: int, values: Sequence[bool]) -> None: ...    # FC 0x0F
    async def broadcast_write_registers(self, address: int, values: Sequence[int]) -> None: ... # FC 0x10
```

The `Bus.broadcast_*` methods are the *only* way to broadcast — there is no `bus.slave(0)` (it raises `ConfigurationError`; see §5.2). This deliberate asymmetry means callers can't accidentally broadcast a read FC, since the read FCs simply don't exist as `broadcast_*` variants.

Internally, each broadcast call routes through `Bus._broadcast_txn(adu_bytes)`:

1. Acquire the bus lock.
2. Hold the t3.5 inter-frame gap (§6.1) just like unicast.
3. `await stream.send(adu_bytes)` then `drain()`.
4. Skip the rx state machine entirely — broadcasts have no response.
5. Hold the bus lock for an additional `timing.broadcast_turnaround` seconds (default 0.100 s; *serial §2.4.1* recommends 100–200 ms) so every slave has time to process the write before the next transaction hits the bus.
6. Release the lock. Return `None`.

The 3.5-char tx gap alone is **not** spec-compliant for broadcasts — the turnaround delay is the explicit "let slaves finish processing" window. Without it, a fast follow-up unicast can arrive while a slave is still busy applying the broadcast write.

A deferred case: when FC 0x08 (Diagnostics) lands, sub-function 0x01 (Restart Communications Option) is broadcast-eligible per *app §6.8.1*. It will surface as `Bus.broadcast_restart_communications(...)`, routed through the same `_broadcast_txn` machinery.

## 7. Error model

Mirrors anyserial's multi-inheritance idiom. All inherit `ModbusError` plus a stdlib/AnyIO base.

| Class | Bases | Trigger |
|---|---|---|
| `ModbusError` | `Exception` | Base |
| `ConfigurationError` | `ModbusError, ValueError` | Bad value passed to a config dataclass or constructor |
| `ProtocolError` | `ModbusError, ValueError` | Codec/framer rejected something well-formed |
| `CRCError` | `ProtocolError` | CRC mismatch |
| `FrameError` | `ProtocolError` | Truncated, junk between frames |
| `FrameTimeoutError` | `ModbusError, TimeoutError` | No response within deadline |
| `ConnectionLostError` | `ModbusError, anyio.BrokenResourceError` | Stream disconnected mid-txn |
| `BusClosedError` | `ModbusError, anyio.ClosedResourceError` | Bus closed |
| `UnexpectedResponseError` | `ProtocolError` | Slave addr or FC echoed doesn't match request |
| `ModbusUnsupportedFunctionError` | `ModbusError, NotImplementedError` | Caller asked for or framer received a known-but-not-implemented FC (see §6.3) |

`ConfigurationError` is raised eagerly during `BusConfig`/`RetryPolicy`/`TimingConfig`/`Slave` construction; it never surfaces from a live transaction. Wire-level violations are `ProtocolError`. The two are deliberately distinct so callers can pre-validate their config without writing `try`/`except` around real I/O.

Modbus *exception responses* (function code with high bit set, body = an exception code byte) map to dedicated subclasses, all inheriting `ModbusError` (not `ProtocolError` — they're a slave-side semantic outcome, not a wire error). The codes covered by *app §7* are:

| Code | Class |
|---|---|
| 0x01 | `IllegalFunctionError` |
| 0x02 | `IllegalDataAddressError` |
| 0x03 | `IllegalDataValueError` |
| 0x04 | `SlaveDeviceFailureError` |
| 0x05 | `AcknowledgeError` |
| 0x06 | `SlaveDeviceBusyError` |
| 0x08 | `MemoryParityError` |
| 0x0A | `GatewayPathUnavailableError` |
| 0x0B | `GatewayTargetFailedToRespondError` |

Any code outside this set (notably 0x07, 0x09, and 0x0C–0xFF, which are unassigned in v1.1b3) raises `ModbusUnknownExceptionError`, which inherits from `ModbusExceptionResponse` so callers wanting "any slave-returned exception" can catch the base class. The raw byte is exposed on `exception_code` (inherited from the base):

```python
class ModbusUnknownExceptionError(ModbusExceptionResponse):
    """Slave returned an exception code not defined by app §7."""
    # inherits exception_code: int from ModbusExceptionResponse
```

This is intentionally not a `ProtocolError` — the slave returned a well-formed exception ADU; we just don't have a named class for the code it chose. Callers who need to handle a specific legacy code (e.g. NAK on Modicon devices) can match on `err.code == 0x07`.

**Legacy 0x07 (Negative Acknowledge).** Pre-v1.1 Modicon controllers defined exception code 0x07 as "Negative Acknowledge". v1.1b3 §7 does not list 0x07; the NAK semantic was repositioned as a Diagnostics counter (FC 0x08 sub 0x10, *app §6.8.1*). We deliberately do **not** ship a `NegativeAcknowledgeError` class — anything a legacy device emits as 0x07 surfaces as `ModbusUnknownExceptionError(code=0x07)`. Downstream device libraries that target old Modicon hardware can subclass and re-raise as needed.

`code_to_exception(code: int) -> type[ModbusError]` translator in `exceptions.py`, parallel to `anyserial.errno_to_exception`. Returns `ModbusUnknownExceptionError` for any unmapped code; never raises `KeyError`.

## 8. Capability model

```python
class ModbusCapability(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"

@dataclass(frozen=True, slots=True, kw_only=True)
class SlaveCapabilities:
    function_codes: Mapping[FunctionCode, ModbusCapability]   # Probed lazily.
    max_coils_per_read: int | None = None                     # Spec: 2000
    max_registers_per_read: int | None = None                 # Spec: 125
```

`Slave.probe()` runs FC 0x03 / 0x04 with `count=1` against a few well-known offsets and maps the outcome:

- success → `SUPPORTED`
- `IllegalFunctionError` (exception code 0x01) → `UNSUPPORTED` (App Protocol §7 mandates this code for unimplemented FCs)
- `FrameTimeoutError` / `ConnectionLostError` → `UNKNOWN` (a silent slave is indistinguishable from a slave that drops unknown FCs without responding; do not downgrade silence to `UNSUPPORTED`)
- `IllegalDataAddressError` → `SUPPORTED` for the FC, but the probed address isn't valid; move the probe.

Cached on the slave handle. Optional — many users won't need it; downstream device libraries can skip probing because they already know what their device supports.

## 9. RS-485 strategy

Pass through to `anyserial`. `BusConfig` does not duplicate `RS485Config` knobs; users open the serial port with the RS-485 settings they want, then hand it to `Bus`. Document the canonical pattern:

```python
from anyserial import open_serial_port, SerialConfig, RS485Config, Parity

port = await open_serial_port(
    "/dev/ttyUSB0",
    SerialConfig(
        baudrate=19_200,
        parity=Parity.EVEN,
        rs485=RS485Config(enabled=True, rts_on_send=True, rts_after_send=False),
    ),
)
bus = Bus(port)
```

Document a troubleshooting note for USB-RS485 adapters where the kernel can't auto-toggle RTS — point to anyserial's `drain_exact` + manual RTS pattern.

## 10. PDU codec — design notes

Each FC gets two pure functions:

```python
def encode_read_holding_request(address: int, count: int) -> bytes: ...
def decode_read_holding_response(payload: bytes) -> tuple[int, ...]: ...
```

- **Bounds-checked.** Addresses are always in `[0, 0xFFFF]` (*app §4.4*: "In a MODBUS PDU each data is addressed from 0 to 65535"). Per-FC quantity limits come straight from the spec:

  | FC | Quantity range | Spec |
  |----|---------------|------|
  | 0x01 Read Coils | 1 – 2000 (0x7D0) | app §6.1 |
  | 0x02 Read Discrete Inputs | 1 – 2000 (0x7D0) | app §6.2 |
  | 0x03 Read Holding Registers | 1 – 125 (0x7D) | app §6.3 |
  | 0x04 Read Input Registers | 1 – 125 (0x7D) | app §6.4 |
  | 0x0F Write Multiple Coils | 1 – 1968 (0x07B0) | app §6.11 |
  | 0x10 Write Multiple Registers | 1 – 123 (0x7B) | app §6.12 |
  | 0x17 Read/Write Multiple Registers | read 1 – 125, write 1 – 121 (0x79) | app §6.17 |

  Violations raise `ValueError`. Each `encode_*` enforces its own bounds; `decode_*` does not re-check (the framer has already accepted the byte_count).

- **FC 0x05 Write Single Coil — wire value.** Per *app §6.5*, the value field on the wire must be exactly `0xFF00` (ON) or `0x0000` (OFF); any other value is a protocol violation. The high-level API takes `on: bool` and the encoder produces the correct word. The decoder of the echo response asserts the value is one of these two — otherwise `ProtocolError`.

- **Function code 0 is invalid.** *app §4.1*: "Function code '0' is not valid." The `FunctionCode` enum does not include 0, so encoding is naturally protected; the framer rejects FC 0 in incoming responses with `ProtocolError` (see §6.3).

- **Strict types:** registers are `tuple[int, ...]` (immutable), coils are `tuple[bool, ...]`, write echoes return `int` start-address + `int` count.

- **Property-tested with hypothesis:** roundtrip `decode(encode(x)) == x` for all valid inputs; `encode` raises on invalid; `decode` rejects truncated/oversized payloads.

CRC is its own module (`crc.py`) — single function, 256-entry precomputed table, hot-path-friendly. `crc16_modbus(data: Buffer) -> int`.

**CRC byte order on the wire.** Per *serial §2.5.1.2*: "low-order byte of the field is appended first, followed by the high-order byte." This is **opposite** to the big-endian convention *app §4.2* uses for data fields — easy to mis-implement. The framer's `verify_crc` and the writer's CRC append must both honor this little-endian-on-wire order. Tested against the worked example from *serial §6.2.2 Appendix B*.

## 11. Decoders / encoders for multi-register types

Living in `anymodbus.decoders` (and surfaced as methods on `Slave`):

```python
class WordOrder(StrEnum):
    HIGH_LOW = "high_low"      # MSW first — equivalent to struct.pack(">f", ...)
    LOW_HIGH = "low_high"      # LSW first — common on certain controllers

class ByteOrder(StrEnum):
    BIG = "big"                # within each 16-bit word — Modbus spec (App Protocol §4.2)
    LITTLE = "little"          # rare, seen on some devices

class RegisterType(StrEnum):
    INT16 = "int16"; UINT16 = "uint16"
    INT32 = "int32"; UINT32 = "uint32"
    INT64 = "int64"; UINT64 = "uint64"
    FLOAT32 = "float32"; FLOAT64 = "float64"
    STRING = "string"

# Per-type helpers (also exposed on Slave for the common-case helpers):
def decode_float32(words, *, word_order=HIGH_LOW, byte_order=BIG) -> float: ...
def encode_float32(value, *, word_order=..., byte_order=...) -> tuple[int, int]: ...
def decode_float64(...): ...; def encode_float64(...): ...
def decode_int16(...): ...; def encode_int16(...): ...
def decode_int32(...): ...; def encode_int32(...): ...
def decode_int64(...): ...; def encode_int64(...): ...
def decode_string(words, *, byte_order=BIG, encoding="ascii", strip_null=True) -> str: ...
def encode_string(value, *, register_count=None, byte_count=None, byte_order=BIG, ...): ...

# Type-dispatched single entry point — the recommended API for downstream
# device libraries that drive a register schema from configuration:
def decode(words, *, type: RegisterType, word_order=..., byte_order=..., ...) -> int | float | str: ...
def encode(value, *, type: RegisterType, register_count=None, byte_count=None, ...) -> tuple[int, ...]: ...
```

All four (word_order × byte_order) combinations are covered. Defaults are high-word-first, big-endian within word — equivalent to ``struct.pack(">f", ...)``. The Modbus Application Protocol spec (§4.2) defines big-endian byte ordering *within* a single 16-bit register but does **not** standardize multi-register word ordering: that's vendor-defined. ``HIGH_LOW`` is simply the most common convention. minimalmodbus uses 0..3 magic numbers; we use named enums for clarity at call sites. Downstream device libraries layer their vendor-specific defaults on top by passing the order explicitly.

## 12. Testing strategy

### 12.1 Test categories

- **Unit (fast, in-process):** PDU codec roundtrips (hypothesis), CRC vector tests against well-known fixtures, framer state-machine tests with a `MemoryByteStream` fake.
- **Integration:** `client_slave_pair()` — wires `Bus` to a `MockSlave` over `anyserial.testing.serial_port_pair()`. Runs full RTU handshakes including timing.
- **Fault-injection:** `FaultPlan` from anyserial — re-exported and extended with Modbus-aware faults (drop random bytes, flip a CRC bit, hold response for N seconds, return wrong slave address).
- **Hardware (opt-in):** `pytest -m hardware` with `ANYMODBUS_TEST_PORT` + `ANYMODBUS_TEST_SLAVE_ADDRESS` env vars, plus a tiny YAML manifest of expected register values for the device under test.
- **Concurrency stress:** spawn N tasks all calling `slave.read_holding_registers` on the same bus, assert all responses correct, no frame fusion. Validates that the bus lock is doing its job under contention.

**Spec-derived correctness tests (must-have for v0.1):**

- `test_framer_response_length_table` — assert each entry in `_FIXED_TAIL` and `_BYTE_COUNT_1B` against a spec-derived fixture (one row per FC), so a future code change that mutates the table fails loudly.
- `test_crc_byte_order_low_then_high` — feed the worked example from *serial §6.2.2 Appendix B* through `crc16_modbus`, assert both the integer value and that the on-wire encoding emits low byte before high byte.
- `test_bus_slave_zero_rejected` — `bus.slave(0)` raises `ConfigurationError`; the only broadcast surface is `Bus.broadcast_*`.
- `test_broadcast_methods_only_writes` — assert by introspection that `Bus.broadcast_*` exposes only write methods; no read variants exist.
- `test_broadcast_holds_turnaround_delay` — measure that the bus lock is held for ~`broadcast_turnaround` seconds after a broadcast write, so the next unicast request can't preempt slave processing.
- `test_broadcast_no_rx_attempted` — with a `MockSlave` that records whether anything was read after the request, assert nothing is read after a broadcast.
- `test_unexpected_slave_keeps_waiting` — `MockSlave` injects a stray frame addressed to a different slave, then the right one within the same response timeout; assert the right reply is returned and the deadline was not reset.
- `test_unknown_exception_code_returns_unknown_class` — feed exception codes 0x07, 0x09, 0xFF and assert `ModbusUnknownExceptionError` with the raw code preserved.
- `test_known_unsupported_fc_raises_precise_error` — feed a forged response with FC 0x07/0x08/0x18/etc. and assert `ModbusUnsupportedFunctionError`, *not* `FrameError` or stream corruption.
- `test_per_fc_quantity_bounds` — hypothesis-driven, one parametrize per FC, asserting both min-1 and max+1 raise.
- `test_fc_05_wire_value_enforcement` — encoder produces 0xFF00/0x0000 only; decoder rejects any other value with `ProtocolError`.
- `test_pdu_max_byte_count_sanity` — fuzz the rx framer with `byte_count` in `(251..255)` and assert `FrameError` with no large allocation.
- `test_fc_zero_rejected` — forged response with FC byte == 0 raises `ProtocolError`.
- `test_slave_address_validation` — `bus.slave(0)`, `bus.slave(248)`, `bus.slave(-1)`, `bus.slave(256)` all raise `ConfigurationError`; `bus.slave(1)` and `bus.slave(247)` succeed.

### 12.2 `MockSlave` design

```python
class MockSlave:
    address: int
    coils: bytearray
    discrete_inputs: bytearray
    holding_registers: list[int]
    input_registers: list[int]

    async def serve(self, stream: anyio.abc.ByteStream) -> None:
        """Read requests, write responses, until cancelled."""

def client_slave_pair(*, slave_address: int = 1) -> tuple[Bus, MockSlave]:
    """Returns (bus, slave) pair backed by anyserial's serial_port_pair."""
```

Expose enough surface that downstream device libraries can spin up a `MockSlave` preloaded with their device's register map and write protocol-level integration tests without hardware.

## 13. Performance strategy

Not the primary lever — Modbus RTU is 19200 baud most of the time, hardware-limited. But measure:

- **Round-trip latency** vs pymodbus and minimalmodbus on a loopback PTY — target ≤ pymodbus on small reads (single register).
- **Concurrent fan-out** across multiple buses (different ports) — target near-linear scaling, the same demonstration anyserial leans on.
- **Single-bus serialization throughput** — target maximum FC 3 reads per second on a given baud, characterize headroom over the on-wire ceiling.

Benchmarks live in `benchmarks/` under `pytest-benchmark`. CI doesn't run them, but `make bench` does.

## 14. Documentation plan

Built with **Zensical** (same as anyserial). File list, mirroring anyserial's structure:

```
docs/
├── index.md
├── quickstart.md              # 30-line example
├── configuration.md           # BusConfig, RetryPolicy, TimingConfig
├── rtu.md                     # 8E1 default, 3.5-char gap, frame structure
├── framing.md                 # Length-aware reader rationale
├── timing.md                  # Per-baud table, RS-485 considerations
├── exceptions.md              # Full hierarchy + Modbus exception code table
├── cancellation.md            # AnyIO scopes, request_timeout precedence
├── decoders.md                # Float/int32/string + word-order matrix
├── capabilities.md            # Probe pattern, SlaveCapabilities
├── sync.md                    # Sync wrapper notes
├── testing.md                 # MockSlave, client_slave_pair, FaultPlan
├── performance.md             # Numbers + how we measure
├── troubleshooting.md         # "no response" → checklist
├── migration-from-pymodbus.md # Address all the pymodbus pain points by example
├── migration-from-minimalmodbus.md
├── design.md                  # Stub include of root DESIGN.md
└── changelog.md               # Stub include of root CHANGELOG.md
```

`migration-from-pymodbus.md` is high-leverage — it walks through the live tradeoffs (AnyIO vs asyncio, tx-side 3.5-char timing, idempotent-only retry default, connection-on-sustained-timeout posture, strict typing, required-parity keyword, RTU-client-only scope) with side-by-side code, framed honestly as design choices rather than as bugs in pymodbus. That posture — match `pymodbus`'s §20 audit, don't oversell — is how we earn migrations from a mature incumbent.

## 15. Tooling & packaging

Mirror `anyserial/pyproject.toml`. Key bits:

```toml
[project]
name = "anymodbus"
dynamic = ["version"]
description = "Async-native Modbus RTU client for Python, built on AnyIO and anyserial."
readme = "README.md"
requires-python = ">=3.13"
license = "MIT"
license-files = ["LICENSE"]
keywords = ["modbus", "modbus-rtu", "rs485", "industrial", "scada", "async", "anyio", "serial"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Framework :: AnyIO",
    "Intended Audience :: Developers",
    "Intended Audience :: Manufacturing",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    "Programming Language :: Python :: Implementation :: CPython",
    "Topic :: System :: Hardware",
    "Topic :: Communications",
    "Typing :: Typed",
]
dependencies = [
    "anyio>=4.13",
    "anyserial>=0.1.1",
]

[project.optional-dependencies]
trio = ["trio>=0.27"]

[dependency-groups]
dev    = [{include-group="lint"}, {include-group="type"}, {include-group="test"}, {include-group="docs"}, "pre-commit>=4.0"]
lint   = ["ruff>=0.8"]
type   = ["mypy>=1.13", "pyright>=1.1.390"]
test   = ["pytest>=8.3", "pytest-cov>=6.0", "coverage[toml]>=7.13.5", "hypothesis>=6.120", "trio>=0.27"]
bench  = ["pytest-benchmark>=4.0"]
docs   = ["zensical>=0.0.33"]
```

- Same Ruff rule set as anyserial. Add `T20` (no print) for examples discipline.
- mypy strict + pyright strict (dual checker).
- `pytest filterwarnings = ["error", "ignore::DeprecationWarning:anyio.*"]`.
- Markers: `hardware`, `slow`, `concurrency`. Hardware deselected by default.
- hatch-vcs for version → `src/anymodbus/_version.py`.
- `Makefile` mirroring anyserial's targets (help/install/sync/lint/format/typecheck/test/test-all/cov/bench/docs/clean + `act-*`).

## 16. CI/CD plan

`.github/workflows/`:

- **`ci.yml`** — lint, typecheck (mypy + pyright), unit tests on `{ubuntu, macos, windows} × {3.13, 3.14}`, integration tests on Linux (uses anyserial's serial_port_pair which works under Linux PTYs), free-threaded 3.13t allowed-failure, build (`uv build` + `twine check`).
- **`bench.yml`** — manual dispatch only.
- **`docs.yml`** — Zensical build + GH Pages deploy.
- **`publish.yml`** — PyPI publish on tag, trusted publisher (OIDC).

Top-level `permissions: contents: read`. Concurrency-cancel-in-progress per ref. `UV_FROZEN: "1"`. `astral-sh/setup-uv@v8`. Codecov upload per job, flagged.

## 17. Repository skeleton

```
anymodbus/
├── .github/workflows/{ci,bench,docs,publish}.yml
├── src/anymodbus/...
├── tests/
│   ├── unit/
│   │   ├── test_crc.py
│   │   ├── test_pdu_roundtrip.py        # hypothesis
│   │   ├── test_framer.py
│   │   └── test_decoders.py             # word-order matrix
│   ├── integration/
│   │   ├── test_client_slave_pair.py
│   │   ├── test_concurrent_safety.py    # N tasks share one bus; assert no frame fusion
│   │   ├── test_retry_policy.py
│   │   └── test_fault_injection.py
│   └── hardware/
│       └── test_real_device.py          # opt-in
├── benchmarks/
│   ├── bench_roundtrip.py
│   └── bench_concurrent.py
├── docs/...
├── examples/
│   ├── 01_basic_read.py
│   ├── 02_concurrent_polling.py
│   ├── 03_float_setpoint.py             # 32-bit float read/write across two registers
│   └── 04_with_anyserial_rs485.py
├── DESIGN.md                            # This document
├── CHANGELOG.md
├── README.md
├── LICENSE
├── Makefile
├── pyproject.toml
└── zensical.toml
```

## 18. Roadmap

### v0.1 — RTU client MVP (target: 2 weeks of focused work)

- Codecs for FC 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x0F, 0x10.
- Bus + Slave + Framer + CRC + length-aware reader.
- Exception hierarchy + code translator (`ConfigurationError` separated from `ProtocolError`).
- RetryPolicy with idempotent-only default and `retry_on` filter.
- Decoders: int16/int32/int64 (signed and unsigned), float32/float64, and string, with `WordOrder` / `ByteOrder` enums (most-common-convention defaults). A type-dispatched `decode(words, type=RegisterType.X, ...)` / `encode(value, type=RegisterType.X, ...)` pair is the recommended entry point for downstream device libraries that drive a register schema from configuration.
- Sync wrapper.
- Broadcast helpers (`Bus.broadcast_*`) honouring `timing.broadcast_turnaround`.
- `MockSlave` + `client_slave_pair` + concurrency tests.
- Migration-from-pymodbus doc with side-by-side examples.
- Full mypy strict + pyright strict + ruff clean + 90%+ coverage.
- Published to PyPI.

**v0.1 connection contract:** if the underlying stream raises
`anyio.BrokenResourceError`, the `Bus` surfaces `ConnectionLostError` and
the bus is dead — open a new one. There is no auto-reconnect in v0.1
(unlike pymodbus's `retries+3`-then-reconnect behaviour). A thin
`ResilientBus` wrapper is on the v0.2/v0.3 roadmap.

### v0.2 — Hardening & extras

- FC 0x16 (Mask Write), 0x17 (Read/Write Multiple).
- `Slave.probe()` capability detection.
- FC 0x2B / MEI 0x0E (Read Device Identification). Per *app §6.21*, multi-object responses use a `More Follows` / `NextObjectId` continuation loop — a single user call may issue 1..N transactions on the bus. Wrapper API:
    ```python
    await slave.read_device_identification(
        category: Literal["basic", "regular", "extended"] = "basic",
    ) -> dict[int, bytes]   # ObjectId -> raw object value
    await slave.read_device_identification_object(object_id: int) -> bytes  # individual access (ReadDevId code 0x04)
    ```
    The streaming form loops until the response's `More Follows` byte is 0x00, advancing `Object Id` to the server's `Next Object Id` each iteration. Each segment is independently retried under the active `RetryPolicy`; on partial-segment failure the partial dict is attached to the raised `ModbusError` for diagnostics. Individual-access form is a single transaction.
- Benchmarks vs pymodbus / minimalmodbus published in `docs/performance.md`.
- More fault-injection scenarios.

### v0.3 — Modbus TCP

- `anymodbus.tcp` adds `open_modbus_tcp(host, port)` returning a `Bus` over an `anyio.connect_tcp` stream.
- MBAP framer (different framing — adds transaction ID, no CRC). Cleanly slot in as an alternative `Framer` strategy without disturbing the `Bus` API.

### v0.4 — Modbus ASCII (optional, only if a user asks)

### v1.0 — API freeze

Lock the public surface. Anything user-visible after this needs a deprecation cycle with `warnings.deprecated`.

## 19. Open questions to flag

1. **Default parity / baudrate.** **Resolved:** `open_modbus_rtu` requires both as keyword arguments with **no default**. Real-device defaults vary too widely (8N1 vs 8E1, 9600 vs 19200 vs 38400 ...) for any portable default to be safe. Force the user to make a deliberate choice. Note that *serial §2.5.1* makes 8E1 the spec default — when a downstream library has no better information, that's the recommendation to surface in docs.
2. **Broadcast (slave_address=0).** **Resolved:** specified in §6.6. Broadcasts go through `Bus.broadcast_*` methods. `bus.slave(0)` raises `ConfigurationError`. The 3.5-char tx gap is held before the broadcast, and `timing.broadcast_turnaround` (default 100 ms per *serial §2.4.1*) is held after, before the bus lock is released. Only write FCs (5, 6, 15, 16) are valid; reads, mask write, and read/write multiple raise `ValueError` synchronously.
3. **Should `Bus` accept any `anyio.abc.ByteStreamConnectable` and own connect/reconnect?** **Open.** Lean: **no for v0.1**. Caller hands in an already-connected stream. Reconnection is a v0.2+ topic and likely belongs in a thin `ResilientBus` wrapper.
4. **Inter-character timeout (1.5 char-times).** **Resolved:** specified in §6.1 as `TimingConfig.inter_char_idle`, used by both the unknown-FC gap-based reader (§6.3) and the unexpected-slave drain logic (§6.3). Default `max(1.5 * 11 / baudrate, 0.00075)` per *serial §2.5.1.1*.
5. **Logging.** **Resolved:** use `logging` with a single named logger `anymodbus.bus`, log frame hex at DEBUG, exception responses and discarded stray frames at INFO, CRC/timeout at WARNING. Ruff's `LOG`/`G` rules already enforce extra-arg style.

## 20. Honest differentiation from the existing ecosystem

**`anymodbus` is not a drop-in replacement for `pymodbus`.** pymodbus is more mature, broader in scope (TCP/ASCII/server/simulator), and works fine for most users. The differences below are the cases where `anymodbus`'s tradeoffs fit better.

### Verified against pymodbus 3.13.0 (latest stable) and 4.0.0dev10

| Concern | pymodbus current | **anymodbus** |
|---|---|---|
| AnyIO-native (asyncio + uvloop + trio) | No — asyncio only (`import asyncio` throughout transaction/transport) | **Yes** |
| Concurrency lock on bus transactions | `asyncio.Lock` (in `TransactionManager.execute`) | `anyio.Lock` |
| Length-aware rx framer | Yes — `pdu_class.calculateRtuFrameSize()` | Yes |
| Tx-side enforced 3.5-char inter-frame gap | **Not enforced** (lock alone, no pre-tx sleep) | **Enforced** |
| Idempotent-only retry default | No — single `retries: int` for all FCs | **Yes** (`retry_idempotent_only=True` by default) |
| Connection on sustained timeout | Closes after `retries+3` (auto-reconnects if configured) | Raises only; no connection effect |
| Required parity keyword | No — defaults to `"N"` | **Yes** (no default; opinionated correctness) |
| `mypy strict = true` | No (partial-strict flags) | **Yes** |
| `pyright typeCheckingMode` | `standard` | **`strict`** |
| Scope | Client + server, RTU + TCP + ASCII + UDP + TLS + simulator | RTU client only |

### Things pymodbus is better at today

- Battle-tested across many years and many devices. `anymodbus` is pre-alpha.
- Auto-reconnect behavior built in. We require the caller to manage the connection.
- Broader scope — if you need TCP, ASCII, or a server, pymodbus is the answer.
- Mature error recovery for flaky industrial hardware.

### versus minimalmodbus

`minimalmodbus` is sync-only, pyserial-coupled, and a single 3000-line file. Its strengths are simplicity and an ergonomic per-instrument API. We borrow the per-slave-handle ergonomic. If you're writing a sync bench script and never need async, `minimalmodbus` is a perfectly good choice.

### versus umodbus / async-modbus

`umodbus` (AdvancedClimateSystems) is a low-level codec-only library; `async-modbus` is a thin async wrapper that pairs umodbus with `serialio`/`sockio`. Both have small ecosystems. We share their "PDU codec is pure functions, transport is pluggable" architecture but ship a complete client.

---

## Sources informing this plan

- The Modbus Application Protocol spec v1.1b3 and the Modbus over Serial Line spec v1.02 — the canonical references for framing, timing, function codes, and exception codes.
- Direct reading of `pymodbus` 3.13.0 (latest stable) and 4.0.0dev10 source — `pymodbus/transaction/transaction.py`, `pymodbus/framer/rtu.py`, `pymodbus/client/serial.py` — to ground the comparison in §20 against current behavior rather than historical bug reports.
- [`minimalmodbus`](https://minimalmodbus.readthedocs.io/) for the per-instrument ergonomic, and [`uModbus`](https://github.com/AdvancedClimateSystems/uModbus) for the "pure-codec ⊕ pluggable transport" architecture.
- [Linux RS-485 kernel docs](https://docs.kernel.org/driver-api/serial/serial-rs485.html) for the kernel-vs-userspace direction-control story.
- `anyserial`'s own [DESIGN.md](https://github.com/GraysonBellamy/anyserial/blob/main/DESIGN.md) and pyproject.toml as the conventions reference.
