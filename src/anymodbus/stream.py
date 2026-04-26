"""Convenience entry point: :func:`open_modbus_rtu`.

Opens an :class:`anyserial.SerialPort` with sensible Modbus-RTU defaults
and wraps it in a :class:`Bus`. Power users who already hold a serial port
(or any AnyIO byte stream) should construct :class:`Bus` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from anyserial import Parity, SerialConfig, open_serial_port

from anymodbus.bus import Bus
from anymodbus.exceptions import ConfigurationError

if TYPE_CHECKING:
    from anymodbus.config import BusConfig

ParityLiteral = Literal["none", "even", "odd", "mark", "space"]

_PARITY_MAP: Final[dict[str, Parity]] = {
    "none": Parity.NONE,
    "even": Parity.EVEN,
    "odd": Parity.ODD,
    "mark": Parity.MARK,
    "space": Parity.SPACE,
}


async def open_modbus_rtu(
    path: str,
    *,
    baudrate: int,
    parity: ParityLiteral,
    config: BusConfig | None = None,
) -> Bus:
    """Open a serial port and return a :class:`Bus` ready to issue requests.

    ``baudrate`` and ``parity`` are required keyword arguments with no
    defaults. The Modbus RTU spec specifies 8E1 (even parity), but real
    devices vary too widely for any default to be safely portable —
    consult the device's protocol manual.

    Args:
        path: Serial device path (e.g., ``/dev/ttyUSB0``, ``COM3``).
        baudrate: Bits per second. Common values: 9600, 19200, 38400, 115200.
        parity: ``"even"`` for spec-conformant RTU; ``"none"`` and ``"odd"``
            also occur in the field.
        config: Bus-level configuration. Defaults to :class:`BusConfig()`.

    Returns:
        An open :class:`Bus`. Use as ``async with await open_modbus_rtu(...) as bus:``.

    Raises:
        ConfigurationError: ``parity`` is not one of the supported values.
    """
    parity_enum = _PARITY_MAP.get(parity)
    if parity_enum is None:
        msg = f"parity must be one of {sorted(_PARITY_MAP)}; got {parity!r}"
        raise ConfigurationError(msg)
    serial_cfg = SerialConfig(baudrate=baudrate, parity=parity_enum)
    port = await open_serial_port(path, serial_cfg)
    return Bus(port, config=config)


__all__ = ["ParityLiteral", "open_modbus_rtu"]
