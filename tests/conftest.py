"""Shared pytest fixtures.

The ``anyio_backend`` fixture is parametrized across the full backend matrix:
asyncio (default), asyncio+uvloop when uvloop is installed, and trio. This uses
AnyIO's built-in pytest plugin; do NOT add ``pytest-anyio`` as a separate
dependency.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.mark.structures import ParameterSet

# Order matters for pyright on Windows: it narrows ``sys.platform == "win32"``
# to ``True`` and would mark the short-circuited ``find_spec`` branch (and its
# import) unreachable. Evaluating ``find_spec`` first keeps the import "used" on
# every platform; it is cheap and side-effect-free.
_UVLOOP_UNAVAILABLE = importlib.util.find_spec("uvloop") is None or sys.platform == "win32"

_PARAMS: list[ParameterSet] = [
    pytest.param(("asyncio", {"use_uvloop": False}), id="asyncio"),
    pytest.param(
        ("asyncio", {"use_uvloop": True}),
        id="asyncio+uvloop",
        marks=pytest.mark.skipif(
            _UVLOOP_UNAVAILABLE,
            reason="uvloop is unsupported or not installed on this platform",
        ),
    ),
    pytest.param("trio", id="trio"),
]


@pytest.fixture(params=_PARAMS)
def anyio_backend(request: pytest.FixtureRequest) -> object:
    """Run async tests against asyncio, asyncio+uvloop when available, and trio."""
    return request.param
