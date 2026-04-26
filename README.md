# anymodbus

Async-native Modbus RTU client for Python, built on [AnyIO](https://anyio.readthedocs.io/) and [anyserial](https://github.com/GraysonBellamy/anyserial).

[![CI](https://github.com/GraysonBellamy/anymodbus/actions/workflows/ci.yml/badge.svg)](https://github.com/GraysonBellamy/anymodbus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/anymodbus.svg)](https://pypi.org/project/anymodbus/)
[![Python versions](https://img.shields.io/pypi/pyversions/anymodbus.svg)](https://pypi.org/project/anymodbus/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> [!WARNING]
> **Alpha.** The v0.1 surface is implemented and tested but has not yet been
> exercised against a wide range of real hardware. Expect minor API tweaks
> before v1.0. See [DESIGN.md](DESIGN.md) for the full plan and
> [CHANGELOG.md](CHANGELOG.md) for the current state.

## Overview

`anymodbus` is a small, opinionated Modbus RTU **client** built on AnyIO and `anyserial`. It is intentionally protocol-only and narrow in scope. It does not ship servers, ASCII transport, or device-specific drivers — `pymodbus` is the right choice if you need any of those, and the two libraries can coexist in one project.

The cases `anymodbus` is built for:

- **AnyIO-native.** Same code runs under `asyncio`, `uvloop`, or `trio`. `pymodbus` is asyncio-only.
- **Tx-side 3.5-char inter-frame gap.** Enforced before sending. `pymodbus` relies on its concurrency lock plus OS scheduling and does not enforce a pre-tx idle gap.
- **Idempotent-only retries by default.** Reads (FC 1-4) retry on transient transport errors; writes (FC 5/6/15/16) do not, unless you opt in. Protects against silent double-writes when a successful write's response is lost in transit.
- **Strict typing.** `mypy strict = true` plus `pyright typeCheckingMode = "strict"`. `pymodbus` uses partial-strict mypy and `standard` pyright.
- **Required `baudrate` and `parity`.** No defaults — mismatched parity silently drops every frame, so making it explicit at the call site is worth the small ergonomic cost.
- **Transport-agnostic.** Takes any `anyio.abc.ByteStream`, defaults to an `anyserial.SerialPort`. TCP support is planned in v0.3 with the same `Bus` API.

See [docs/migration-from-pymodbus.md](docs/migration-from-pymodbus.md) for an honest comparison.

## Requirements

- Python 3.13 or 3.14
- `anyio >= 4.13`
- `anyserial >= 0.1.1`

## Installation

```bash
uv add anymodbus
# or
pip install anymodbus
```

Optional extras:

```bash
uv add "anymodbus[trio]"    # trio runtime
```

## Usage

### Async, one slave

```python
import anyio
from anymodbus import open_modbus_rtu


async def main() -> None:
    async with await open_modbus_rtu("/dev/ttyUSB0", baudrate=19_200, parity="even") as bus:
        slave = bus.slave(address=1)
        regs = await slave.read_holding_registers(0x0040, count=2)
        print(regs)


anyio.run(main)
```

### Sync

```python
from anymodbus.sync import open_modbus_rtu

with open_modbus_rtu("/dev/ttyUSB0", baudrate=19_200, parity="even") as bus:
    slave = bus.slave(1)
    regs = slave.read_holding_registers(0, count=4, timeout=1.0)
```

### Reading a 32-bit float across two registers

Word order varies by device — the Modbus spec doesn't standardize multi-register layout. The default is `HIGH_LOW` × big-endian-within-word, equivalent to `struct.pack(">f", ...)`; pass `word_order="low_high"` for devices that store the LSW first.

```python
from anymodbus import WordOrder

async with await open_modbus_rtu("/dev/ttyUSB0", baudrate=19_200, parity="even") as bus:
    slave = bus.slave(address=1)
    high_low_value = await slave.read_float(0x0040)                              # default
    low_high_value = await slave.read_float(0x0044, word_order=WordOrder.LOW_HIGH)
```

### Wrapping an existing serial port (with RS-485)

```python
from anyserial import open_serial_port, SerialConfig, RS485Config, Parity
from anymodbus import Bus

port = await open_serial_port(
    "/dev/ttyUSB0",
    SerialConfig(
        baudrate=19_200,
        parity=Parity.EVEN,
        rs485=RS485Config(enabled=True, rts_on_send=True, rts_after_send=False),
    ),
)
async with Bus(port) as bus:
    regs = await bus.slave(1).read_holding_registers(0, count=4)
```

### Concurrent fan-out across multiple buses

```python
import anyio
from anymodbus import open_modbus_rtu


async def poll_one(path: str, results: dict[str, tuple[int, ...]]) -> None:
    async with await open_modbus_rtu(path, baudrate=19_200, parity="even") as bus:
        results[path] = await bus.slave(1).read_holding_registers(0, count=4)


async def main() -> None:
    paths = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"]
    results: dict[str, tuple[int, ...]] = {}
    async with anyio.create_task_group() as tg:
        for p in paths:
            tg.start_soon(poll_one, p, results)
    for path, regs in results.items():
        print(path, regs)


anyio.run(main)
```

### Testing without hardware

```python
from anymodbus.testing import client_slave_pair

async with client_slave_pair(slave_address=1) as (bus, mock):
    mock.holding_registers[0:4] = [10, 20, 30, 40]
    regs = await bus.slave(1).read_holding_registers(0, count=4)
    assert regs == (10, 20, 30, 40)
```

## Documentation

Full documentation will live at <https://graysonbellamy.github.io/anymodbus/>. Starting points:

- [Quickstart](docs/quickstart.md)
- [Configuration](docs/configuration.md)
- [RTU framing](docs/rtu.md)
- [Decoders & word order](docs/decoders.md)
- [Exceptions](docs/exceptions.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Migration from pymodbus](docs/migration-from-pymodbus.md)

## Contributing

Issues and PRs are welcome. To get a local checkout running:

```bash
git clone https://github.com/GraysonBellamy/anymodbus
cd anymodbus
uv sync --all-extras
uv run pre-commit install
```

Before opening a PR:

```bash
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pyright
```

Hardware-dependent tests are opt-in via `pytest -m hardware` with `ANYMODBUS_TEST_PORT` and `ANYMODBUS_TEST_SLAVE_ADDRESS` set.

## License

MIT. See [LICENSE](LICENSE).
