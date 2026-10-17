"""MaaNTE 战斗系统。

公开接口（不依赖 maa，可在任意 CustomAction 中调用）：
    run_combat(context, overrides=None, option_nodes=(), reporter=None) -> CombatResult
    load_scheme(scheme, custom_scheme="") -> CombatScheme
    compile_combo(text) -> list[Command]

MaaFramework 注册（combat_run / combat_in_hud）在 actions.py，由 custom/action/__init__.py 导入。
"""

from .api import run_combat
from .dsl import Command, ComboError, compile_combo
from .engine import CombatEngine, EngineConfig, Reporter
from .locate import load_scheme
from .model import CombatResult, EndReason
from .scheme import CombatScheme, SchemeError, parse_scheme
from .settings import CombatOptions, merge_options

__all__ = [
    "run_combat",
    "load_scheme",
    "compile_combo",
    "parse_scheme",
    "merge_options",
    "Command",
    "ComboError",
    "CombatEngine",
    "EngineConfig",
    "Reporter",
    "CombatResult",
    "EndReason",
    "CombatScheme",
    "SchemeError",
    "CombatOptions",
]
