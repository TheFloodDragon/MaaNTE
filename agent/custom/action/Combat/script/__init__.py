"""战斗脚本编排层。

四个模块各司其职，依赖方向是单向的：

    schema  ->  conditions        （解析规则时构造条件）
    engine  ->  schema/conditions （按状态选规则，纯逻辑）
    primitives -> schema          （把动作翻译成内核调用）

``engine`` 与 ``primitives`` 互不依赖：前者只决定"做什么"，后者只负责
"怎么做"。因此决策逻辑可以完全离线测试。
"""

from __future__ import annotations

from .conditions import CombatState, Condition, parse_condition
from .engine import (
    IDLE,
    REASON_FALLBACK,
    REASON_IDLE,
    REASON_RULE,
    CombatEngine,
    Decision,
    RuleLedger,
)
from .primitives import ActionRunner
from .schema import Action, CombatScript, Rule, parse_script

__all__ = [
    "Action",
    "ActionRunner",
    "CombatEngine",
    "CombatScript",
    "CombatState",
    "Condition",
    "Decision",
    "IDLE",
    "REASON_FALLBACK",
    "REASON_IDLE",
    "REASON_RULE",
    "Rule",
    "RuleLedger",
    "parse_condition",
    "parse_script",
]
