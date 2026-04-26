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

    Attributes:
        corrupt_crc_after_n: After this many requests, return one response
            with a corrupted CRC then resume normal operation.
        delay_response_seconds: Hold every response by this many seconds
            before sending. Useful for timeout testing.
        wrong_slave_address: Echo this address in the response instead of
            the slave's real address.
        drop_response_after_n: After this many requests, drop one response
            entirely.
    """

    corrupt_crc_after_n: int | None = None
    delay_response_seconds: float = 0.0
    wrong_slave_address: int | None = None
    drop_response_after_n: int | None = None


__all__ = ["FaultPlan"]
