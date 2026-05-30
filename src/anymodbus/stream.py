"""Convenience entry points: :func:`open_modbus_rtu` / :func:`open_modbus_ascii`.

Opens an :class:`anyserial.SerialPort` with sensible Modbus defaults and wraps
it in a :class:`Bus`. Power users who already hold a serial port (or any AnyIO
byte stream) should construct :class:`Bus` directly — e.g.
``Bus(my_stream, framing=Framing.ASCII)`` is the blessed "I own the port" path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from anyserial import ByteSize, Parity, SerialConfig, open_serial_port

from anymodbus._types import Framing
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

# anyserial's ByteSize is a StrEnum keyed on str values ("7"/"8"); int lookup
# raises, so map explicitly. Modbus ASCII is classically 7E1; RTU is 8-bit only.
_BYTE_SIZE_MAP: Final[dict[int, ByteSize]] = {7: ByteSize.SEVEN, 8: ByteSize.EIGHT}


def _resolve_parity(parity: ParityLiteral) -> Parity:
    parity_enum = _PARITY_MAP.get(parity)
    if parity_enum is None:
        msg = f"parity must be one of {sorted(_PARITY_MAP)}; got {parity!r}"
        raise ConfigurationError(msg)
    return parity_enum


async def _open(
    path: str,
    *,
    baudrate: int,
    parity: ParityLiteral,
    data_bits: int,
    framing: Framing,
    config: BusConfig | None,
) -> Bus:
    """Shared opener body for RTU/ASCII; constructs the port and the Bus."""
    parity_enum = _resolve_parity(parity)
    byte_size = _BYTE_SIZE_MAP.get(data_bits)
    if byte_size is None:
        msg = f"data_bits must be 7 or 8 (got {data_bits!r})"
        raise ConfigurationError(msg)
    serial_cfg = SerialConfig(baudrate=baudrate, parity=parity_enum, byte_size=byte_size)
    port = await open_serial_port(path, serial_cfg)
    return Bus(port, config=config, framing=framing)


async def open_modbus_rtu(
    path: str,
    *,
    baudrate: int,
    parity: ParityLiteral,
    config: BusConfig | None = None,
) -> Bus:
    """Open a serial port and return an RTU :class:`Bus` ready to issue requests.

    ``baudrate`` and ``parity`` are required keyword arguments with no
    defaults. The Modbus RTU spec specifies 8E1 (even parity), but real
    devices vary too widely for any default to be safely portable —
    consult the device's protocol manual. RTU is **8-bit only** by spec, so
    there is no data-bits knob here (see :func:`open_modbus_ascii`).

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
    return await _open(
        path,
        baudrate=baudrate,
        parity=parity,
        data_bits=8,
        framing=Framing.RTU,
        config=config,
    )


async def open_modbus_ascii(
    path: str,
    *,
    baudrate: int,
    parity: ParityLiteral,
    data_bits: int = 8,
    config: BusConfig | None = None,
) -> Bus:
    """Open a serial port and return a Modbus-**ASCII** :class:`Bus`.

    Modbus ASCII is classically **7E1** (7 data bits + even parity); an 8-bit
    receiver cannot read a 7E1 sender (it reads the parity bit as data), so this
    opener exposes ``data_bits`` (7 or 8). Both work on the wire — every ASCII
    frame byte is printable 7-bit ASCII — so 8-bit links are fine too.

    Callers needing other serial parameters should construct
    ``Bus(open_serial_port(...), framing=Framing.ASCII)`` directly.

    Args:
        path: Serial device path (e.g., ``/dev/ttyUSB0``, ``COM3``).
        baudrate: Bits per second.
        parity: ``"even"`` for the classic 7E1 wire; others occur in the field.
        data_bits: 7 (classic 7E1) or 8. Default 8.
        config: Bus-level configuration. Defaults to :class:`BusConfig()`.

    Raises:
        ConfigurationError: ``parity`` is unsupported or ``data_bits`` is not 7/8.
    """
    return await _open(
        path,
        baudrate=baudrate,
        parity=parity,
        data_bits=data_bits,
        framing=Framing.ASCII,
        config=config,
    )


__all__ = ["ParityLiteral", "open_modbus_ascii", "open_modbus_rtu"]
