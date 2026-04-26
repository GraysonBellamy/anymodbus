"""Open a serial port with explicit RS-485 settings, then wrap it in a Bus.

Use this pattern when you need to configure RS-485 direction control beyond
what ``open_modbus_rtu`` exposes — e.g., for a USB-RS485 adapter where the
kernel handles RTS-toggle automatically (``RS485Config(enabled=True)``).

Run with:

    uv run python examples/04_with_anyserial_rs485.py /dev/ttyUSB0
"""

from __future__ import annotations

import sys

import anyio
from anyserial import Parity, RS485Config, SerialConfig, open_serial_port

from anymodbus import Bus


async def main(path: str) -> None:
    port = await open_serial_port(
        path,
        SerialConfig(
            baudrate=19_200,
            parity=Parity.EVEN,
            rs485=RS485Config(
                enabled=True,
                rts_on_send=True,
                rts_after_send=False,
            ),
        ),
    )
    async with Bus(port) as bus:
        regs = await bus.slave(1).read_holding_registers(0, count=4)
        sys.stdout.write(f"regs = {regs}\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: 04_with_anyserial_rs485.py <serial-path>\n")
        sys.exit(2)
    anyio.run(main, sys.argv[1])
