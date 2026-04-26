# Cancellation

`anymodbus` uses standard AnyIO cancel scopes — there's no library-specific timer mechanism to learn.

## Per-call timeout

Every transaction is wrapped in `BusConfig.request_timeout` by default (3.0 s). The library never enforces a deadline of its own beyond that; outer scopes always preempt:

```python
import anyio

# Outer 0.5s scope wins over the bus's 3.0s default.
with anyio.move_on_after(0.5):
    regs = await slave.read_holding_registers(0, count=4)
```

Override per-bus:

```python
from anymodbus import BusConfig, open_modbus_rtu

bus = await open_modbus_rtu(
    "/dev/ttyUSB0",
    baudrate=19_200,
    parity="even",
    config=BusConfig(request_timeout=1.0),
)
```

A `request_timeout` expiry surfaces as `FrameTimeoutError`, which inherits from the stdlib `TimeoutError` — so `except TimeoutError:` catches it without importing the library type.

## What the bus does on cancellation

If a transaction is cancelled mid-flight (outer scope, KeyboardInterrupt, task group teardown):

1. The pending `stream.send` / `stream.receive` is cancelled immediately by AnyIO.
2. The `async with bus._lock:` releases cleanly.
3. The next caller acquires the lock, enforces the inter-frame idle gap as usual, and proceeds.

The bus stays usable. There is no corrupted-state recovery ritual to perform.

## Cancel a fan-out

```python
async with anyio.create_task_group() as tg:
    with anyio.fail_after(2.0):
        for slave_id in range(1, 32):
            tg.start_soon(bus.slave(slave_id).read_holding_registers, 0, count=4)
```

Each task serializes through the bus lock; the outer `fail_after` cancels every still-queued task as soon as the deadline hits.

## Interaction with retries

`RetryPolicy.retries` is the number of *additional* attempts after the first, so the worst-case time spent in one `slave.read_*` call is roughly:

```
(retries + 1) * (request_timeout + inter_frame_idle + retry_policy.backoff_base)
```

If you wrap a call in `anyio.fail_after(deadline)` shorter than that, the outer scope wins — retries respect outer cancellation.

## Cancellation vs the broadcast turnaround

`Bus.broadcast_*` methods hold the bus lock for `TimingConfig.broadcast_turnaround` seconds after sending. Cancelling during that window is fine — the lock releases, the next caller waits the inter-frame idle as usual, and slaves still get their full processing window because they only see what's on the wire (which already happened).

## What `anymodbus` does NOT do

- **No automatic reconnection.** If `stream.send` raises `BrokenResourceError`, the bus surfaces `ConnectionLostError` and the bus is dead — open a new one. Auto-reconnect is on the v0.2/v0.3 roadmap as a thin `ResilientBus` wrapper. (`pymodbus` reconnects after `retries+3` consecutive timeouts; we deliberately do not.)
- **No internal retry loop independent of `RetryPolicy`.** What you configure is what runs.
