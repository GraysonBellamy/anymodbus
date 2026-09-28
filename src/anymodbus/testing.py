"""Public test helpers.

Import :class:`MockSlave`, :class:`MockServer`, :class:`FaultPlan`,
:class:`QuantityLimits`, :class:`ServerException`, :func:`client_slave_pair`
and :func:`client_server_pair` from here in test suites — both inside
``anymodbus`` and in downstream device libraries that wrap the protocol
layer. The ``_mock`` subpackage is private and may be restructured between
releases.

For a custom server that does not use :class:`MockSlave`, the building blocks
are public too: the request decoders and response encoders in
:mod:`anymodbus.pdu`, and ``get_framer(framing).read_request_adu(...)`` from
:mod:`anymodbus.framing` to read one request frame from a stream.
"""

from __future__ import annotations

from anymodbus._mock import (
    FaultPlan,
    MockServer,
    MockSlave,
    QuantityLimits,
    ServerException,
    client_server_pair,
    client_slave_pair,
)

__all__ = [
    "FaultPlan",
    "MockServer",
    "MockSlave",
    "QuantityLimits",
    "ServerException",
    "client_server_pair",
    "client_slave_pair",
]
