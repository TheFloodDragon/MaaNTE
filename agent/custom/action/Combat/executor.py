"""出招表执行器：把 dsl.Command 映射为输入原语。

每条指令执行前等待 HUD 可见（覆盖 Q 动画、入场动画、加载），一段出招结束后
释放所有仍按住的键（未配对的 keydown/mousedown 不会残留到下一段）。
"""

from __future__ import annotations

from typing import Callable, Sequence

from utils.logger import logger

from .dsl import Command
from .inputs import InputDriver
from .keys import ARC_KEY, DODGE_KEY, JUMP_KEY, SKILL_KEY, TAP_SECONDS, ULTIMATE_KEY
from .model import SwitchFailed
from .runtime import SLEEP_SLICE, Runtime

# 按 Q 后观察 HUD 是否消失（终结技动画开始）的窗口；消失后由下一条指令前的 wait_ready 等动画结束
ULTIMATE_SETTLE = 0.4


class ComboExecutor:
    def __init__(self, runtime: Runtime, driver: InputDriver, switch_to: Callable[[int], bool]):
        self.rt = runtime
        self.driver = driver
        self._switch_to = switch_to

    def run(self, commands: Sequence[Command]) -> None:
        try:
            for command in commands:
                self.rt.check()
                self.rt.wait_ready()
                self._dispatch(command)
        except SwitchFailed as exc:
            logger.debug("combo aborted: switch(%d) not confirmed", exc.slot)
        finally:
            if self.driver.holding:
                self.driver.release_all()

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
                d.click_for(button, a[0], self.rt.now)
            else:
                d.click(button)
        elif name in ("l_hold", "r_hold"):
            d.hold_mouse("left" if name == "l_hold" else "right", a[0])
        elif name == "wait":
            self.rt.sleep(a[0])
        elif name == "jump":
            d.tap(JUMP_KEY)
        elif name == "dodge":
            d.tap(DODGE_KEY)
        elif name == "walk":
            d.tap(a[0], a[1])
        elif name == "switch":
            if not self._switch_to(a[0]):
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

    def _settle_ultimate(self) -> None:
        """Q 释放成功时 HUD 会消失；看到消失就返回，交给下一条指令前的 wait_ready 等待动画结束。"""
        deadline = self.rt.now() + ULTIMATE_SETTLE
        while self.rt.now() < deadline:
            snap = self.rt.fresh()
            if snap.valid and not snap.ready:
                return
            self.rt.sleep(SLEEP_SLICE)
