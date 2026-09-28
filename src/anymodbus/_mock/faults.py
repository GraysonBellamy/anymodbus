"""Fault-injection plan for :class:`MockSlave`.

Lets tests script transient failures (CRC corruption, response delay,
wrong slave address, dropped bytes) without writing custom mocks for
each scenario.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class FaultPlan:
    """A scripted sequence of faults to inject into a :class:`MockSlave`.

    Each field describes an independent fault mode; faults compose. ``None``
    on an integer field means "never trigger this mode".

    The ``*_after_n`` fields name a response by its 0-based index among the
    responses this slave sends, and fire once, on that response only:
    ``corrupt_crc_after_n=0`` corrupts the first response, ``=2`` the third.
    Requests the slave does not answer (addressed to another slave, or
    broadcasts) do not count.

    Attributes:
        corrupt_crc_after_n: Send the response with this 0-based index with a
            corrupted checksum (CRC for RTU, LRC for ASCII), then resume normal
            operation.
        delay_response_seconds: Hold every response by this many seconds
            before sending. Useful for timeout testing.
        wrong_slave_address: Echo this address in the response instead of
            the slave's real address.
        drop_response_after_n: Drop the response with this 0-based index
            entirely, then resume normal operation.
    """

    corrupt_crc_after_n: int | None = None
    delay_response_seconds: float = 0.0
    wrong_slave_address: int | None = None
    drop_response_after_n: int | None = None


__all__ = ["FaultPlan"]
