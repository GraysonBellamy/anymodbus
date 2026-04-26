# anymodbus

Async-native Modbus RTU client for Python, built on [AnyIO](https://anyio.readthedocs.io/) and [anyserial](https://github.com/GraysonBellamy/anyserial).

!!! warning "Alpha"
    The v0.1 surface is implemented and tested but has not yet been exercised against a wide range of real hardware. Expect minor API tweaks before v1.0. See [DESIGN.md](https://github.com/GraysonBellamy/anymodbus/blob/main/DESIGN.md) for the full plan.

## What it is

A small, opinionated Modbus RTU **client** built on AnyIO and `anyserial`. The design tradeoffs that distinguish it from `pymodbus` (the dominant existing library):

- **AnyIO-native** — works under `asyncio`, `uvloop`, and `trio`.
- **Tx-side enforced 3.5-character inter-frame gap** before each send.
- **Idempotent-only retries by default** — writes do not silently retry.
- **Strict typing** — `mypy strict` plus `pyright strict`.
- **Required `baudrate` and `parity`** — no spec-vs-real-device default trap.
- **Narrow scope** — RTU client only.

For TCP, ASCII, server-side support, or a battle-tested option, use `pymodbus`. See [Migration from pymodbus](migration-from-pymodbus.md) for an honest comparison.

## What it isn't

- A Modbus server.
- A Modbus ASCII implementation.
- A Modbus TCP implementation (planned for v0.3).
- A device driver. Vendor-specific register maps and quirks belong in downstream packages layered on top.

## Where to start

- [Quickstart](quickstart.md) — minimal async + sync examples.
- [RTU framing](rtu.md) — the wire format and why we read by length.
- [Decoders](decoders.md) — the float/int32 word-order matrix.
- [Migration from pymodbus](migration-from-pymodbus.md) — what `anymodbus` does differently and why.
- [Design](https://github.com/GraysonBellamy/anymodbus/blob/main/DESIGN.md) — full architecture document.

## License

MIT.
