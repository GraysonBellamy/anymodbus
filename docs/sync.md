# Sync wrapper

For scripts and test benches that don't want an event loop, `anymodbus.sync` mirrors the async API as a blocking facade.

## Quickstart

```python
from anymodbus.sync import open_modbus_rtu

with open_modbus_rtu("/dev/ttyUSB0", baudrate=19_200, parity="even") as bus:
    slave = bus.slave(1)
    regs = slave.read_holding_registers(0, count=4, timeout=1.0)
    slave.write_register(0x0080, value=2500)
```

Every blocking call accepts an optional `timeout=` keyword that wraps the underlying awaitable in `anyio.fail_after` on the portal thread. Expiry surfaces as the stdlib `TimeoutError` (which `anymodbus.FrameTimeoutError` already inherits from, so `except FrameTimeoutError:` works too).

## How it works — the shared portal

`anymodbus.sync` piggybacks on `anyserial.sync`'s process-wide `BlockingPortalProvider`. Opening a sync `anymodbus.Bus` while a sync `anyserial.SerialPort` is already open in the same process **does not** spawn a second event-loop thread — they share the one portal.

This matters in mixed codebases: if you already use sync `anyserial` somewhere, sync `anymodbus` reuses that thread for free.

## Configuring the portal

To pick a non-default AnyIO backend (e.g. `trio` for the portal thread), use `configure_portal`, re-exported from `anyserial.sync`:

```python
from anymodbus.sync import configure_portal

configure_portal(backend="trio")  # call once at process startup, before any sync open
```

Call `configure_portal` **before** the first `open_modbus_rtu` / `anyserial` open in the process; once the portal has started, the backend is fixed for the lifetime of the process.

## When to use sync vs async

- **Sync:** scripts, REPL exploration, test benches, glue code in mostly-sync codebases.
- **Async:** anything polling many slaves, anything in a larger async application, anything where latency matters.

The portal hop costs tens to hundreds of microseconds per call. That's negligible for setup and one-shot reads, but visible on tight polling loops — prefer async for those.

## What's mirrored

The sync surface mirrors the async one verbatim, with `timeout=` keywords added:

| Async | Sync |
|-------|------|
| `await open_modbus_rtu(...)` | `open_modbus_rtu(...)` |
| `bus.slave(addr)` | `bus.slave(addr)` |
| `await slave.read_holding_registers(0, count=4)` | `slave.read_holding_registers(0, count=4, timeout=1.0)` |
| `await bus.broadcast_write_register(addr, value)` | `bus.broadcast_write_register(addr, value, timeout=1.0)` |
| `async with bus:` | `with bus:` |

Construction takes an `async_bus=` keyword for tests that already hold an async `Bus` and want to drive it from sync code without re-opening the port.

## Cleanup

`with open_modbus_rtu(...) as bus:` closes the bus on exit. If you forget the context manager, the bus's `__del__` warns and best-effort closes — but don't rely on that; the portal callback during finalisation has fewer guarantees than in-context teardown.
