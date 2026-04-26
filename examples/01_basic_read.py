"""Read four holding registers from a Modbus RTU slave.

Run with:

    uv run python examples/01_basic_read.py /dev/ttyUSB0
"""

from __future__ import annotations

import sys

import anyio

from anymodbus import open_modbus_rtu


async def main(path: str) -> None:
    async with await open_modbus_rtu(path, baudrate=19_200, parity="none") as bus:
        slave = bus.slave(address=1)
        regs = await slave.read_holding_registers(0, count=4)
        for offset, value in enumerate(regs):
            sys.stdout.write(f"reg[{offset}] = 0x{value:04X} ({value})\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: 01_basic_read.py <serial-path>\n")
        sys.exit(2)
    anyio.run(main, sys.argv[1])
