# Capabilities

`anymodbus` exposes a tri-state capability model (`SUPPORTED` / `UNSUPPORTED` / `UNKNOWN`) for slave function-code support. Many users won't need this — if you know your device's protocol map, skip probing entirely. But for tools that scan unknown slaves, the model is there.

## When to probe

- **Skip it.** If you're writing a downstream device library and you already know your device supports FC 3, just call `slave.read_holding_registers(...)`. Probing burns transactions on the bus for no information you didn't already have.
- **Probe.** If you're writing a generic scanner, an unknown-device discovery tool, or a UI that wants to enable/disable controls based on what the slave can do, probe once at startup and cache.

## Probing

```python
from anymodbus import Capability, FunctionCode

caps = await slave.probe()

if caps.get(FunctionCode.READ_HOLDING_REGISTERS) is Capability.SUPPORTED:
    regs = await slave.read_holding_registers(0, count=4)
```

`Slave.probe()` issues one `count=1` request per read FC (1–4) and walks a small set of probe addresses to handle slaves whose register space starts above 0. The result is cached on the slave handle and returned (also accessible via `slave.capabilities`).

## How outcomes are interpreted

| Probe response | Verdict |
|----------------|---------|
| Successful read | `SUPPORTED` |
| `IllegalFunctionError` (exception code 0x01) | `UNSUPPORTED` — the slave understood the request and explicitly refused the FC, per *App Protocol §7* |
| `IllegalDataAddressError` at all probe addresses | `UNKNOWN` — the FC works but every address we tried was invalid; callers shouldn't assume blanket support |
| `FrameTimeoutError` / `ConnectionLostError` | `UNKNOWN` — a silent slave is indistinguishable from a slave that drops unsupported FCs without responding |

A timeout on one FC short-circuits further probing for the rest — if the slave isn't responding to FC 1, it's almost certainly not going to respond to FC 4 either, and you don't want to wait through `len(FCs) * request_timeout` seconds before giving up.

## Why writes aren't probed

There's no spec-defined non-destructive write probe. Issuing FC 5/6/0x0F/0x10 to a real slave would change state. So write FCs always come back as `Capability.UNKNOWN` from `probe()` — that lets callers distinguish "we didn't probe" from "the slave refused".

If you need to know whether writes work, the only honest answer is to try one against an address you control.

## `SlaveCapabilities` shape

```python
from anymodbus import SlaveCapabilities

@dataclass(frozen=True, slots=True, kw_only=True)
class SlaveCapabilities:
    function_codes: Mapping[FunctionCode, Capability]
    max_coils_per_read: int | None = None
    max_registers_per_read: int | None = None

    def get(self, fc: FunctionCode) -> Capability:
        ...  # defaults to UNKNOWN
```

`max_coils_per_read` / `max_registers_per_read` aren't filled in by `probe()` — they're caller-set hints for code that wants to chunk a large request. Spec ceilings are 2000 coils / 125 registers; many real devices accept fewer.

## When the cache is stale

The cache is **not** invalidated automatically. If you swap the device on the bus (slave address re-used), call `probe()` again. There's no probe-on-failure recovery — if a previously-`SUPPORTED` FC starts returning `IllegalFunctionError`, your code sees it as a normal Modbus exception, not a capability change.
