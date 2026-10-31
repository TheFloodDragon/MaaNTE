"""可注入时钟的战斗监视器。生命周期时间使用真实秒，不受输入节奏倍率影响。"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable

from .model import (
    INVALID_SNAPSHOT,
    CombatEnded,
    CombatStopped,
    EndReason,
    InputSuspended,
    Phase,
    Snapshot,
)

SNAPSHOT_INTERVAL = 0.2
SLEEP_SLICE = 0.05
ENTER_HITS = 2
MAX_ACCUM_STEP = 1.0
OBSERVATION_TIMEOUT = 10.0


class Monitor:
    """只有正向终局证据才能确认结束；所有缺失区间均不可用于证明清场。"""

    def __init__(
        self,
        start: float,
        max_duration: float,
        clear_confirm: float,
        hud_lost_timeout: float,
        observation_timeout: float = OBSERVATION_TIMEOUT,
    ):
        self.start = start
        self.max_duration = max_duration
        self.clear_confirm = clear_confirm
        self.hud_lost_timeout = hud_lost_timeout
        self.observation_timeout = observation_timeout
        self.clear_accum = 0.0
        self.hud_missing = 0.0
        self.observation_missing = 0.0
        self.phase = Phase.ACTIVE
        self.snapshot = INVALID_SNAPSHOT
        self.retreat_pending = False
        self._last_at: float | None = None
        self._negative = False
        self._terminal: str | None = None
        self._terminal_hits = 0
        self._reason: str | None = None

    def input_allowed(self, snap: Snapshot) -> bool:
        return bool(
            snap.ready
            and snap.hostile is not None
            and (not self.retreat_pending or snap.hostile is True)
        )

    def update(self, snap: Snapshot) -> None:
        if self._last_at is not None and snap.at <= self._last_at:
            return  # 同一帧不重复累计，也不能重复确认终局
        dt = (
            0.0
            if self._last_at is None
            else min(snap.at - self._last_at, MAX_ACCUM_STEP)
        )
        previous_negative = self._negative
        self._last_at = snap.at
        self.snapshot = snap
        self._reason = None
        self._negative = False

        terminal = snap.terminal_reason
        self._terminal_hits = (
            self._terminal_hits + 1
            if terminal and terminal == self._terminal
            else int(bool(terminal))
        )
        self._terminal = terminal
        if terminal:
            self.observation_missing = self.hud_missing = 0.0
            self.phase = Phase.SUSPENDED
            if self._terminal_hits >= ENTER_HITS:
                self._reason = terminal
                self.phase = Phase.ENDED
            return

        if not snap.valid:
            self.observation_missing += dt
            self.phase = Phase.UNCERTAIN
            return
        if snap.retreat_prompt is True:
            self.retreat_pending = True
            self.observation_missing = 0.0
            self.hud_missing += dt
            self.phase = Phase.SUSPENDED
            return
        if not snap.in_hud or snap.black is not False or snap.suspended is True:
            # 撤离提示之后的黑屏仍只是转场，不能据此断言撤退已完成。
            self.observation_missing = 0.0
            self.hud_missing += dt
            self.phase = Phase.SUSPENDED
            return
        unknown = (
            snap.hostile is None
            or snap.suspended is None
            or snap.retreat_prompt is None
            or bool(snap.terminal_signals)  # 相互矛盾的终局证据
        )
        if unknown:
            self.hud_missing = 0.0
            self.observation_missing += dt
            self.phase = Phase.UNCERTAIN
            return
        self.observation_missing = 0.0
        if snap.hostile is True:
            self.hud_missing = self.clear_accum = 0.0
            self.retreat_pending = False  # 取消撤离且新图确认仍有敌人，才恢复输出
            self.phase = Phase.ACTIVE
            return
        self.hud_missing = 0.0
        if previous_negative:
            self.clear_accum += dt  # 只累计两张相邻可判断阴性图之间的时间
        self._negative = True
        if self.retreat_pending:
            # 撤离提示消失但没有敌人：不恢复输出，也没有离场证据；按目标丢失计时结束。
            self.phase = Phase.SUSPENDED
        else:
            self.phase = Phase.VERIFYING if self.clear_accum > 0 else Phase.UNCERTAIN

    def end_reason(self, now: float) -> str | None:
        if self._reason is not None:
            return self._reason
        if self.max_duration > 0 and now - self.start >= self.max_duration:
            return EndReason.TIMEOUT
        if self.observation_missing >= self.observation_timeout:
            return EndReason.OBSERVATION_LOST
        if self.hud_lost_timeout > 0 and self.hud_missing >= self.hud_lost_timeout:
            return EndReason.HUD_LOST
        if self._negative and self.clear_accum >= self.clear_confirm:
            return EndReason.TARGET_LOST
        return None

    @property
    def evidence(self) -> tuple[tuple[str, bool | None], ...]:
        snap = self.snapshot
        return snap.evidence or (
            ("hud", snap.in_hud),
            ("enemy", snap.hostile),
            ("retreat_prompt", snap.retreat_prompt),
            *(
                (key, getattr(snap, key))
                for key in ("cleared", "disengaged", "retreated", "defeated")
            ),
        )


class Runtime:
    def __init__(
        self,
        observe: Callable[[], Snapshot],
        is_stopping: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._observe = observe
        self._is_stopping = is_stopping
        self.now = clock
        self._sleeper = sleeper
        self._snap = INVALID_SNAPSHOT
        self._snap_at = float("-inf")
        self.monitor: Monitor | None = None
        self.targeting = None
        self._maintaining = False
        self._driver = None
        self._input_interrupted = False
        self.on_phase = None

    def bind_input(self, driver) -> None:
        self._driver = driver
        driver.before_input = self.before_input

    def check(self) -> None:
        if self._is_stopping():
            raise CombatStopped()
        if self.monitor is not None:
            reason = self.monitor.end_reason(self.now())
            if reason is not None and reason != EndReason.TIMEOUT:
                # 最后一帧复核；不能先根据过期快照离场，再错过刚出现的敌人。
                self.fresh()
                if self._is_stopping():
                    raise CombatStopped()
                reason = self.monitor.end_reason(self.now())
            if reason is not None:
                raise CombatEnded(reason)

    def arm(self, monitor: Monitor, targeting=None) -> None:
        self.monitor = monitor
        self.targeting = targeting
        self._input_interrupted = False
        monitor.update(self._snap)

    def disarm(self) -> None:
        self.monitor = None
        self.targeting = None
        self._input_interrupted = False

    def snapshot(self, max_age: float = SNAPSHOT_INTERVAL) -> Snapshot:
        if self.now() - self._snap_at >= max_age:
            self._snap = replace(self._observe(), at=self.now())
            self._snap_at = self._snap.at
            if self.monitor is not None:
                phase = self.monitor.phase
                self.monitor.update(self._snap)
                if (
                    not self.monitor.input_allowed(self._snap)
                    and self._driver is not None
                ):
                    if self._driver.holding:
                        self._driver.release_all()
                        self._input_interrupted = True
                if phase != self.monitor.phase and self.on_phase is not None:
                    self.on_phase(self.monitor.phase, self._snap)
            if self.targeting is not None:
                self.targeting.observe(self._snap)
        return self._snap

    def fresh(self) -> Snapshot:
        return self.snapshot(max_age=0.0)

    def usable(self, snap: Snapshot) -> bool:
        """这张图上是否允许发送新输入（已开战时还要求不在撤离确认/未知状态）。"""
        return (
            self.monitor.input_allowed(snap) if self.monitor is not None else snap.ready
        )

    def _check_interrupted(self) -> None:
        if self._input_interrupted:
            self._input_interrupted = False
            raise InputSuspended()

    def before_input(self) -> None:
        self.check()
        if self.monitor is None:
            raise RuntimeError("combat input outside an active session")
        # 复用不超过 SNAPSHOT_INTERVAL 的截图：每次按键都截图识别会让连点频率大幅下降。
        # 画面变为不可操作时，snapshot() 会立即释放按住的键，下一次输入前再拦截。
        snap = self.snapshot()
        self.check()
        self._check_interrupted()
        if not self.monitor.input_allowed(snap):
            raise InputSuspended()

    def sleep(self, seconds: float, *, polling: bool = False) -> None:
        deadline = self.now() + max(0.0, seconds)
        while True:
            self.check()
            if self.monitor is not None:
                self.snapshot()
                self.check()
                if not polling:
                    self._check_interrupted()
            remaining = deadline - self.now()
            if remaining <= 0:
                return
            self._sleeper(min(SLEEP_SLICE, remaining))

    def wait_ready(self, limit: float | None = None, maintain: bool = True) -> Snapshot:
        """只在确实能输入时返回；失明/菜单期间不以超时为理由放行攻击。

        长时间不可输入由 Monitor 的 hud_lost / observation_lost 结束，limit 只给调用方额外上限。
        """
        deadline = self.now() + limit if limit is not None else None
        while True:
            self.check()
            snap = self.snapshot()
            self.check()
            self._check_interrupted()
            usable = self.usable(snap)
            if usable:
                if not maintain:
                    return snap
                self._maintain()
                snap = self.snapshot()
                self.check()
                if self.monitor is None or self.monitor.input_allowed(snap):
                    return snap
            if deadline is not None and self.now() >= deadline:
                raise CombatEnded(
                    EndReason.OBSERVATION_LOST
                    if not snap.valid or snap.hostile is None
                    else EndReason.HUD_LOST
                )
            self.sleep(SLEEP_SLICE, polling=True)

    def _maintain(self) -> None:
        if self.targeting is None or self._maintaining:
            return
        self._maintaining = True
        try:
            if self.targeting.maintain():
                self.fresh()  # 一次索敌动作后必须重新识别，不能连做多个动作
        finally:
            self._maintaining = False

    def wait_for_enemy(self, timeout: float) -> bool:
        deadline = self.now() + timeout if timeout > 0 else None
        streak, counted_at = 0, None
        invalid_since = None
        while True:
            self.check()
            snap = self.snapshot()
            if snap.at != counted_at:
                counted_at = snap.at
                streak = streak + 1 if snap.ready and snap.hostile is True else 0
                if streak >= ENTER_HITS:
                    return True
            if not snap.valid:
                if invalid_since is None:
                    invalid_since = self.now()
                elif self.now() - invalid_since >= OBSERVATION_TIMEOUT:
                    raise CombatEnded(EndReason.OBSERVATION_LOST)
            else:
                invalid_since = None
            if deadline is not None and self.now() >= deadline:
                return False
            self.sleep(SNAPSHOT_INTERVAL, polling=True)
