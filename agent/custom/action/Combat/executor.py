"""出招表执行器：把 dsl.Command 映射为输入原语。

- 每条指令执行前等待 HUD 可见（覆盖 Q 动画、入场动画、加载、弹窗），锁敌维护也在这里插入。
- 连点类指令（l_click(秒) / r_click(秒) / 补足在场时间）每次点击前都等 HUD，
  等待的时间顺延、不计入时长，HUD 被遮挡时不会盲点。
- 战斗策略禁用的动作直接跳过；敌人丢失一段时间时暂缓终结技。
- 一段出招结束或被打断时释放所有仍按住的键（未配对的 keydown/mousedown 不会残留）。
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

from utils.logger import logger

from .dsl import CONTROL_COMMANDS, Command
from .inputs import InputDriver
from .keys import (
    ARC_KEY,
    CLICK_INTERVAL,
    DODGE_KEY,
    JUMP_KEY,
    SKILL_KEY,
    SLOT_KEYS,
    SLOW_COMMAND_GAP,
    SPEED_MAX,
    SPEED_MIN,
    TAP_SECONDS,
    ULTIMATE_KEY,
)
from .model import ActionResult, SwitchFailed
from .runtime import SLEEP_SLICE, Runtime

# 按 Q 后观察 HUD 是否消失（终结技动画开始）的窗口；消失后由下一条指令前的 wait_ready 等动画结束
ULTIMATE_SETTLE = 0.4

# 战斗策略可禁用的动作（model.TOGGLE_ACTIONS）→ 对应的出招表指令。
# sprint/walk/普攻是移动与基础攻击，不受禁用影响。
ACTION_COMMANDS: dict[str, frozenset[str]] = {
    "ultimate": frozenset({"ultimate"}),
    "skill": frozenset({"skill"}),
    "arc": frozenset({"arc"}),
    "dodge": frozenset({"dodge"}),
    "jump": frozenset({"jump"}),
    "switch": frozenset({"switch"}),
}
# 同一动作的原始按键写法（keydown(q) / keypress(1) 等）同样跳过，禁用不能被绕开。
# keyup 始终放行：松键永远安全。
ACTION_KEYS: dict[str, frozenset[str]] = {
    "ultimate": frozenset({ULTIMATE_KEY}),
    "skill": frozenset({SKILL_KEY}),
    "arc": frozenset({ARC_KEY}),
    "dodge": frozenset({DODGE_KEY, "shift"}),
    "jump": frozenset({JUMP_KEY}),
    "switch": frozenset(SLOT_KEYS.values()),
}
RAW_PRESS_COMMANDS = frozenset({"keydown", "keypress"})


def validate_speed(value) -> float:
    """输入节奏倍率：0.5~2.0。拒绝布尔、NaN、Inf 与越界值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("combat speed must be a number")
    factor = float(value)
    if not math.isfinite(factor) or not SPEED_MIN <= factor <= SPEED_MAX:
        raise ValueError(
            "combat speed must be between %.1f and %.1f" % (SPEED_MIN, SPEED_MAX)
        )
    return factor


def disabled_commands(actions) -> frozenset[str]:
    """把禁用的动作名展开成出招表指令名集合，未知动作忽略。"""
    return frozenset().union(
        *(ACTION_COMMANDS[a] for a in actions if a in ACTION_COMMANDS)
    )


