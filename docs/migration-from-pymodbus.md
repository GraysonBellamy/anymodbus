# Migration from pymodbus

`pymodbus` is the dominant Python Modbus library. It's mature, broad in scope (RTU, TCP, ASCII, server-side, simulator), and well-supported. **For most projects it's the right choice.** This page is for the narrower case where `anymodbus`'s tradeoffs fit better:

- You only need an RTU **client**, not a server.
- You're already in (or moving to) an AnyIO codebase, or you want trio support.
- You want stricter typing guarantees in your dependency graph.
- You want idempotent-only retries by default so transient timeouts can never silently double-fire writes.

If none of those apply, stay on pymodbus — the migration cost won't pay off.

## API translation

The high-level API surfaces are similar. The main differences are vocabulary (`device_id=` vs per-slave handle) and a few keyword changes.

### Open a client / bus

```python
# pymodbus
from pymodbus.client import AsyncModbusSerialClient
client = AsyncModbusSerialClient("/dev/ttyUSB0", baudrate=19200, parity="N")
await client.connect()

# anymodbus
from anymodbus import open_modbus_rtu
bus = await open_modbus_rtu("/dev/ttyUSB0", baudrate=19200, parity="none")
# baudrate and parity are required keywords — no defaults.
```

### Read holding registers

```python
# pymodbus 4.x — uses device_id keyword on every call
result = await client.read_holding_registers(address=0, count=4, device_id=1)
if result.isError():
    raise RuntimeError(result)
regs = result.registers

# anymodbus — per-slave handle, raises specific exceptions on error
regs = await bus.slave(1).read_holding_registers(0, count=4)
```

### Decode a 32-bit float across two registers

```python
# pymodbus 4.x — convert_from_registers classmethod
from pymodbus.client.mixin import ModbusClientMixin
result = await client.read_holding_registers(0x40, count=2, device_id=1)
value = ModbusClientMixin.convert_from_registers(
    result.registers,
    data_type=ModbusClientMixin.DATATYPE.FLOAT32,
    word_order="big",
)

# anymodbus — high_low/low_high enums, default is struct.pack(">f", ...) layout
value = await bus.slave(1).read_float(0x40)

# Or for a device that stores the low word first:
from anymodbus import WordOrder
value = await bus.slave(1).read_float(0x40, word_order=WordOrder.LOW_HIGH)
```

### Concurrent reads on one bus

Both libraries serialize concurrent transactions through an internal lock — `asyncio.Lock` in pymodbus, `anyio.Lock` here. Concurrent tasks sharing a single client/bus are safe in both:

```python
# Both libraries: safe
async with anyio.create_task_group() as tg:
    for slave_id in [1, 2, 3]:
        tg.start_soon(bus.slave(slave_id).read_holding_registers, 0, count=4)
```

## Genuine differences worth knowing about

### 1. AnyIO vs asyncio

pymodbus imports `asyncio` directly throughout the transaction and transport layers. There is no path to use it under trio. `anymodbus` runs on AnyIO, so the same code works under asyncio (with or without uvloop) and trio.

### 2. Tx-side inter-frame timing

The Modbus RTU spec requires at least 3.5 character-times of bus idle between consecutive frames. `pymodbus` does not enforce this on the tx side — it relies on its concurrency lock plus OS scheduling. In practice that's fine for most setups, but spec-strict slaves on a fast host can occasionally see back-to-back transactions land too close together.

`anymodbus` records the time of last bus activity inside the bus and sleeps before each tx until at least 3.5 character-times have elapsed. The cost is bounded (microseconds at typical baud rates).

### 3. Retry policy

`pymodbus` exposes a single `retries: int` parameter applied to every transaction regardless of function code. Reads and writes are retried identically.

`anymodbus.RetryPolicy` defaults to `retry_idempotent_only=True`: reads (FC 1-4) retry on transient transport errors; writes (FC 5/6/15/16) raise immediately. This protects against the case where a write succeeded on the slave but the response was lost in transit, which a blind retry would silently double-fire. You can opt back into write retries explicitly.

### 4. Connection-on-sustained-timeout

`pymodbus` closes the connection after `retries + 3` consecutive timeouts and (if configured) auto-reconnects. This is a deliberate model that handles flaky devices well — sustained no-response is treated as a transport-level fault.

`anymodbus` raises `FrameTimeoutError` on each timeout with no connection effect. The application decides what to do. Neither model is wrong; they make different tradeoffs around explicit-control vs convenience.

### 5. Type-checker strictness

| | pymodbus | anymodbus |
|---|---|---|
| `py.typed` | Yes | Yes |
| `mypy strict = true` | No (partial flags) | Yes |
| `pyright typeCheckingMode` | `standard` | `strict` |

If your project also runs mypy strict / pyright strict, pymodbus's annotations may surface gaps that `anymodbus` won't.

### 6. Default parity

`pymodbus` defaults `parity="N"` because that's what most real devices use. `anymodbus` requires `parity=` as an explicit keyword with no default — opinionated about forcing the user to confirm against the device manual. The cost is one extra line of code per `open_modbus_rtu` call; the benefit is that mismatched parity (which silently drops every frame) becomes harder to introduce by accident.

### 7. Scope

`pymodbus` is RTU + TCP + ASCII + UDP + TLS, client + server + simulator. `anymodbus` is RTU client only. If you need any of the other modes, you need pymodbus or another library — `anymodbus` is not a drop-in replacement for the full feature set.

## When to stay on pymodbus

- You're using TCP, ASCII, UDP, or running a server.
- You're on asyncio and pymodbus is working fine.
- You rely on its auto-reconnect behavior.
- You need its battle-tested handling of flaky industrial hardware.

## When to move to anymodbus

- You're already on AnyIO or want trio.
- You want strict typing in your dependency graph.
- You want explicit control over retry semantics, especially for writes.
- You're building a downstream device library and want a small, focused dependency.

Honest summary: `pymodbus` and `anymodbus` are different points on the same design space. The two libraries can and do coexist.
