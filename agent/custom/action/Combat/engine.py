"""战斗引擎：等待进战 → 按方案轮转（出招 → 补足最短在场 → 切人确认）→ 结束判定。

固定配队：方案声明的配队只用于提示，不识别也不校验；切人失败（空槽/阵亡）的槽位
本场移出轮转。切人确认依赖右侧队伍栏的当前角色高亮，整场都识别不到时降级为站场。
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.logger import logger

from .executor import ComboExecutor
from .inputs import InputDriver
from .keys import SLOT_KEYS
from .model import CombatEnded, CombatResult, CombatStopped, EndReason
from .runtime import Monitor, Runtime
from .scheme import CombatScheme

SWITCH_CONFIRM = 1.2  # 切人确认窗口
SWITCH_RESEND_AFTER = 0.5  # 窗口内未确认时补按一次
SWITCH_POLL = 0.05
SWITCH_GAP = 0.5  # 两次切人按键的最小间隔
TURN_MIN = 0.1  # 每轮出招的最短耗时，防止极短的出招表空转刷键


class Reporter:
    """用户可见消息接口，默认什么也不做。api.PrintReporter 负责 i18n 输出。"""

    def waiting(self, scheme: CombatScheme) -> None:
        pass

    def started(self, scheme: CombatScheme) -> None:
        pass

    def ended(self, result: CombatResult) -> None:
        pass

    def slot_unavailable(self, slot: int) -> None:
        pass

    def degraded(self) -> None:
        pass


@dataclass
class EngineConfig:
    wait_timeout: float = 30.0
    max_duration: float = 300.0
    clear_confirm: float = 3.0
    hud_lost_timeout: float = 10.0


class CombatEngine:
    def __init__(self, runtime: Runtime, driver: InputDriver, scheme: CombatScheme, config: EngineConfig, reporter: Reporter | None = None):
        self.rt = runtime
        self.driver = driver
        self.scheme = scheme
        self.config = config
        self.reporter = reporter or Reporter()
        self.executor = ComboExecutor(runtime, driver, self._combo_switch)
        self.current = 0  # 当前确认的槽位，0 = 未知
        self.unavailable: set[int] = set()
        self.degraded = False
        self._slot_seen = False
        self._last_switch = float("-inf")

    # ---- 主流程 ----
    def run(self) -> CombatResult:
        began = None
        reason = EndReason.ERROR
        try:
            self.reporter.waiting(self.scheme)
            if not self.rt.wait_for_enemy(self.config.wait_timeout):
                reason = EndReason.NO_COMBAT
            else:
                began = self.rt.now()
                cfg = self.config
                self.rt.arm(Monitor(began, cfg.max_duration, cfg.clear_confirm, cfg.hud_lost_timeout))
                self.reporter.started(self.scheme)
                self._fight()
        except CombatEnded as exc:
            reason = exc.reason
        except CombatStopped:
            reason = EndReason.STOPPED
        except Exception:
            logger.exception("combat engine crashed")
            reason = EndReason.ERROR
        finally:
            self.rt.disarm()
            self.driver.release_all()
        result = CombatResult.of(reason, 0.0 if began is None else self.rt.now() - began)
        self.reporter.ended(result)
        return result

    def _fight(self) -> None:
        scheme = self.scheme
        if scheme.mode == "cycle" and len(set(scheme.order)) > 1:
            pos = self._enter_rotation()
            while pos is not None and not self.degraded:
                self._turn(self.current)
                pos = self._advance(pos)
        while True:  # 站场：反复执行当前角色的出招，直到结束信号抛出
            snap = self.rt.snapshot()
            if snap.slot:
                self._slot_seen = True
                self.current = snap.slot  # 用户手动切人时跟随，执行对应槽位的出招
            self._turn(self.current)

    def _turn(self, slot: int) -> None:
        started = self.rt.now()
        self.executor.run(self.scheme.combo_for(slot))
        self._fill_field_time(started)
        idle = TURN_MIN - (self.rt.now() - started)
        if idle > 0:
            self.rt.sleep(idle)

    def _fill_field_time(self, started: float) -> None:
        """未满最短在场时间时用普攻补足，避免切人过快被游戏忽略。"""
        remaining = self.scheme.min_field_time - (self.rt.now() - started)
        if remaining > 0:
            self.rt.wait_ready()
            self.driver.click_for("left", remaining, self.rt.now)

    # ---- 轮转 ----
    def _enter_rotation(self) -> int | None:
        order = self.scheme.order
        snap = self.rt.fresh()
        if snap.slot:
            self._slot_seen = True
            self.current = snap.slot
            if snap.slot in order:
                return order.index(snap.slot)
        for pos, slot in enumerate(order):
            if slot in self.unavailable:
                continue
            if self.switch_to(slot):
                return pos
            if not self._slot_seen:  # 整个确认窗口都识别不到任何槽位：识别失效
                self._degrade()
                return None
            self._mark_unavailable(slot)
        self._degrade()
        return None

    def _advance(self, pos: int) -> int | None:
        order = self.scheme.order
        for step in range(1, len(order) + 1):
            nxt = (pos + step) % len(order)
            slot = order[nxt]
            if slot in self.unavailable:
                continue
            if slot == self.current:
                return nxt
            if self.switch_to(slot):
                return nxt
            self._mark_unavailable(slot)
        return None  # 其他槽位都不可用，退回站场

    def _combo_switch(self, slot: int) -> bool:
        """出招表 switch(n)。识别失效时只按键不确认；确认失败的槽位本场移出，之后立即失败不再等待。"""
        if self.degraded:
            self._press_slot(slot)
            self.current = slot
            return True
        if self.switch_to(slot):
            return True
        if not self._slot_seen:  # 从未识别到任何槽位：是识别失效而不是槽位不可用
            self._degrade()
            self.current = slot  # 按键已发出，按已切换处理，继续本段出招
            return True
        self._mark_unavailable(slot)
        return False

    def _press_slot(self, slot: int) -> float:
        self.rt.wait_ready()
        since_last = self.rt.now() - self._last_switch
        if since_last < SWITCH_GAP:  # 连续切人时保证最小间隔，避免游戏忽略按键
            self.rt.sleep(SWITCH_GAP - since_last)
        self.driver.tap(SLOT_KEYS[slot])
        self._last_switch = self.rt.now()
        return self._last_switch

    def switch_to(self, slot: int) -> bool:
        """按槽位键并等待队伍栏高亮确认；窗口内补按一次。"""
        if slot == self.current:
            snap = self.rt.snapshot()
            if not snap.slot or snap.slot == slot:
                return True
            self.current = snap.slot  # 记录过期（例如阵亡后游戏自动换人），按实际情况重新切
        if slot in self.unavailable:
            return False
        key = SLOT_KEYS[slot]
        sent = self._press_slot(slot)
        deadline, resent = sent + SWITCH_CONFIRM, False
        while True:
            snap = self.rt.fresh()
            if snap.slot:
                self._slot_seen = True
            if snap.ready and snap.slot == slot:
                self.current = slot
                return True
            now = self.rt.now()
            if snap.valid and snap.black:
                deadline = max(deadline, now + 0.5)  # 黑屏/加载：顺延，长时间不恢复由监视器判 hud_lost
            if now >= deadline:
                if snap.slot:
                    self.current = snap.slot
                return False
            if not resent and now - sent >= SWITCH_RESEND_AFTER:
                resent = True
                self.driver.tap(key)
            self.rt.sleep(SWITCH_POLL)

    def _mark_unavailable(self, slot: int) -> None:
        if slot not in self.unavailable:
            self.unavailable.add(slot)
            logger.debug("combat slot %d removed from rotation", slot)
            self.reporter.slot_unavailable(slot)

    def _degrade(self) -> None:
        if not self.degraded:
            self.degraded = True
            self.reporter.degraded()
