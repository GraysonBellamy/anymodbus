"""Public test helpers.

Import :class:`MockSlave`, :class:`FaultPlan`, and :func:`client_slave_pair`
from here in test suites — both inside ``anymodbus`` and in downstream
device libraries that wrap the protocol layer. The ``_mock`` subpackage is
private and may be restructured between releases.
"""

from __future__ import annotations

from anymodbus._mock import FaultPlan, MockSlave, client_slave_pair

__all__ = ["FaultPlan", "MockSlave", "client_slave_pair"]
