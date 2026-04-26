"""Capability snapshot for a Modbus slave.

Probing is optional — many users know their device's function-code support
ahead of time and skip the probe. When :meth:`Slave.probe` is called the
result lives on the slave handle; downstream code can branch on it without
re-issuing requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anymodbus._types import Capability, FunctionCode

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class SlaveCapabilities:
    """What a Modbus slave is known to support.

    Attributes:
        function_codes: Mapping of function code to a tri-state availability
            verdict. Codes not present in the mapping have not been probed
            and should be considered :attr:`Capability.UNKNOWN`.
        max_coils_per_read: Slave-reported (or assumed) maximum coil count
            per FC 1/2 request. Spec ceiling is 2000.
        max_registers_per_read: Slave-reported (or assumed) maximum register
            count per FC 3/4 request. Spec ceiling is 125.
    """

    function_codes: Mapping[FunctionCode, Capability]
    max_coils_per_read: int | None = None
    max_registers_per_read: int | None = None

    def get(self, fc: FunctionCode) -> Capability:
        """Return the capability for ``fc``, defaulting to ``UNKNOWN``."""
        return self.function_codes.get(fc, Capability.UNKNOWN)


# Probe interpretation guide for ``Slave.probe`` implementations and
# downstream callers building their own probe routines:
#
# - ``IllegalFunctionError`` (exception code 0x01) → ``UNSUPPORTED``. The
#   slave understood the request and explicitly refused the function code.
#   Per App Protocol §7, this is the spec-mandated response for an
#   unimplemented FC.
# - Any successful response → ``SUPPORTED``.
# - ``FrameTimeoutError`` or ``ConnectionLostError`` → ``UNKNOWN``. A silent
#   slave (offline, address mismatch, wrong baud) is indistinguishable from
#   a slave that drops unsupported FCs without responding. Do NOT downgrade
#   to ``UNSUPPORTED`` on timeout.
# - ``IllegalDataAddressError`` → ``SUPPORTED`` for the FC, but the address
#   we probed isn't valid. Move the probe to a known-valid address.

__all__ = ["SlaveCapabilities"]
