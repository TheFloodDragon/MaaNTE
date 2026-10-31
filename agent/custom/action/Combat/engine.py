"""战斗引擎：等待进战 → 按方案轮转（出招 → 补足最短在场 → 切人确认）→ 结束判定。

固定配队：方案声明的配队只用于提示，不识别也不校验。超出队伍人数的槽位开战前就移出轮转；
切人失败（空槽/阵亡）的槽位本场移出轮转。切人确认依赖右侧队伍栏的当前角色高亮，
整场都识别不到时降级为站场。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from utils.logger import logger

from .executor import ComboExecutor, disabled_commands
from .inputs import InputDriver
from .keys import SLOT_KEYS
from .model import CombatEnded, CombatResult, CombatStopped, EndReason, InputSuspended
from .runtime import Monitor, Runtime
from .scheme import CombatScheme
from .targeting import SEARCH_EXTRA_CONFIRM, Targeting

SWITCH_CONFIRM = 1.2  # 切人确认窗口
SWITCH_RESEND_AFTER = 0.5  # 窗口内未确认时补按一次
SWITCH_POLL = 0.05
SWITCH_GAP = 0.5  # 两次切人按键的最小间隔
TURN_MIN = 0.1  # 每轮出招的最短耗时，防止极短的出招表空转刷键
EMPTY_TURN_ATTACK = 1.0  # 一段出招全部被跳过（禁用/暂缓终结技）时改为普攻的秒数


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

    def team_hint(self, scheme: CombatScheme) -> None:
        pass

    def scheme_error(self, exc) -> None:
        pass

    def strategy_hint(self, changes) -> None:
        """changes：api.strategy_changes 的结果，如 (("team_size", 3), ("disabled", ("ultimate",)))。"""
        pass


@dataclass
class EngineConfig:
    wait_timeout: float = 30.0
    max_duration: float = 300.0
    clear_confirm: float = 3.0
    hud_lost_timeout: float = 10.0
    team_size: int = 4
    lock_target: bool = True
    search_turn: bool = False
    disabled: frozenset = field(
        default_factory=frozenset
    )  # 禁用的动作名（model.TOGGLE_ACTIONS）
    disabled_slots: frozenset = field(default_factory=frozenset)  # 不主动切入的槽位
    speed: float = 1.0  # 输入节奏倍率，不是游戏速度


def effective_order(order, excluded) -> list[int]:
    """去掉 excluded 中的槽位；因此变得相邻（含首尾循环相邻）的同一槽位合并为一轮。

    例如主C站场 [1,2,1,3,1,4] 在 2 人队时变为 [1,2]。方案本身写的连续重复保持不变。
    """
    kept: list[int] = []
    gap = False  # 最后一个保留槽位之后是否去掉过槽位
    lead_gap = False  # 第一个保留槽位之前是否去掉过槽位
    for slot in order:
        if slot in excluded:
            gap = True
            lead_gap = lead_gap or not kept
            continue
        if kept and gap and kept[-1] == slot:
            gap = False
            continue
        kept.append(slot)
        gap = False
    if len(kept) > 1 and kept[0] == kept[-1] and (gap or lead_gap):
        kept.pop()
    return kept


class CombatEngine:
    def __init__(
        self,
        runtime: Runtime,
        driver: InputDriver,
        scheme: CombatScheme,
        config: EngineConfig,
        reporter: Reporter | None = None,
    ):
        self.rt = runtime
        self.driver = driver
        self.scheme = scheme
        self.config = config
        self.reporter = reporter or Reporter()
        runtime.bind_input(driver)
        self.excluded = frozenset(
            s for s in SLOT_KEYS if s > config.team_size
        ) | frozenset(
            config.disabled_slots
        )  # 超出队伍人数或被禁用，不主动切入
        self.order = effective_order(scheme.order, self.excluded)
        self.switch_allowed = "switch" not in config.disabled
        self.targeting = Targeting(
            driver,
            runtime.now,
            lock=config.lock_target,
            search=config.search_turn,
            monitor=lambda: runtime.monitor,
        )
        self.executor = ComboExecutor(
            runtime,
            driver,
            self._combo_switch,
            disabled=disabled_commands(config.disabled),
            hold_ultimate=self.targeting.hold_ultimate,
            speed=config.speed,
            excluded_slots=self.excluded,
        )
        self.current = 0  # 当前确认的槽位，0 = 未知
        self.unavailable: set[int] = set()
        self.degraded = False
        self._slot_seen = False
        self._last_switch = float("-inf")
        self._pos: int | None = None  # 轮转位置（order 下标）
        self._entered = False  # 本场是否已确定首发

    @property
    def rotating(self) -> bool:
        return (
            self.scheme.mode == "cycle"
            and self.switch_allowed
            and len(set(self.order)) > 1
        )

    def run(self, script=None) -> CombatResult:
        """执行一场战斗。script(session) 可选：确认进战后调用一次，接管出招。"""
        began, reason = None, EndReason.ERROR
        evidence = ()
        self.current = 0
        self.unavailable.clear()
        self.degraded = self._slot_seen = False
        self._last_switch = float("-inf")
        self._pos, self._entered = None, False
        self.executor.reset_speed()  # 脚本/出招表改过的倍率只在本场有效
        self.rt.on_phase = lambda phase, snap: self._notify(
            "state_changed", phase, snap
        )
        try:
            self.reporter.waiting(self.scheme)
            if not self.rt.wait_for_enemy(self.config.wait_timeout):
                reason = EndReason.NO_COMBAT
            else:
                began = self.rt.now()
                cfg = self.config
                confirm = cfg.clear_confirm + (
                    SEARCH_EXTRA_CONFIRM if cfg.search_turn else 0.0
                )
                self.rt.arm(
                    Monitor(began, cfg.max_duration, confirm, cfg.hud_lost_timeout),
                    self.targeting,
                )
                self.reporter.started(self.scheme)
                try:
                    self.targeting.engage()
                except InputSuspended:
                    pass  # 开场画面不可操作：不补按，交给后续 wait_ready
                if script is None:
                    self._fight()
                else:
                    reason = self._run_script(script)
        except CombatEnded as exc:
            reason = exc.reason
        except CombatStopped:
            reason = EndReason.STOPPED
        except Exception:
            logger.exception("combat engine crashed")
            reason = EndReason.ERROR
        finally:
            if self.rt.monitor is not None:
                evidence = self.rt.monitor.evidence
            self.rt.disarm()
            self.driver.release_all()
        result = CombatResult.of(
            reason,
            0.0 if began is None else self.rt.now() - began,
            entered=began is not None,
            evidence=evidence,
        )
        self.reporter.ended(result)
        return result

    def _run_script(self, script) -> str:
        from .session import CombatSession, ScriptFinished

        session = CombatSession(self)
        try:
            script(session)
        except ScriptFinished:
            pass
        finally:
            session._close()
            self.driver.release_all()
        if session.end_reason == EndReason.STOPPED:
            raise CombatStopped()
        if session.end_reason is not None:  # 脚本吞掉了结束信号
            return session.end_reason
        self.rt.fresh()  # 用脚本返回后的新截图裁决，确认的终局/停止优先于 script_finished
        self.rt.check()
        return EndReason.SCRIPT_FINISHED

    def _fight(self) -> None:
        while True:
            try:
                self.play_turn()
            except InputSuspended:
                self.rt.wait_ready()  # 当前动作已释放；恢复后重新识别，不续发剩余按键

    def play_turn(self) -> None:
        """默认轮转的一轮（自动战斗与 session.run_default 共用）。

        轮转模式：首轮先切到首发 → 当前角色出招并补足站场 → 切到下一位。
        站场模式（或无法轮转）：跟随实际角色出招。
        """
        if self.rotating and not self._entered:
            self._pos = self._enter_rotation()
            self._entered = True
        if self.rotating and self._pos is not None and not self.degraded:
            if self.order[self._pos] != self.current and self.current in self.order:
                self._pos = self.order.index(
                    self.current
                )  # 脚本或玩家手动切过人：从实际角色续接
            self._turn(self.current)
            self._pos = self._advance(self._pos)
            return
        snap = self.rt.wait_ready()
        if snap.slot:
            self._slot_seen = True
            self.current = snap.slot  # 站场跟随实际角色，禁用槽位只代表不主动切入
        self._turn(self.current)

    def _turn(self, slot: int) -> None:
        started = self.rt.now()
        if self.executor.run(self.scheme.combo_for(slot)) == 0:
            self.executor.attack(EMPTY_TURN_ATTACK)  # 整段被跳过：普攻，避免原地空转
        self._fill_field_time(started, slot)
        idle = self.executor.scaled(TURN_MIN) - (self.rt.now() - started)
        if idle > 0:
            self.rt.sleep(idle)

    def _fill_field_time(self, started: float, slot: int) -> None:
        """未满最短在场时间时用普攻补足；站场时长是真实秒，不受输入节奏影响。"""
        remaining = self.scheme.field_time_for(slot) - (self.rt.now() - started)
        if remaining > 0:
            self.executor.attack(remaining)

    def _enter_rotation(self) -> int | None:
        """首发 = 生效轮转顺序的首位；已在首发上不按键，首发切不过去时依次尝试后续槽位。"""
        snap = self.rt.fresh()
        if snap.slot:
            self._slot_seen = True
            self.current = snap.slot
        for pos, slot in enumerate(self.order):
            if slot in self.unavailable:
                continue
            if self.switch_to(slot):
                return pos
            if not self._slot_seen:  # 整个确认窗口都识别不到任何槽位：识别失效
                self._degrade()
                return None
            self._mark_unavailable(slot)
        return None  # 轮转里的槽位都切不过去，留在当前角色站场

    def _advance(self, pos: int) -> int | None:
        """前进到下一个可用槽位。跳过不可用槽位后又回到当前角色时继续往后找，避免同一角色连续两轮。"""
        order = self.order
        skipped = False
        for step in range(1, len(order) + 1):
            nxt = (pos + step) % len(order)
            slot = order[nxt]
            if slot in self.unavailable:
                skipped = True
                continue
            if slot == self.current:
                if skipped and step < len(order):
                    continue
                return nxt
            if self.switch_to(slot):
                return nxt
            self._mark_unavailable(slot)
            skipped = True
        return None  # 其他槽位都不可用，退回站场

    def _combo_switch(self, slot: int) -> bool | None:
        """出招表 switch(n)。禁用/超出队伍的槽位跳过；识别失效时只按键不确认。"""
        if slot in self.excluded or not self.switch_allowed:
            return None
        if self.degraded:
            self._press_slot(slot)
            self.current = slot
            return True
        if self.switch_to(slot):
            return True
        if not self._slot_seen:  # 从未识别到任何槽位：识别失效而非槽位不可用
            self._degrade()
            self.current = slot  # 按键已发出，继续本段
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
        """按槽位键并等待队伍栏高亮确认。

        画面可操作的时间满 SWITCH_RESEND_AFTER 仍未切换时，按新截图补按一次（游戏偶尔吞掉切人键）；
        遮挡/动画时间顺延，不计入确认窗口和补按计时。
        """
        if isinstance(slot, bool) or slot not in SLOT_KEYS:
            raise ValueError("combat slot must be an integer from 1 to 4")
        if slot == self.current:
            snap = self.rt.snapshot()
            if not snap.slot or snap.slot == slot:
                return True
            self.current = snap.slot  # 记录过期（例如阵亡后自动换人）
        if slot in self.unavailable or slot in self.excluded or not self.switch_allowed:
            return False
        previous = self.current
        sent = self._press_slot(slot)
        deadline, resend_at, resent = (
            sent + SWITCH_CONFIRM,
            sent + SWITCH_RESEND_AFTER,
            False,
        )
        while True:
            snap = self.rt.fresh()
            self.rt.check()
            if not self.rt.usable(snap):
                before = self.rt.now()
                snap = self.rt.wait_ready(maintain=False)
                paused = self.rt.now() - before
                deadline += paused
                resend_at += paused
            if snap.slot:
                self._slot_seen = True
            if snap.slot == slot:
                self.current = slot
                self._notify("switched", previous, slot)
                return True
            now = self.rt.now()
            if now >= deadline:
                if snap.slot:
                    self.current = snap.slot
                return False
            if not resent and now >= resend_at:
                resent = True
                self.driver.tap(SLOT_KEYS[slot])
                self._last_switch = self.rt.now()
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

    def _notify(self, name: str, *args) -> None:
        callback = getattr(self.reporter, name, None)
        if callable(callback):
            try:
                callback(*args)
            except Exception as exc:  # 用户消息失败不应中断战斗
                logger.warning("combat reporter %s failed: %s", name, exc)
