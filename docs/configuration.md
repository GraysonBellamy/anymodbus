# Configuration

`anymodbus` uses three frozen dataclasses, all importable from the top level:

- [`BusConfig`](#busconfig) — top-level bus configuration.
- [`TimingConfig`](#timingconfig) — inter-frame and inter-character timing.
- [`RetryPolicy`](#retrypolicy) — when and how to retry on transient errors.

All are immutable; use `.with_changes(**fields)` to derive a new instance. Invalid values raise `ConfigurationError` at construction time (it inherits both `ModbusError` and `ValueError`, so existing `except ValueError` blocks still catch it).

## `BusConfig`

| Field | Default | Notes |
|---|---|---|
| `request_timeout` | `3.0` | Per-call deadline in seconds. Outer `anyio.move_on_after` scopes still preempt. Capped at 60 s. |
| `timing` | `TimingConfig()` | See below. |
| `retries` | `RetryPolicy()` | See below. |
| `drain_after_send` | `True` | Wait for the kernel TX queue to drain before reading. Important for RS-485. |
| `reset_input_buffer_before_request` | `True` | Flush stale rx bytes before each request. |

## `TimingConfig`

| Field | Default | Notes |
|---|---|---|
| `inter_frame_idle` | `"auto"` | Pre-tx idle gap. `"auto"` → `max(3.5 * 11 / baud, 1.75 ms)` (Serial Line spec §2.5.1.1). Pass a float to override. |
| `inter_char_idle` | `"auto"` | Rx-side idle gap for the unknown-FC fallback path only (1.5 char-times). |
| `post_tx_settle` | `0.0` | Optional delay after `send` returns and before reading. Some RS-485 transceivers benefit from 1-2 ms. |
| `broadcast_turnaround` | `0.1` | Seconds the bus must remain idle after a broadcast (slave_address=0) write. Spec calls this the "Turnaround delay" (§2.4.1) and recommends 100-200 ms so every slave finishes processing. The 3.5-char gap alone is insufficient for broadcasts. |

## `RetryPolicy`

| Field | Default | Notes |
|---|---|---|
| `retries` | `1` | Number of additional attempts after the first. Must be >= 0; no upper cap. |
| `retry_on` | `frozenset({CRCError, FrameTimeoutError})` | Exception classes that count as "transient." Modbus exception responses (`IllegalFunctionError`, etc.) are never retried regardless of this set — the slave told us no. |
| `retry_idempotent_only` | `True` | Only function codes with `is_idempotent_function(fc) is True` (today: FC 1-4) retry on transport errors; writes do not, to avoid double-firing if the request landed but the response was lost. |
| `backoff_base` | `0.0` | Extra seconds added after each retry, on top of the inter-frame idle. |

## Example

```python
from anymodbus import BusConfig, RetryPolicy, TimingConfig, open_modbus_rtu
from anymodbus.exceptions import CRCError, FrameTimeoutError

config = BusConfig(
    request_timeout=2.0,
    timing=TimingConfig(post_tx_settle=0.002),
    retries=RetryPolicy(
        retries=2,
        retry_on=frozenset({CRCError, FrameTimeoutError}),
        retry_idempotent_only=False,    # opt into write retries
        backoff_base=0.05,
    ),
)

bus = await open_modbus_rtu(
    "/dev/ttyUSB0",
    baudrate=19_200,
    parity="even",
    config=config,
)
```
