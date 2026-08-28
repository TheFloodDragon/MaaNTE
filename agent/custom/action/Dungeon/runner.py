"""刷本循环：传送 -> 进入 -> 前进开战 -> 战斗 -> 定位出口 -> 领奖 -> 下一轮。

## 依赖注入

所有外部能力（步骤执行、传送、战斗、时钟）都通过构造注入，因此可以完全
离线跑闭环仿真：验证"第 2 轮失败后是否走恢复流程"不需要游戏。这与
``Combat`` 的做法一致。

## 一轮的顺序

1. ``entry``   —— 从大世界走到副本列表并选中目标副本
2. ``confirm`` —— 确认进入（体力/次数消耗弹窗）
3. ``advance`` —— 进副本后前进，直到遇敌（副本里敌人在远处，站着不动不会开打）
4. 战斗        —— 交给 AutoCombat
5. ``settle``  —— 等战斗结束标志（可选；没配则以战斗自身结束为准）
6. ``locate``  —— 定位出口/奖励点（出口位置不固定，靠探测循环找）
7. ``reward``  —— 领奖
8. ``exit``    —— 退出副本或「再次挑战」，回到可再次进入的状态

``advance`` 与 ``locate`` 是可选阶段：没配就跳过，行为与旧配置完全一致。

## 两种轮次衔接方式

``loop_mode`` 决定下一轮从哪开始：

- ``reenter``（默认）：exit 退回大世界，每轮都完整走 entry+confirm。
- ``again``：exit 点「再次挑战」让副本原地重开，下一轮直接从 advance 开始。
  省掉走路和选副本，但**只在上一轮成功时**才跳过 entry——一旦某轮失败，
  当前停在哪个界面就不确定了，必须回到完整流程重新定位。

## 失败处理

任一阶段失败时走 ``recover`` 阶段（用户配置，通常是「按 ESC 回大世界」），
然后累计失败次数。连续失败达到 ``max_failures`` 就停止——继续跑只会在
错误的界面上反复乱点。

失败计数是**累计**而非连续：刷本中偶发失败若总量过大，说明配置或环境有
问题，应当停下来让用户看，而不是用无限重试掩盖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

try:
    from agent.custom.action.Combat.kernel.errors import (
        AbortException,
        TaskerStoppedException,
    )
    from agent.custom.action.Dungeon.config import (
        LOOP_MODE_AGAIN,
        PHASE_ADVANCE,
        PHASE_CONFIRM,
        PHASE_ENTRY,
        PHASE_EXIT,
        PHASE_LOCATE,
        PHASE_RECOVER,
        PHASE_REWARD,
    )
except ImportError:  # pragma: no cover
    from ..Combat.kernel.errors import AbortException, TaskerStoppedException
    from .config import (
        LOOP_MODE_AGAIN,
        PHASE_ADVANCE,
        PHASE_CONFIRM,
        PHASE_ENTRY,
        PHASE_EXIT,
        PHASE_LOCATE,
        PHASE_RECOVER,
        PHASE_REWARD,
    )

# 结束原因
REASON_DONE = "done"
REASON_STOPPED = "stopped"
REASON_TOO_MANY_FAILURES = "too_many_failures"
REASON_ABORTED = "aborted"
REASON_ERROR = "error"
REASON_TELEPORT_FAILED = "teleport_failed"
REASON_CONFIG_INVALID = "config_invalid"

# 轮次结果
ROUND_OK = "ok"
ROUND_ENTRY_FAILED = "entry_failed"
ROUND_CONFIRM_FAILED = "confirm_failed"
ROUND_ADVANCE_FAILED = "advance_failed"
ROUND_COMBAT_FAILED = "combat_failed"
ROUND_SETTLE_TIMEOUT = "settle_timeout"
ROUND_LOCATE_FAILED = "locate_failed"
ROUND_REWARD_FAILED = "reward_failed"
ROUND_EXIT_FAILED = "exit_failed"

# 阶段 -> 失败原因
_PHASE_FAILURE = {
    PHASE_ENTRY: ROUND_ENTRY_FAILED,
    PHASE_CONFIRM: ROUND_CONFIRM_FAILED,
    PHASE_ADVANCE: ROUND_ADVANCE_FAILED,
    PHASE_LOCATE: ROUND_LOCATE_FAILED,
    PHASE_REWARD: ROUND_REWARD_FAILED,
    PHASE_EXIT: ROUND_EXIT_FAILED,
}

SETTLE_POLL_INTERVAL = 1.0


@dataclass
class FarmResult:
    """整个刷本任务的结果摘要。"""

    success: bool
    reason: str
    rounds_done: int = 0
    rounds_failed: int = 0
    elapsed: float = 0.0
    round_reasons: dict = field(default_factory=dict)

    def describe(self) -> str:
        detail = ", ".join(
            f"{k}x{v}" for k, v in sorted(self.round_reasons.items())
        )
        return (
            f"reason={self.reason} 成功={self.rounds_done} "
            f"失败={self.rounds_failed} 耗时={self.elapsed:.1f}s [{detail}]"
        )


class DungeonFarmRunner:
    """驱动刷本循环。

    参数
    ----
    executor
        ``StepExecutor``，需提供 ``run_step(step) -> bool`` 与
        ``wait_recognize(step)``。
    config
        ``DungeonConfig``。
    teleport
        ``() -> bool``，执行传送；``None`` 表示跳过传送。
    combat
        ``() -> bool``，执行一场战斗。
    log
        ``(str) -> None``。
    """

    def __init__(
        self,
        executor,
        config,
        teleport=None,
        combat=None,
        log=None,
        clock=time.monotonic,
        sleep=None,
        raise_if_stopped=None,
    ):
        self.executor = executor
        self.config = config
        self._teleport = teleport
        self._combat = combat
        self._log = log or (lambda *a: None)
        self._clock = clock
        self._sleep = sleep or (lambda d: None)
        self._raise_if_stopped = raise_if_stopped or (lambda: None)

    # ------------------------------------------------------------------
    # 阶段执行
    # ------------------------------------------------------------------

    def run_phase(self, phase: str) -> bool:
        """按顺序执行一个阶段的所有步骤。

        ``required=False`` 的步骤失败只记日志不中断——它们表示"可能出现的
        弹窗"这类可选环节。
        """
        for index, step in enumerate(self.config.steps(phase)):
            self._raise_if_stopped()
            ok = self.executor.run_step(step)
            if ok:
                continue
            if step.required:
                self._log(
                    f"[{phase}] 第 {index + 1} 步失败: {step.describe()}"
                )
                return False
            self._log(
                f"[{phase}] 可选步骤未命中，跳过: {step.describe()}"
            )
        return True

    def wait_settle(self) -> bool:
        """等结算画面。没配 settle 时视为成功（以战斗自身结束为准）。"""
        settle = self.config.settle
        if settle is None:
            return True

        deadline = self._clock() + self.config.settle_timeout
        while True:
            self._raise_if_stopped()
            try:
                box = self.executor.recognize(settle)
            except Exception as exc:
                self._log(f"结算识别异常: {exc}")
                box = None
            if box is not None:
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(SETTLE_POLL_INTERVAL)

    def run_round(self, index: int, skip_entry: bool = False) -> str:
        """跑一轮，返回 ``ROUND_*``。

        ``skip_entry`` 用于「再次挑战」循环：上一轮结束时副本已经原地重开，
        角色就站在副本里，再去走 entry（找入口、选副本）只会点空。
        """
        if skip_entry:
            self._log("上一轮以「再次挑战」重开，跳过 entry/confirm")
        else:
            if not self.run_phase(PHASE_ENTRY):
                return ROUND_ENTRY_FAILED
            if not self.run_phase(PHASE_CONFIRM):
                return ROUND_CONFIRM_FAILED

        # 前进开战。放在战斗之前而不是塞进 confirm，是因为它的失败含义不同：
        # confirm 失败是「没进副本」，advance 失败是「进了副本但没找到敌人」，
        # 前者要重新点进入，后者说明地形/朝向不对，混在一起就没法定位问题。
        if not self.run_phase(PHASE_ADVANCE):
            return ROUND_ADVANCE_FAILED

        if self._combat is not None:
            self._raise_if_stopped()
            if not self._combat():
                return ROUND_COMBAT_FAILED

        if not self.wait_settle():
            return ROUND_SETTLE_TIMEOUT
        if not self.run_phase(PHASE_LOCATE):
            return ROUND_LOCATE_FAILED
        if not self.run_phase(PHASE_REWARD):
            return ROUND_REWARD_FAILED
        if not self.run_phase(PHASE_EXIT):
            return ROUND_EXIT_FAILED
        return ROUND_OK

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> FarmResult:
        started = self._clock()
        done = 0
        failed = 0
        reasons: dict[str, int] = {}

        def result(success, reason):
            return FarmResult(
                success=success,
                reason=reason,
                rounds_done=done,
                rounds_failed=failed,
                elapsed=self._clock() - started,
                round_reasons=reasons,
            )

        if not self.config.ok:
            for item in self.config.errors:
                self._log(f"[配置错误] {item}")
            return result(False, REASON_CONFIG_INVALID)

        for item in self.config.issues:
            self._log(f"[配置提醒] {item}")

        try:
            # 传送只做一次：副本入口通常不会因为刷一轮就变位置
            if self._teleport is not None:
                self._log("开始传送到副本入口")
                if not self._teleport():
                    return result(False, REASON_TELEPORT_FAILED)

            unlimited = self.config.rounds == 0
            index = 0
            skip_entry = False
            while unlimited or index < self.config.rounds:
                self._raise_if_stopped()
                index += 1
                total = "∞" if unlimited else str(self.config.rounds)
                self._log(f"--- 第 {index}/{total} 轮 ---")

                reason = self.run_round(index, skip_entry=skip_entry)
                reasons[reason] = reasons.get(reason, 0) + 1

                if reason == ROUND_OK:
                    done += 1
                    # 只有成功结束的一轮才敢断定「人还在副本里」。
                    skip_entry = self.config.loop_mode == LOOP_MODE_AGAIN
                    self._log(f"第 {index} 轮完成")
                    continue

                # 失败后当前界面不确定：下一轮必须重新走完整入口流程。
                skip_entry = False
                failed += 1
                self._log(f"第 {index} 轮失败: {reason}")

                if failed >= self.config.max_failures:
                    self._log(
                        f"累计失败 {failed} 次，达到上限 "
                        f"{self.config.max_failures}，停止刷本"
                    )
                    return result(done > 0, REASON_TOO_MANY_FAILURES)

                # 失败后尝试回到可重试的状态
                if self.config.steps(PHASE_RECOVER):
                    self._log("执行恢复流程")
                    if not self.run_phase(PHASE_RECOVER):
                        self._log("恢复流程失败，停止刷本")
                        return result(done > 0, REASON_TOO_MANY_FAILURES)

        except TaskerStoppedException:
            self._log("任务被停止")
            return result(False, REASON_STOPPED)
        except AbortException as exc:
            self._log(f"刷本中止: {exc}")
            return result(False, REASON_ABORTED)
        except Exception as exc:
            self._log(f"刷本异常: {exc}")
            return result(False, REASON_ERROR)

        return result(done > 0, REASON_DONE)
