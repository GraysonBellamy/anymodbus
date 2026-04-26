"""Private mock-slave + fault-injection internals.

Public entry points live in :mod:`anymodbus.testing`. This subpackage
may be restructured between releases; do not import from it directly
in user code.
"""

from __future__ import annotations

from anymodbus._mock.faults import FaultPlan
from anymodbus._mock.pair import client_slave_pair
from anymodbus._mock.slave import MockSlave

__all__ = ["FaultPlan", "MockSlave", "client_slave_pair"]
