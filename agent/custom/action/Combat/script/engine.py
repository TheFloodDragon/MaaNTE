"""战斗决策引擎：给定状态，选出该执行哪条规则。

**这一层不碰游戏**。``decide()`` 是纯函数：输入 ``CombatState``，输出
``Decision``（选中的规则 + 原因）。执行由调用方（runtime）负责。

这样拆分带来的好处是决策逻辑可以完全离线测试：想验证"血量低于 30% 时
优先吃药"，构造一个 state 断言 ``decide()`` 的结果即可，不需要游戏、
不需要截图、不需要 mock 内核。

冷却与触发次数由 ``RuleLedger`` 维护，它是引擎里**唯一**的可变状态，
且与决策逻辑分离，便于单独验证。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .conditions import CombatState
from .schema import CombatScript, Rule


@dataclass
class RuleLedger:
    """记录每条规则的触发次数与上次触发时间。"""

    fire_counts: dict[str, int] = field(default_factory=dict)
    last_fired: dict[str, float] = field(default_factory=dict)

    def record(self, rule_name: str, now: float):
        self.fire_counts[rule_name] = self.fire_counts.get(rule_name, 0) + 1
        self.last_fired[rule_name] = now

    def count(self, rule_name: str) -> int:
        return self.fire_counts.get(rule_name, 0)

    def is_cooling(self, rule: Rule, now: float) -> bool:
        """规则是否还在冷却中。"""
        if rule.cooldown <= 0:
            return False
        last = self.last_fired.get(rule.name)
        if last is None:
            return False
        return (now - last) < rule.cooldown

    def remaining_cooldown(self, rule: Rule, now: float) -> float:
        if rule.cooldown <= 0:
            return 0.0
        last = self.last_fired.get(rule.name)
        if last is None:
            return 0.0
        return max(0.0, rule.cooldown - (now - last))

    def reset(self):
        self.fire_counts.clear()
        self.last_fired.clear()


# 决策原因，供日志与测试断言使用
REASON_RULE = "rule"
REASON_FALLBACK = "fallback"
REASON_IDLE = "idle"


@dataclass(frozen=True)
class Decision:
    """一次决策的结果。"""

    reason: str
    rule: Rule | None = None
    actions: tuple = ()

    @property
    def rule_name(self) -> str:
        return self.rule.name if self.rule is not None else ""

    def describe(self) -> str:
        if self.reason == REASON_RULE:
            return f"rule:{self.rule_name}"
        return self.reason


IDLE = Decision(reason=REASON_IDLE)


class CombatEngine:
    """按优先级选规则的决策引擎。"""

    def __init__(self, script: CombatScript, ledger: RuleLedger | None = None):
        self.script = script
        self.ledger = ledger or RuleLedger()
        self._sorted = script.sorted_rules
        self._last_fallback_at: float | None = None

    def reset(self):
        """重置运行时状态，用于开始新一场战斗。"""
        self.ledger.reset()
        self._last_fallback_at = None

    def _state_with_ledger(self, state: CombatState) -> CombatState:
        """把账本注入 state，让 every 类条件能查到上次触发时间。

        用 dataclasses.replace 生成新快照而不是原地改，保持 state 只读语义。
        """
        from dataclasses import replace

        return replace(
            state,
            rule_fire_counts=self.ledger.fire_counts,
            rule_last_fired=self.ledger.last_fired,
        )

    def eligible_rules(self, state: CombatState):
        """返回当前所有满足条件且不在冷却中的规则（按优先级）。

        单独暴露这个方法，便于测试"为什么没选中"这类问题。
        """
        bound = self._state_with_ledger(state)
        result = []
        for rule in self._sorted:
            if rule.once and self.ledger.count(rule.name) > 0:
                continue
            if self.ledger.is_cooling(rule, state.now):
                continue
            if not rule.condition.evaluate(bound):
                continue
            result.append(rule)
        return result

    def decide(self, state: CombatState) -> Decision:
        """选出本 tick 应执行的动作。

        顺序：优先级最高的可用规则 -> 兜底动作 -> 空闲。
        """
        eligible = self.eligible_rules(state)
        if eligible:
            rule = eligible[0]
            return Decision(
                reason=REASON_RULE, rule=rule, actions=rule.actions
            )

        if self.script.fallback:
            interval = self.script.fallback_interval
            if (
                interval <= 0
                or self._last_fallback_at is None
                or (state.now - self._last_fallback_at) >= interval
            ):
                return Decision(
                    reason=REASON_FALLBACK, actions=self.script.fallback
                )

        return IDLE

    def commit(self, decision: Decision, now: float):
        """记录一次决策已被执行。

        引擎不自动记账，由调用方在**动作真正执行后**调用，
        避免"决定了但没执行成功"却占用冷却。
        """
        if decision.reason == REASON_RULE and decision.rule is not None:
            self.ledger.record(decision.rule.name, now)
        elif decision.reason == REASON_FALLBACK:
            self._last_fallback_at = now

    def explain(self, state: CombatState) -> list[str]:
        """诊断用：解释每条规则当前为何可用/不可用。

        脚本不生效是最常见的用户问题，有这个方法就能直接把原因打到日志里，
        而不是让用户去猜。
        """
        bound = self._state_with_ledger(state)
        lines = []
        for rule in self._sorted:
            if rule.once and self.ledger.count(rule.name) > 0:
                lines.append(f"{rule.name}: 跳过（once 已触发）")
                continue
            remaining = self.ledger.remaining_cooldown(rule, state.now)
            if remaining > 0:
                lines.append(f"{rule.name}: 冷却中（剩余 {remaining:.2f}s）")
                continue
            if not rule.condition.evaluate(bound):
                lines.append(
                    f"{rule.name}: 条件不满足 [{rule.condition.describe()}]"
                )
                continue
            lines.append(f"{rule.name}: 可执行")
        return lines
