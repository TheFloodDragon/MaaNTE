"""锁敌与索敌（纯逻辑，不依赖 maa，可单测）。

- 开战时按一次中键锁定目标。
- 敌人血条连续消失 RELOCK_AFTER 秒后，每 RELOCK_INTERVAL 秒按一次中键重新锁定。
- 消失 HOLD_ULTIMATE_AFTER 秒后暂缓终结技，避免把 Q 放空（与锁敌开关无关）。
- 可选：消失 SEARCH_AFTER 秒后每 SEARCH_INTERVAL 秒向右转 SEARCH_DEGREES 度找敌人，
  每次丢失最多转 SEARCH_MAX_TURNS 次（一圈）。

observe() 接收每一帧快照；maintain() 只由 Runtime.wait_ready 在确认 HUD 可见后、
两条输入之间调用，因此不会打断正在按住的键。丢失计时按真实时间。
"""

from __future__ import annotations

from typing import Callable

from .keys import LOCK_BUTTON
from .model import Snapshot

RELOCK_AFTER = 0.5
RELOCK_INTERVAL = 0.6
HOLD_ULTIMATE_AFTER = 1.0
SEARCH_AFTER = 1.0
SEARCH_INTERVAL = 0.6
SEARCH_DEGREES = 90.0
SEARCH_MAX_TURNS = 4
# 开启索敌时脱战确认额外延长的秒数，给转一圈留出时间
SEARCH_EXTRA_CONFIRM = 2.0


class Targeting:
    def __init__(
        self,
        driver,
        clock: Callable[[], float],
        lock: bool = True,
        search: bool = False,
    ):
        self.driver = driver
        self.clock = clock
        self.lock = lock
        self.search = search
        self.lost_since: float | None = None
        self.turns = 0  # 本次丢失已转动的次数
        self._last_lock = float("-inf")
        self._last_turn = float("-inf")

    def engage(self) -> None:
        """进入战斗：重置丢失状态；开启锁敌时按一次中键。"""
        self.lost_since = None
        self.turns = 0
        self._last_turn = float("-inf")
        if self.lock:
            self._press_lock()

    def observe(self, snap: Snapshot) -> None:
        """只看 HUD 可见的帧：见到敌人清除丢失状态，确认没有敌人才开始计时，识别失败（None）不改变状态。"""
        if not snap.ready:
            return
        if snap.enemy is True:
            self.lost_since = None
            self.turns = 0
        elif snap.enemy is False and self.lost_since is None:
            self.lost_since = snap.at

    def lost_for(self) -> float:
        if self.lost_since is None:
            return 0.0
        return max(0.0, self.clock() - self.lost_since)

    def hold_ultimate(self) -> bool:
        """敌人消失一段时间后暂缓终结技。"""
        return self.lost_for() >= HOLD_ULTIMATE_AFTER

    def maintain(self) -> None:
        """在两条输入之间调用：先转视角（可选）再重新锁定，让中键锁到转过来后看到的敌人。"""
        lost = self.lost_for()
        if lost <= 0:
            return
        now = self.clock()
        if (
            self.search
            and lost >= SEARCH_AFTER
            and self.turns < SEARCH_MAX_TURNS
            and now - self._last_turn >= SEARCH_INTERVAL
        ):
            self._last_turn = now
            self.turns += 1
            self.driver.turn(SEARCH_DEGREES)
        if (
            self.lock
            and lost >= RELOCK_AFTER
            and self.clock() - self._last_lock >= RELOCK_INTERVAL
        ):
            self._press_lock()

    def _press_lock(self) -> None:
        self._last_lock = self.clock()
        self.driver.click(LOCK_BUTTON)
