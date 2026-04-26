"""Read a 32-bit float spread across two consecutive holding registers.

Modbus has no standardized layout for 32-bit values: each device chooses its
own word order and (less often) byte order within each word. ``anymodbus``
defaults to the Modbus Application Protocol spec's worked example
(``WordOrder.HIGH_LOW``, ``ByteOrder.BIG``), equivalent to
``struct.pack(">f", value)``. **Always check your device's protocol manual**
before relying on the defaults.

This example demonstrates both orderings on the same address pair so you can
see which one matches your device.

Run with:

    uv run python examples/03_float_setpoint.py /dev/ttyUSB0 19200 even 1 0x0040
"""

from __future__ import annotations

import sys
from typing import cast, get_args

import anyio

from anymodbus import WordOrder, open_modbus_rtu
from anymodbus.stream import ParityLiteral


async def main(
    path: str,
    baudrate: int,
    parity: ParityLiteral,
    slave_address: int,
    register: int,
) -> None:
    async with await open_modbus_rtu(path, baudrate=baudrate, parity=parity) as bus:
        slave = bus.slave(address=slave_address)

        high_low = await slave.read_float(register, word_order=WordOrder.HIGH_LOW)
        low_high = await slave.read_float(register, word_order=WordOrder.LOW_HIGH)

        sys.stdout.write(f"register 0x{register:04X} as HIGH_LOW = {high_low:g}\n")
        sys.stdout.write(f"register 0x{register:04X} as LOW_HIGH = {low_high:g}\n")
        sys.stdout.write("Whichever value looks plausible is your device's word order.\n")


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.stderr.write(
            "usage: 03_float_setpoint.py <serial-path> <baud> <parity> "
            "<slave-addr> <register-hex>\n"
        )
        sys.exit(2)
    valid_parities = get_args(ParityLiteral)
    parity_arg = sys.argv[3]
    if parity_arg not in valid_parities:
        sys.stderr.write(f"parity must be one of {valid_parities!r}, got {parity_arg!r}\n")
        sys.exit(2)
    register_arg = sys.argv[5]
    register_value = int(register_arg, 16) if register_arg.startswith("0x") else int(register_arg)
    anyio.run(
        main,
        sys.argv[1],
        int(sys.argv[2]),
        cast("ParityLiteral", parity_arg),
        int(sys.argv[4]),
        register_value,
    )
