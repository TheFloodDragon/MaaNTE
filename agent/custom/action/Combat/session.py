"""脚本战斗会话：run_combat(context, script=fn) 确认进战后调用 fn(session) 一次。

会话与默认轮转共用引擎、监视器与输入驱动，所有动作同样经过停止检查、HUD/场景门控、
战斗策略禁用与松键清理。返回的 ActionResult.sent 只表示控制器接受了输入，
不代表技能真的放出或命中（项目暂无可用性识别）。

    def my_script(session):
        session.switch(2)
        session.perform("skill, ultimate, l_click(1)")
        session.set_speed(0.8)
        while session.state != "ended":
            session.run_default(turns=1)
"""

from __future__ import annotations

from typing import Sequence

from .dsl import Command, compile_combo
from .keys import SLOT_KEYS
from .model import (
    ActionResult,
    CombatEnded,
    CombatStopped,
    EndReason,
    InputSuspended,
    Phase,
    Snapshot,
)


class ScriptFinished(Exception):
    """session.stop() 抛出，用于从脚本深处直接结束。"""


class CombatSession:
    def __init__(self, engine):
        self._engine = engine
        self._closed = False
        self.end_reason: str | None = (
            None  # 会话内观察到的结束原因（被脚本吞掉时由引擎采用）
        )

    # ---- 只读状态 ----
    @property
    def state(self) -> str:
        if self._closed or self.end_reason is not None:
            return Phase.ENDED
        monitor = self._engine.rt.monitor
        return monitor.phase if monitor is not None else Phase.ENDED

    @property
    def snapshot(self) -> Snapshot:
        return self._engine.rt.snapshot()

    @property
    def current_slot(self) -> int:
        return self._engine.current

    @property
    def unavailable_slots(self) -> frozenset[int]:
        return frozenset(self._engine.unavailable | self._engine.excluded)

    @property
    def elapsed(self) -> float:
        monitor = self._engine.rt.monitor
        return 0.0 if monitor is None else self._engine.rt.now() - monitor.start

    @property
    def speed(self) -> float:
        return self._engine.executor.speed

    @property
    def evidence(self) -> tuple:
        monitor = self._engine.rt.monitor
        return () if monitor is None else monitor.evidence

    # ---- 控制 ----
    def set_speed(self, factor: float) -> float:
        """本场后续的输入节奏倍率（0.5~2.0）。非法值抛 ValueError。"""
        self._ensure_open()
        return self._engine.executor.set_speed(factor)

    def stop(self) -> None:
        """脚本主动结束本场；结果原因为 script_finished。"""
        self._ensure_open()
        raise ScriptFinished()

    def switch(self, slot: int) -> ActionResult:
        self._ensure_open()
        if isinstance(slot, bool) or slot not in SLOT_KEYS:
            raise ValueError("combat slot must be an integer from 1 to 4")
        return self._guard(lambda: self._switch(slot))

    def perform(self, combo, slot: int | None = None) -> ActionResult:
        """执行一段出招（DSL 文本、多行列表或 compile_combo 结果）。指定 slot 时先确认切人。"""
        self._ensure_open()
        commands = self._commands(combo)
        if slot is not None and (isinstance(slot, bool) or slot not in SLOT_KEYS):
            raise ValueError("combat slot must be an integer from 1 to 4")

        def body() -> ActionResult:
            if slot is not None:
                switched = self._switch(slot)
                if not switched.success:
                    return switched
            return self._engine.executor.run_detailed(commands)

        return self._guard(body)

    def attack(self, seconds: float = 0.0) -> ActionResult:
        return self._single("l_click(%r)" % float(seconds) if seconds else "l_click")

    def skill(self, hold: float = 0.0) -> ActionResult:
        return self._single("skill(%r)" % float(hold) if hold else "skill")

    def ultimate(self) -> ActionResult:
        return self._single("ultimate")

    def arc(self) -> ActionResult:
        return self._single("arc")

    def dodge(self, direction: str = "") -> ActionResult:
        return self._single("dodge(%s)" % direction if direction else "dodge")

    def jump(self) -> ActionResult:
        return self._single("jump")

    def walk(self, direction: str, seconds: float) -> ActionResult:
        return self._single("walk(%s, %r)" % (direction, float(seconds)))

    def wait(self, seconds: float) -> ActionResult:
        return self._single("wait(%r)" % float(seconds))

    def run_default(self, turns: int = 1) -> ActionResult:
        """按当前有效方案执行 turns 轮（与自动战斗同一套轮转：首发 → 出招 → 补足站场 → 切下一位）。"""
        self._ensure_open()
        if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
            raise ValueError("turns must be a positive integer")
        done = 0

        def body() -> ActionResult:
            nonlocal done
            for _ in range(turns):
                self._engine.play_turn()
                done += 1
            return ActionResult("completed", executed=done)

        result = self._guard(body)
        if result.status == "interrupted":
            return ActionResult("interrupted", executed=done)
        return result

    # ---- 内部 ----
    def _single(self, text: str) -> ActionResult:
        return self.perform(text)

    def _commands(self, combo) -> Sequence[Command]:
        if isinstance(combo, (list, tuple)) and all(
            isinstance(c, Command) for c in combo
        ):
            if not combo:
                raise ValueError("combo is empty")
            return list(combo)
        return compile_combo(combo)  # ComboError 直接抛给脚本作者

    def _switch(self, slot: int) -> ActionResult:
        """与出招表 switch(n) 同一套规则：禁用/超出队伍跳过，识别失效时只按键不确认。"""
        engine = self._engine
        before = engine._last_switch
        outcome = engine._combo_switch(slot)
        if outcome is None:
            return ActionResult("skipped", skipped=1)
        if not outcome:
            return ActionResult("switch_failed", skipped=1)
        if engine._last_switch == before:
            return ActionResult("unchanged")  # 已是该角色，没有发送按键
        return ActionResult("sent", executed=1)

    def _guard(self, body) -> ActionResult:
        try:
            return body()
        except InputSuspended:
            return ActionResult("interrupted")
        except CombatEnded as exc:
            self.end_reason = exc.reason
            raise
        except CombatStopped:
            self.end_reason = EndReason.STOPPED
            raise

    def _ensure_open(self) -> None:
        """结束原因一经观察即锁定：脚本吞掉异常后再调用动作，仍按同一原因拒绝。"""
        if self._closed:
            raise RuntimeError("combat session has ended")
        if self.end_reason == EndReason.STOPPED:
            raise CombatStopped()
        if self.end_reason is not None:
            raise CombatEnded(self.end_reason)

    def _close(self) -> None:
        self._closed = True


__all__ = ["CombatSession", "ScriptFinished"]
