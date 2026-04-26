"""Smoke-import every script in ``examples/``.

Examples are not run end-to-end here (they all need real hardware), but they
*must* import cleanly against the current public API. A drift between the
public surface and an example silently rotting on disk is exactly the failure
mode this test prevents.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
_EXAMPLE_PATHS = sorted(p for p in _EXAMPLES_DIR.glob("*.py") if not p.name.startswith("_"))


@pytest.mark.parametrize("path", _EXAMPLE_PATHS, ids=[p.stem for p in _EXAMPLE_PATHS])
def test_example_imports(path: Path) -> None:
    spec = importlib.util.spec_from_file_location(f"_anymodbus_example_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
