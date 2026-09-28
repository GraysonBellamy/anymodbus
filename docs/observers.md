# Transaction observers

A transaction observer is a plain function the bus calls after every attempt it makes, with a `TransactionInfo` describing it. Use one to timestamp readings with when the request really went out, to count errors and retries, or to log traffic.

```python
from anymodbus import TransactionInfo, TransactionOutcome


def on_transaction(info: TransactionInfo) -> None:
    if info.outcome is not TransactionOutcome.REPLY:
        print(f"attempt {info.attempt}/{info.max_attempts} to {info.slave_address}: {info.outcome}")


remove = bus.add_transaction_observer(on_transaction)
...
remove()  # stop observing
```

You can also pass one when constructing the bus: `Bus(stream, on_transaction=on_transaction)`. The sync wrapper has the same `add_transaction_observer`; there the observer runs on the event-loop thread, not the calling thread.

## When it is called

Once per attempt: a read that is retried reports each attempt, and a broadcast reports once. It is called as the attempt ends, however it ends — including by cancellation — with the bus lock held. So it must be **synchronous, quick, and never block**; hand anything slow to another task. Anything it raises is logged and ignored, never passed to the caller. Observers run in the order they were added.

Calls that end before an attempt starts are not reported: a closed bus, a bad argument, or a cancellation while waiting for the bus lock.

## What it reports

| Field | Meaning |
|---|---|
| `request_id` | The same for every attempt of one call; increases per bus. |
| `attempt`, `max_attempts` | 1-based attempt number, and `RetryPolicy.retries + 1` (1 for broadcasts). |
| `will_retry` | The bus will send the request again after this attempt. |
| `slave_address`, `function_code` | Where the request went (0 for a broadcast) and its FC. |
| `outcome` | A `TransactionOutcome`: `REPLY`, `EXCEPTION_REPLY`, `BROADCAST_SENT`, `TIMEOUT`, `CHECKSUM_ERROR`, `UNEXPECTED_RESPONSE`, `FRAME_ERROR`, `CONNECTION_ERROR`, `CANCELLED`, or `ERROR`. |
| `error` | The exception the attempt ended with; `None` for replies, broadcasts and cancellations. |
| `started_at` | When the attempt began, before waiting out the inter-frame gap. |
| `sent_at` | When the request had been written and drained, or `None` if it never finished sending. |
| `ended_at` | When the attempt ended. |
| `sent_at_ns`, `ended_at_ns` | The same instants on `time.monotonic_ns()`. |
| `discarded_bytes` | Bytes of a late reply dropped before this request (see [late replies](cancellation.md#late-replies)). |

The `*_at` fields use the AnyIO clock, the one the bus schedules its gaps and deadlines on. On trio that clock carries an arbitrary offset, so compare it only with other `anyio.current_time()` values; use the `*_ns` fields to compare with timestamps taken elsewhere.

## Timestamping readings

The bus waits out the inter-frame gap *inside* the call — 3.5 character times, rounded up to about 16 ms by the timer on Windows — and, after a failure, any late-reply window. So the time before `await slave.read_*()` is not when the request left; `sent_at` is. The observer runs before the call returns, so read what it recorded straight after the `await`:

```python
last_sent_ns = 0


def remember(info: TransactionInfo) -> None:
    global last_sent_ns
    if info.outcome is TransactionOutcome.REPLY and info.sent_at_ns is not None:
        last_sent_ns = info.sent_at_ns


bus.add_transaction_observer(remember)
words = await bus.slave(1).read_input_registers(0, count=4)
requested_ns = last_sent_ns  # when this read's request went out
```

## Counting errors and recovered errors

Keep the bus's own retries on and count from the reports:

```python
from collections import Counter

failures: Counter[TransactionOutcome] = Counter()
recovered = 0


def count(info: TransactionInfo) -> None:
    global recovered
    if info.outcome is TransactionOutcome.REPLY:
        recovered += info.attempt - 1  # failed attempts this call recovered from
    else:
        failures[info.outcome] += 1
```