class ComboExecutor:
    def __init__(
        self,
        runtime: Runtime,
        driver: InputDriver,
        switch_to: Callable[[int], bool | None],
        disabled: frozenset[str] = frozenset(),
        hold_ultimate: Callable[[], bool] = lambda: False,
        speed: float = 1.0,
        excluded_slots: frozenset[int] = frozenset(),
    ):
        self.rt = runtime
        self.driver = driver
        self._switch_to = switch_to
        self.disabled = frozenset(disabled)
        # 原始按键不能绕过禁用：禁用动作的键 + 不参与的槽位（禁用/超出队伍）的切人键
        self.disabled_keys = frozenset().union(
            *(ACTION_KEYS[name] for name in self.disabled if name in ACTION_KEYS)
        ) | frozenset(SLOT_KEYS[s] for s in excluded_slots if s in SLOT_KEYS)
        self._hold_ultimate = hold_ultimate
        self.base_speed = validate_speed(speed)
        self.speed = self.base_speed

    def reset_speed(self) -> None:
        self.speed = self.base_speed

    def set_speed(self, factor: float) -> float:
        self.speed = validate_speed(factor)
        return self.speed

    def scaled(self, seconds: float) -> float:
        """默认节奏间隔按倍率缩放；显式时长和安全窗口不走这里。"""
        return seconds / self.speed

    def command_gap(self) -> float:
        return max(0.0, 1.0 / self.speed - 1.0) * SLOW_COMMAND_GAP

    def run(self, commands: Sequence[Command]) -> int:
        """执行一段出招，返回实际发送输入的指令数（跳过和控制指令不算）。"""
        return self.run_detailed(commands).executed

    def run_detailed(self, commands: Sequence[Command]) -> ActionResult:
        executed = skipped = controls = 0
        status = "sent"
        try:
            for command in commands:
                self.rt.check()
                if command.name in CONTROL_COMMANDS:
                    self.set_speed(command.args[0])
                    controls += 1
                    continue
                if self._blocked(command):
                    skipped += 1
                    continue
                self.rt.wait_ready()
                if command.name == "ultimate" and self._hold_ultimate():
                    logger.debug("combat ultimate held: enemy lost")
                    skipped += 1
                    continue
                if self._dispatch(command) is None:
                    skipped += 1
                    continue
                executed += 1
                gap = self.command_gap()
                if gap > 0:
                    self.rt.sleep(gap)
        except SwitchFailed as exc:
            logger.debug("combo aborted: switch(%d) not confirmed", exc.slot)
            status = "switch_failed"
        finally:
            if self.driver.holding:
                self.driver.release_all()
        if status == "sent" and executed == 0:
            status = "configured" if controls and not skipped else "skipped"
        return ActionResult(status, executed, skipped, controls)

    def _blocked(self, command: Command) -> bool:
        """被战斗策略禁用：语义指令本身，或者用原始按键写出的同一动作。"""
        if command.name in self.disabled:
            return True
        return (
            command.name in RAW_PRESS_COMMANDS and command.args[0] in self.disabled_keys
        )

    def attack(self, seconds: float) -> None:
        """普攻 seconds 秒（真实时长，只在 HUD 可见时点击）；点击密度受输入节奏影响。"""
        self.repeat_active(seconds, lambda: self.driver.click("left"))

    def repeat_active(
        self,
        seconds: float,
        action: Callable[[], object],
        interval: float | None = None,
    ) -> None:
        """在 seconds 秒的「HUD 可见时间」内重复 action；每次之前等 HUD，等待时间顺延。"""
        step = self.scaled(CLICK_INTERVAL) if interval is None else interval
        deadline = self.rt.now() + seconds
        while True:
            before = self.rt.now()
            self.rt.wait_ready()
            deadline += self.rt.now() - before
            action()
            remaining = deadline - self.rt.now()
            if remaining <= 0:
                return
            self.rt.sleep(min(step, remaining))

    def _dispatch(self, cmd: Command) -> None:
        d, a, name = self.driver, cmd.args, cmd.name
        if name == "skill":
            d.tap(SKILL_KEY, a[0] or TAP_SECONDS)
        elif name == "ultimate":
            d.tap(ULTIMATE_KEY)
            self._settle_ultimate()
        elif name == "arc":
            d.tap(ARC_KEY)
        elif name in ("l_click", "r_click"):
            button = "left" if name == "l_click" else "right"
            if a[0] > 0:
                self.repeat_active(a[0], lambda: d.click(button))
            else:
                d.click(button)
        elif name in ("l_hold", "r_hold"):
            d.hold_mouse("left" if name == "l_hold" else "right", a[0])
        elif name == "wait":
            self.rt.sleep(a[0])
        elif name == "jump":
            d.tap(JUMP_KEY)
        elif name == "dodge":
            d.dodge(a[0])
        elif name == "sprint":
            d.sprint(a[0])
        elif name == "walk":
            d.hold_keys(tuple(a[0]), a[1])
        elif name == "lock":
            d.click("middle")
        elif name == "turn":
            d.turn(a[0])
        elif name == "switch":
            switched = self._switch_to(a[0])
            if switched is None:
                return None
            if not switched:
                raise SwitchFailed(a[0])
        elif name == "keydown":
            d.key_down(a[0])
        elif name == "keyup":
            d.key_up(a[0])
        elif name == "keypress":
            d.tap(a[0])
        elif name == "mousedown":
            d.mouse_down(a[0])
        elif name == "mouseup":
            d.mouse_up(a[0])
        elif name == "click":
            d.click(a[0])
        else:  # 编译器已保证不会出现
            raise ValueError("unknown combat command: %s" % name)
        return True

    def _settle_ultimate(self) -> None:
        """Q 释放成功时 HUD 会消失；看到消失就返回，交给下一条指令前的 wait_ready 等待动画结束。"""
        deadline = self.rt.now() + ULTIMATE_SETTLE
        while self.rt.now() < deadline:
            snap = self.rt.fresh()
            if snap.valid and not snap.ready:
                return
            self.rt.sleep(SLEEP_SLICE)
