"""战斗输入驱动：只通过 MaaFramework 控制器发键，遵循用户选择的控制器类型。

所有按住类操作都会记账，release_all() 保证异常/停止后不会残留按住的键。
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Callable, Iterable

from utils.logger import logger

from .keys import (
    DODGE_KEY,
    DODGE_LEAD,
    FORWARD_KEY,
    MOUSE_CONTACT,
    SPRINT_LEAD,
    TAP_SECONDS,
    TURN_PIXELS_PER_DEGREE,
    TURN_STEP_DELAY,
    TURN_STEP_PIXELS,
    VK,
)


def _wait(job) -> bool:
    if job is None:
        return False
    try:
        result = job.wait()
    except Exception as exc:  # 控制器异常不应打断战斗流程
        logger.warning("combat input job failed: %s", exc)
        return False
    return bool(getattr(result, "succeeded", True))


class InputDriver:
    def __init__(self, controller, sleep: Callable[[float], None], center=(640, 360)):
        self._controller = controller
        self._sleep = sleep
        self.center = center
        self._held_keys: set[int] = set()
        self._held_mouse: set[int] = set()
        self._turn_supported = True

    # ---- 键盘 ----
    def key_down(self, key: str) -> bool:
        vk = VK[key]
        self._held_keys.add(vk)
        return _wait(self._controller.post_key_down(vk))

    def key_up(self, key: str) -> bool:
        vk = VK[key]
        self._held_keys.discard(vk)
        return _wait(self._controller.post_key_up(vk))

    @contextmanager
    def pressing(self, keys: Iterable[str]):
        """按住一组键；退出时（含停止、结束信号、异常）逆序松开。"""
        pressed: list[str] = []
        try:
            for key in keys:
                self.key_down(key)
                pressed.append(key)
            yield
        finally:
            for key in reversed(pressed):
                self.key_up(key)

    def tap(self, key: str, seconds: float = TAP_SECONDS) -> None:
        """按下 → 保持 seconds → 松开。保持期间可被停止/结束信号打断，finally 松键。"""
        with self.pressing((key,)):
            self._sleep(max(seconds, TAP_SECONDS))

    def hold_keys(self, keys: Iterable[str], seconds: float) -> None:
        """同时按住多个键 seconds 秒（如斜向移动 w + a）。"""
        with self.pressing(keys):
            self._sleep(seconds)

    def sprint(self, seconds: float) -> None:
        """冲刺：按住 W，SPRINT_LEAD 秒后点一下 Shift，继续按住到 seconds。"""
        lead = min(SPRINT_LEAD, seconds)
        with self.pressing((FORWARD_KEY,)):
            self._sleep(lead)
            self.tap(DODGE_KEY)
            self._sleep(max(0.0, seconds - lead - TAP_SECONDS))

    def dodge(self, direction: str = "") -> None:
        """闪避；带方向时先按住方向键再点 Shift（如 s = 后撤）。"""
        if not direction:
            self.tap(DODGE_KEY)
            return
        with self.pressing(tuple(direction)):
            self._sleep(DODGE_LEAD)
            self.tap(DODGE_KEY)

    # ---- 鼠标（Win32 控制器 contact：0 左 / 1 右 / 2 中）----
    def mouse_down(self, button: str) -> bool:
        contact = MOUSE_CONTACT[button]
        self._held_mouse.add(contact)
        x, y = self.center
        return _wait(self._controller.post_touch_down(x, y, contact))

    def mouse_up(self, button: str) -> bool:
        contact = MOUSE_CONTACT[button]
        self._held_mouse.discard(contact)
        return _wait(self._controller.post_touch_up(contact))

    def click(self, button: str = "left") -> bool:
        x, y = self.center
        return _wait(self._controller.post_click(x, y, MOUSE_CONTACT[button]))

    def hold_mouse(self, button: str, seconds: float) -> None:
        self.mouse_down(button)
        try:
            self._sleep(seconds)
        finally:
            self.mouse_up(button)

    # ---- 视角 ----
    def turn(self, degrees: float) -> bool:
        """相对移动鼠标转动视角（正数向右）。只有前台 Seize 鼠标支持；不支持时记一次警告，之后不再尝试。"""
        total = int(round(degrees * TURN_PIXELS_PER_DEGREE))
        if total == 0 or not self._turn_supported:
            return self._turn_supported
        move = getattr(self._controller, "post_relative_move", None)
        if move is None:
            return self._turn_unsupported("controller has no post_relative_move")
        steps = max(1, math.ceil(abs(total) / TURN_STEP_PIXELS))
        sent = 0
        for i in range(1, steps + 1):
            target = int(round(total * i / steps))
            try:
                ok = _wait(move(target - sent, 0))
            except Exception as exc:  # 绑定或控制器不支持相对移动
                return self._turn_unsupported(exc)
            if not ok:
                return self._turn_unsupported("relative move rejected")
            sent = target
            if i < steps:
                self._sleep(TURN_STEP_DELAY)
        return True

    def _turn_unsupported(self, reason) -> bool:
        self._turn_supported = False
        logger.warning("combat view turn disabled (needs foreground Seize mouse): %s", reason)
        return False

    # ---- 清理 ----
    @property
    def holding(self) -> bool:
        return bool(self._held_keys or self._held_mouse)

    def release_all(self) -> None:
        """释放所有记账中的按键。逐个 try，单个失败不影响其余。"""
        for vk in list(self._held_keys):
            try:
                self._controller.post_key_up(vk).wait()
            except Exception as exc:
                logger.warning("combat release key %s failed: %s", vk, exc)
        for contact in list(self._held_mouse):
            try:
                self._controller.post_touch_up(contact).wait()
            except Exception as exc:
                logger.warning("combat release mouse %s failed: %s", contact, exc)
        self._held_keys.clear()
        self._held_mouse.clear()
