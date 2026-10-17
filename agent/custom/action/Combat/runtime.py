"""战斗运行时：时钟、停止检查、节流快照、可中断等待与结束判定。

时钟、睡眠、截图感知均可注入，引擎与执行器只依赖本模块，便于用假时钟单测。
"""

from __future__ import annotations

import time
from typing import Callable

from .model import (
    INVALID_SNAPSHOT,
    CombatEnded,
    CombatStopped,
    EndReason,
    Snapshot,
)

SNAPSHOT_INTERVAL = 0.2  # 战斗监视的截图节流间隔
SLEEP_SLICE = 0.05  # 可中断等待的最小切片
ENTER_HITS = 2  # 连续 N 帧见到敌人血条才判定进入战斗（去抖）
MAX_ACCUM_STEP = 1.0  # 单次累计上限，防止长时间无快照后一次性误判


class Monitor:
    """一场战斗内的结束判定。HUD 不可见（Q 动画、切人、加载）期间不累计脱战时间。"""

    def __init__(self, start: float, max_duration: float, clear_confirm: float, hud_lost_timeout: float):
        self.start = start
        self.max_duration = max_duration
        self.clear_confirm = clear_confirm
        self.hud_lost_timeout = hud_lost_timeout
        self.clear_accum = 0.0
        self.hud_missing = 0.0
        self._last_at: float | None = None

    def update(self, snap: Snapshot) -> None:
        if self._last_at is not None and snap.at <= self._last_at:
            return  # 同一帧不重复累计
        dt = 0.0 if self._last_at is None else min(snap.at - self._last_at, MAX_ACCUM_STEP)
        self._last_at = snap.at
        if not snap.valid:
            return
        if snap.ready:
            self.hud_missing = 0.0
            if snap.enemy is True:
                self.clear_accum = 0.0
            elif snap.enemy is False:
                self.clear_accum += dt
        else:
            self.hud_missing += dt

    def end_reason(self, now: float) -> str | None:
        if self.max_duration > 0 and now - self.start >= self.max_duration:
            return EndReason.TIMEOUT
        if self.clear_accum >= self.clear_confirm:
            return EndReason.CLEARED
        if self.hud_lost_timeout > 0 and self.hud_missing >= self.hud_lost_timeout:
            return EndReason.HUD_LOST
        return None


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
        self._snap: Snapshot = INVALID_SNAPSHOT
        self._snap_at = float("-inf")
        self.monitor: Monitor | None = None

    # ---- 停止与结束 ----
    def check(self) -> None:
        if self._is_stopping():
            raise CombatStopped()
        if self.monitor is not None:
            reason = self.monitor.end_reason(self.now())
            if reason is not None:
                raise CombatEnded(reason)

    def arm(self, monitor: Monitor) -> None:
        self.monitor = monitor

    def disarm(self) -> None:
        self.monitor = None

    # ---- 感知 ----
    def snapshot(self, max_age: float = SNAPSHOT_INTERVAL) -> Snapshot:
        now = self.now()
        if now - self._snap_at >= max_age:
            observed = self._observe()
            self._snap = Snapshot(
                at=self.now(),
                valid=observed.valid,
                in_hud=observed.in_hud,
                black=observed.black,
                slot=observed.slot,
                enemy=observed.enemy,
            )
            self._snap_at = now
            if self.monitor is not None:
                self.monitor.update(self._snap)
        return self._snap

    def fresh(self) -> Snapshot:
        return self.snapshot(max_age=0.0)

    # ---- 等待 ----
    def sleep(self, seconds: float) -> None:
        """可中断等待：分片睡眠，期间检查停止，战斗中顺带刷新监视快照。"""
        deadline = self.now() + max(0.0, seconds)
        while True:
            self.check()
            if self.monitor is not None:
                self.snapshot()
                self.check()
            remaining = deadline - self.now()
            if remaining <= 0:
                return
            self._sleeper(min(SLEEP_SLICE, remaining))

    def wait_ready(self, limit: float = 15.0) -> Snapshot:
        """等到可以接收战斗输入（HUD 可见且非黑屏）。截图失败视为可输入，避免卡死。"""
        deadline = self.now() + limit
        while True:
            self.check()
            snap = self.snapshot()
            if not snap.valid or snap.ready or self.now() >= deadline:
                return snap
            self.sleep(SLEEP_SLICE)

    def wait_for_enemy(self, timeout: float) -> bool:
        """等待进入战斗（连续 ENTER_HITS 帧见到敌人血条）。timeout<=0 表示一直等。"""
        deadline = self.now() + timeout if timeout > 0 else None
        streak, counted_at = 0, None
        while True:
            self.check()
            snap = self.snapshot()
            if snap.at != counted_at:
                counted_at = snap.at
                streak = streak + 1 if (snap.ready and snap.enemy is True) else 0
                if streak >= ENTER_HITS:
                    return True
            if deadline is not None and self.now() >= deadline:
                return False
            self.sleep(SNAPSHOT_INTERVAL)
