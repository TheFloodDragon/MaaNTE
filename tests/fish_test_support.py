"""Load fishing modules without importing all registered game actions."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_maante_fish_tests"


def load_fish_module(name: str):
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT / "agent/custom/action/AutoFish")]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")
