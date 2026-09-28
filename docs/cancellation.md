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

The bus stays usable, with one hazard to know about: the slave may still answer.

## Late replies

A request that timed out or was cancelled was usually still received by the slave, and its reply may still be on its way. The bus flushes the input buffer before each request, but that only removes bytes that have *already* arrived. A reply that lands after the next request went out is read as that request's reply:

- **Same function code and length** (for example two FC 03 reads of the same count at different addresses): it is accepted. An FC 03/04 reply carries no register address, so the second read silently returns the first read's data.
- **Different function code or length**: `UnexpectedResponseError`.

The same applies after any reply the bus could not use — a CRC or framing error, or a reply that did not answer the request — because the real reply may follow it.

Set `TimingConfig.late_reply_window` to close the gap. After an attempt whose outcome is uncertain, the bus sends nothing until that long after the attempt ended; meanwhile it reads and discards whatever arrives, and it also waits until the line has been quiet for the inter-frame gap. A normal reply or a Modbus exception response leaves the line in a known state and opens no window, and neither does an attempt cancelled before its request was sent. Retries wait out the window too, so a retried read never takes the previous attempt's reply.

```python
from anymodbus import BusConfig, TimingConfig, estimate_late_reply_window

window = estimate_late_reply_window(
    baudrate=38_400,
    max_turnaround=0.030,  # the device's slowest reply, from its manual
    max_reply_bytes=133,  # the largest reply you read (here 64 registers)
    latency=0.016,  # a USB adapter's latency timer
)
config = BusConfig(timing=TimingConfig(late_reply_window=window))  # ~0.08 s
```

The window is measured from the end of the uncertain attempt, never earlier than its request was sent, so it only has to cover one request-to-end-of-reply time. The cost is paid only after a failure. Leave it at 0 on a port shared with another reader (`reset_input_buffer_before_request=False`): the discard would consume that reader's bytes. A transaction observer reports each request's `discarded_bytes`, so you can see when a late reply was caught.

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

If you wrap a call in `anyio.fail_after(deadline)` shorter than that, the outer scope wins — retries respect outer cancellation. With a `late_reply_window`, add it once per retry after an uncertain attempt.

To count retries and recovered errors, register a [transaction observer](observers.md): it reports every attempt, with `will_retry` and the attempt number.

## Cancellation vs the broadcast turnaround

`Bus.broadcast_*` methods hold the bus lock for `TimingConfig.broadcast_turnaround` seconds after sending. Cancelling during that window is fine — the lock releases, the next caller waits the inter-frame idle as usual, and slaves still get their full processing window because they only see what's on the wire (which already happened).

## What `anymodbus` does NOT do

- **No automatic reconnection.** If the stream reports a disconnect (`BrokenResourceError`) or another OS-level port failure, the bus surfaces `ConnectionLostError` (the latter as its subclass `TransportError`) — treat the port as gone and open a new bus. Auto-reconnect is on the roadmap as a thin `ResilientBus` wrapper. (`pymodbus` reconnects after `retries+3` consecutive timeouts; we deliberately do not.)
- **No internal retry loop independent of `RetryPolicy`.** What you configure is what runs.
