"""Poll multiple Modbus RTU buses concurrently.

Each bus runs its own task; same-bus requests serialize automatically through
the bus lock. Run with:

    uv run python examples/02_concurrent_polling.py /dev/ttyUSB0 /dev/ttyUSB1
"""

from __future__ import annotations

import sys

import anyio

from anymodbus import open_modbus_rtu


async def poll_one(path: str, results: dict[str, tuple[int, ...]]) -> None:
    async with await open_modbus_rtu(path, baudrate=19_200, parity="none") as bus:
        results[path] = await bus.slave(1).read_holding_registers(0, count=4)


async def main(paths: list[str]) -> None:
    results: dict[str, tuple[int, ...]] = {}
    async with anyio.create_task_group() as tg:
        for path in paths:
            tg.start_soon(poll_one, path, results)
    for path, regs in results.items():
        sys.stdout.write(f"{path}: {regs}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: 02_concurrent_polling.py <port> [<port> ...]\n")
        sys.exit(2)
    anyio.run(main, sys.argv[1:])
