"""Test helpers: load pure-logic combat modules without MaaFramework.

``agent/custom/action/Combat/__init__.py`` registers Maa custom actions on
import, which needs the ``maa`` package. Tests instead register a stub
package named ``Combat`` (with ``__path__`` pointing at the real directory)
so that intra-package relative imports resolve while ``__init__`` and the
Maa-dependent modules stay unloaded.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMBAT_DIR = PROJECT_ROOT / "agent" / "custom" / "action" / "Combat"
PACKAGE = "Combat"


def _ensure_stub_package() -> None:
    if PACKAGE in sys.modules:
        return
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(COMBAT_DIR)]
    package.__package__ = PACKAGE
    sys.modules[PACKAGE] = package


def load_combat_module(name: str):
    """Load ``agent/custom/action/Combat/<name>.py`` as ``Combat.<name>``."""
    _ensure_stub_package()
    qualified = f"{PACKAGE}.{name}"
    if qualified in sys.modules:
        return sys.modules[qualified]

    spec = importlib.util.spec_from_file_location(qualified, COMBAT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(qualified, None)
        raise
    return module
