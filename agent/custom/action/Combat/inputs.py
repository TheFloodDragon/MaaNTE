"""战斗输入驱动：只通过 MaaFramework 控制器发键，遵循用户选择的控制器类型。

所有按住类操作都会记账，release_all() 保证异常/停止后不会残留按住的键。
"""

from __future__ import annotations

from typing import Callable

from utils.logger import logger

from .keys import CLICK_INTERVAL, MOUSE_CONTACT, TAP_SECONDS, VK


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

    # ---- 键盘 ----
    def key_down(self, key: str) -> bool:
        vk = VK[key]
        self._held_keys.add(vk)
        return _wait(self._controller.post_key_down(vk))

    def key_up(self, key: str) -> bool:
        vk = VK[key]
        self._held_keys.discard(vk)
        return _wait(self._controller.post_key_up(vk))

    def tap(self, key: str, seconds: float = TAP_SECONDS) -> None:
        """按下 → 保持 seconds → 松开。保持期间可被停止/结束信号打断，finally 松键。"""
        self.key_down(key)
        try:
            self._sleep(max(seconds, TAP_SECONDS))
        finally:
            self.key_up(key)

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

    def click_for(self, button: str, seconds: float, now: Callable[[], float]) -> None:
        """在 seconds 秒内以固定间隔连点。"""
        deadline = now() + seconds
        while True:
            self.click(button)
            remaining = deadline - now()
            if remaining <= 0:
                return
            self._sleep(min(CLICK_INTERVAL, remaining))

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
