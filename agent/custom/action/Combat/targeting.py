"""有限索敌：每张新图最多一个动作；暂停/未知帧不计时，不周期性切换中键。"""

from __future__ import annotations

from typing import Callable

from .keys import LOCK_BUTTON
from .model import Snapshot
from .runtime import MAX_ACCUM_STEP

RELOCK_AFTER = 0.5
RELOCK_INTERVAL = 0.6
HOLD_ULTIMATE_AFTER = 1.0
SEARCH_AFTER = 1.0
SEARCH_INTERVAL = 0.6
SEARCH_DEGREES = 90.0
SEARCH_MAX_TURNS = 4
SEARCH_EXTRA_CONFIRM = 2.0


class Targeting:
    def __init__(
        self,
        driver,
        clock: Callable[[], float],
        lock: bool = True,
        search: bool = False,
        monitor: Callable = lambda: None,
    ):
        self.driver = driver
        self.clock = clock
        self.lock = lock
        self.search = search
        self._monitor = monitor
        self.lost_since: float | None = None
        self.turns = 0
        self._elapsed = 0.0
        self._eligible = False
        self._enemy: bool | None = True
        self._last_at: float | None = None
        self._acted_at: float | None = None
        self._relocked = False
        self._last_lock = float("-inf")
        self._last_turn = float("-inf")

    def engage(self) -> None:
        self.lost_since = None
        self.turns = 0
        self._elapsed = 0.0
        self._eligible = False
        self._last_at = self._acted_at = None
        self._relocked = False
        self._last_turn = float("-inf")
        if self.lock:
            self._press_lock()

    def observe(self, snap: Snapshot) -> None:
        eligible = snap.ready and snap.hostile is False
        if eligible and self._eligible and self._last_at is not None:
            self._elapsed += max(0.0, min(snap.at - self._last_at, MAX_ACCUM_STEP))
        else:
            self._elapsed = 0.0
        self._last_at = snap.at
        self._eligible = eligible
        self._enemy = snap.hostile
        if not eligible:
            self.lost_since = None
        elif self.lost_since is None:
            self.lost_since = snap.at
        if snap.ready and snap.hostile is True:
            self.turns = 0
            self._relocked = False

    def lost_for(self) -> float:
        monitor = self._monitor()
        if not self._eligible:
            return 0.0
        return monitor.clear_accum if monitor is not None else self._elapsed

    def hold_ultimate(self) -> bool:
        return self._enemy is None or self.lost_for() >= HOLD_ULTIMATE_AFTER

    def maintain(self) -> bool:
        """返回是否执行了一个动作，调用者随后必须取得新图。"""
        if not self._eligible or self._acted_at == self._last_at:
            return False
        lost, now = self.lost_for(), self.clock()
        if (
            self.search
            and lost >= SEARCH_AFTER
            and self.turns < SEARCH_MAX_TURNS
            and now - self._last_turn >= SEARCH_INTERVAL
        ):
            self._acted_at = self._last_at
            self._last_turn = now
            self.turns += 1
            if not self.driver.turn(SEARCH_DEGREES):
                self.turns = SEARCH_MAX_TURNS
            return True
        if (
            self.lock
            and not self._relocked
            and lost >= RELOCK_AFTER
            and (not self.search or self.turns > 0)  # 先转向再锁，锁到新视野内的敌人
            and now - self._last_lock >= RELOCK_INTERVAL
        ):
            self._acted_at = self._last_at
            self._relocked = True
            self._press_lock()
            return True
        return False

    def _press_lock(self) -> None:
        self._last_lock = self.clock()
        self.driver.click(LOCK_BUTTON)
