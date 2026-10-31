"""MaaNTE 战斗系统。

公开接口（不依赖 maa，可在任意 CustomAction 中调用）：
    run_combat(context, overrides=None, option_nodes=(), reporter=None,
               *, scheme_data=None, signal_nodes=None, script=None) -> CombatResult
    prepare_combat(context, overrides=None, option_nodes=(), *, scheme_data=None) -> CombatPlan
    load_scheme(scheme, custom_scheme="") -> CombatScheme
    compile_combo(text) -> list[Command]

overrides 的键见 CombatOptions（如 team_size、combat_speed、rotation_order、slots）。
结束原因见 EndReason；CombatResult.success 只在确认清场（cleared）时为 True。
脚本会话见 session.CombatSession。

MaaFramework 注册（combat_run / combat_in_hud / combat_enemy_bar）在 actions.py，由 custom/action/__init__.py 导入。
"""

from .api import prepare_combat, run_combat
from .dsl import Command, ComboError, compile_combo
from .engine import CombatEngine, EngineConfig, Reporter
from .locate import load_scheme
from .model import TOGGLE_ACTIONS, ActionResult, CombatResult, EndReason, Phase
from .plan import CombatPlan, SlotPlan, apply_overrides
from .scheme import CombatScheme, SchemeError, parse_scheme
from .session import CombatSession
from .settings import CombatOptions, SlotOverride, merge_options, merge_options_strict

__all__ = [
    "TOGGLE_ACTIONS",
    "run_combat",
    "prepare_combat",
    "load_scheme",
    "compile_combo",
    "parse_scheme",
    "merge_options",
    "merge_options_strict",
    "apply_overrides",
    "Command",
    "ComboError",
    "CombatEngine",
    "EngineConfig",
    "Reporter",
    "CombatResult",
    "ActionResult",
    "EndReason",
    "Phase",
    "CombatPlan",
    "SlotPlan",
    "CombatScheme",
    "SchemeError",
    "CombatSession",
    "CombatOptions",
    "SlotOverride",
]
