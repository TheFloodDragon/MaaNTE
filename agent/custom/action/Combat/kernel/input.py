"""键鼠输入层：Windows SendInput 直发 + MAA 控制器兜底。

从 ``pinkpaw_core3`` 原样搬移：

- ``DirectInputSender``  <- 同名类，逐字段一致
- ``ActionHelper``       <- ``Core3ActionHelper``

与 core3 的唯一结构性差异：临时 pipeline 节点名前缀可配置
（``node_prefix``），粉爪侧传入 ``"PinkPawHeist"`` 后节点名与原先一致。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from .constants import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    DIRECT_KEY_TAP_DURATION,
    DIRECT_QUICK_PICK_TAP_DURATION,
    MOUSE_VK,
    VK,
)
from .errors import TaskerStoppedException

ULONG_PTR = (
    ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


def norm_key(key: str) -> str:
    """把配置里的按键名规范成小写字符串，便于查虚拟键码。"""
    return str(key).lower()


def normalize_key_sequence(value) -> list[str]:
    """Normalize one key or a sequence of keys into a de-duplicated key list."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if len(text) > 1 and all(char.lower() in {"w", "a", "s", "d"} for char in text):
            keys = list(text)
        else:
            keys = text.replace("+", " ").replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        keys = []
        for item in value:
            keys.extend(normalize_key_sequence(item))
    else:
        keys = [str(value)]

    result = []
    for key in keys:
        normalized = norm_key(key)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


class DirectInputSender:
    """通过 Win32 SendInput 直发键鼠事件，绕过 MAA 节点开销。"""

    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    MAPVK_VK_TO_VSC = 0
    MOUSE_FLAGS = {
        "left": (0x0002, 0x0004),
        "right": (0x0008, 0x0010),
        "middle": (0x0020, 0x0040),
    }

    def __init__(self, enabled=True, log_prefix="[Combat/Kernel]"):
        self.enabled = bool(enabled)
        self.available = False
        self.user32 = None
        self._log_prefix = log_prefix
        if not self.enabled:
            return
        try:
            self.user32 = ctypes.windll.user32
            self.user32.SendInput.argtypes = [
                wintypes.UINT,
                ctypes.POINTER(_INPUT),
                ctypes.c_int,
            ]
            self.user32.SendInput.restype = wintypes.UINT
            self.available = True
        except Exception as exc:
            print(f"{self._log_prefix}[WARN] direct input unavailable: {exc}")

    def _send(self, input_obj):
        if not self.available or self.user32 is None:
            return False
        sent = self.user32.SendInput(
            1, ctypes.byref(input_obj), ctypes.sizeof(input_obj)
        )
        return sent == 1

    def _keyboard_input(self, vk, is_up=False):
        scan = int(self.user32.MapVirtualKeyW(int(vk), self.MAPVK_VK_TO_VSC))
        flags = self.KEYEVENTF_KEYUP if is_up else 0
        w_vk = int(vk)
        if scan:
            flags |= self.KEYEVENTF_SCANCODE
            w_vk = 0
        input_obj = _INPUT()
        input_obj.type = self.INPUT_KEYBOARD
        input_obj.u.ki = _KEYBDINPUT(
            wVk=w_vk,
            wScan=scan,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        )
        return input_obj

    def key_down(self, vk):
        return self._send(self._keyboard_input(vk, is_up=False))

    def key_up(self, vk):
        return self._send(self._keyboard_input(vk, is_up=True))

    def click_key(self, vk, duration=DIRECT_KEY_TAP_DURATION):
        if not self.key_down(vk):
            return False
        released = False
        try:
            time.sleep(max(float(duration), 0.0))
            released = self.key_up(vk)
            return released
        finally:
            if not released:
                self.key_up(vk)

    def _mouse_input(self, flags):
        input_obj = _INPUT()
        input_obj.type = self.INPUT_MOUSE
        input_obj.u.mi = _MOUSEINPUT(
            dx=0,
            dy=0,
            mouseData=0,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        )
        return input_obj

    def mouse_down(self, key="left"):
        flags = self.MOUSE_FLAGS.get(key, self.MOUSE_FLAGS["left"])[0]
        return self._send(self._mouse_input(flags))

    def mouse_up(self, key="left"):
        flags = self.MOUSE_FLAGS.get(key, self.MOUSE_FLAGS["left"])[1]
        return self._send(self._mouse_input(flags))


class ActionHelper:
    """封装按键、鼠标、点击与安全释放，优先直发、失败回落 MAA 控制器。"""

    def __init__(
        self,
        ctx,
        direct_input=True,
        node_prefix="Combat",
        log_prefix="[Combat/Kernel]",
        release_keys=("w", "a", "s", "d", "e", "f", "space", "lshift"),
        stop_message=None,
    ):
        """保存 MAA 上下文，并初始化鼠标当前位置缓存。"""
        self.ctx = ctx
        self.mx, self.my = DEFAULT_WIDTH // 2, DEFAULT_HEIGHT // 2
        self.direct_input = DirectInputSender(
            enabled=direct_input, log_prefix=log_prefix
        )
        self._node_prefix = str(node_prefix)
        self._log_prefix = log_prefix
        self._release_keys = tuple(release_keys)
        self._stop_message = stop_message or f"{self._node_prefix} stopped by Maa tasker."

    @property
    def controller(self):
        """取得当前 tasker 的控制器，用于直接发送按键、鼠标和截图请求。"""
        return getattr(getattr(self.ctx, "tasker", None), "controller", None)

    def is_stopping(self) -> bool:
        """检查 MAA tasker 是否正在停止任务。"""
        tasker = getattr(self.ctx, "tasker", None)
        if tasker is None:
            return False
        stopping = getattr(tasker, "stopping", False)
        if callable(stopping):
            stopping = stopping()
        return bool(stopping)

    def raise_if_stopped(self):
        """任务停止时抛出专用异常，打断正在执行的流程。"""
        if self.is_stopping():
            raise TaskerStoppedException(self._stop_message)

    def run_task(self, task_name, pipeline_override=None):
        """运行一个 MAA pipeline 节点，并在调用前后检查停止状态。"""
        self.raise_if_stopped()
        if pipeline_override is None:
            result = self.ctx.run_task(task_name)
        else:
            result = self.ctx.run_task(task_name, pipeline_override=pipeline_override)
        self.raise_if_stopped()
        return result

    def _call_key(self, node_type, key_str, extra=None):
        """发送 KeyDown、KeyUp 或 ClickKey；有控制器时走低延迟直发，否则临时跑节点。"""
        if node_type != "KeyUp":
            self.raise_if_stopped()
        vk = VK.get(norm_key(key_str))
        if vk is None:
            return False
        direct = self.direct_input
        if direct.available:
            if node_type == "KeyDown" and direct.key_down(vk):
                if node_type != "KeyUp":
                    self.raise_if_stopped()
                return True
            if node_type == "KeyUp" and direct.key_up(vk):
                return True
            if node_type == "ClickKey":
                duration = DIRECT_KEY_TAP_DURATION
                if extra and "direct_duration" in extra:
                    duration = float(extra["direct_duration"])
                if direct.click_key(vk, duration=duration):
                    self.raise_if_stopped()
                    return True
        controller = self.controller
        if controller is not None:
            if node_type == "KeyDown":
                controller.post_key_down(vk)
            elif node_type == "KeyUp":
                controller.post_key_up(vk)
            elif node_type == "ClickKey":
                if hasattr(controller, "post_click_key"):
                    controller.post_click_key(vk)
                else:
                    controller.post_key_down(vk)
                    time.sleep(0.02)
                    controller.post_key_up(vk)
            if node_type != "KeyUp":
                self.raise_if_stopped()
            return True
        param = {"key": vk}
        if extra:
            param.update(
                {key: value for key, value in extra.items() if key != "direct_duration"}
            )
        node_name = f"{self._node_prefix}_{node_type}"
        override = {node_name: {"action": {"type": node_type, "param": param}}}
        ret = self.ctx.run_task(node_name, pipeline_override=override) is not None
        if node_type != "KeyUp":
            self.raise_if_stopped()
        return ret

    def click_key(self, key_str, duration=None):
        """发送一次按键点击。"""
        key = norm_key(key_str)
        if duration is None:
            duration = (
                DIRECT_QUICK_PICK_TAP_DURATION
                if key == "f"
                else DIRECT_KEY_TAP_DURATION
            )
        extra = None
        if duration is not None:
            extra = {"direct_duration": max(float(duration), 0.0)}
        return self._call_key("ClickKey", key_str, extra=extra)

    def key_down(self, key_str):
        """发送按键按下事件。"""
        return self._call_key("KeyDown", key_str)

    def key_up(self, key_str):
        """发送按键抬起事件。"""
        return self._call_key("KeyUp", key_str)

    def move_to(self, x, y, duration_ms=None):
        """把鼠标移动到指定坐标，并维护内部鼠标位置缓存。"""
        self.raise_if_stopped()
        x, y = int(x), int(y)
        dx, dy = x - self.mx, y - self.my
        if dx * dx + dy * dy < 4:
            self.mx, self.my = x, y
            return True
        if duration_ms is None:
            duration_ms = max(int((dx**2 + dy**2) ** 0.5 / 0.5), 50)
        node_name = f"{self._node_prefix}_MouseMove"
        override = {
            node_name: {
                "action": {
                    "type": "Swipe",
                    "param": {
                        "begin": [self.mx, self.my],
                        "end": [x, y],
                        "duration": duration_ms,
                        "only_hover": True,
                    },
                }
            }
        }
        ret = self.ctx.run_task(node_name, pipeline_override=override)
        self.raise_if_stopped()
        if ret:
            self.mx, self.my = x, y
        return ret

    def click(self, x, y):
        """点击指定坐标；控制器可用时直接点击，否则走 MAA Click 节点。"""
        self.raise_if_stopped()
        controller = self.controller
        if controller is not None and hasattr(controller, "post_click"):
            controller.post_click(int(x), int(y))
            self.raise_if_stopped()
            self.mx, self.my = int(x), int(y)
            return True
        self.move_to(x, y)
        node_name = f"{self._node_prefix}_Click"
        override = {
            node_name: {
                "action": {"type": "Click", "param": {"target": [int(x), int(y)]}}
            }
        }
        ret = self.ctx.run_task(node_name, pipeline_override=override) is not None
        self.raise_if_stopped()
        return ret

    def focus_window(self, x=None, y=None):
        """Use a controller click to bring the game window to foreground."""
        self.raise_if_stopped()
        px = DEFAULT_WIDTH // 2 if x is None else int(x)
        py = DEFAULT_HEIGHT // 2 if y is None else int(y)
        controller = self.controller
        if controller is not None and hasattr(controller, "post_click"):
            ret = controller.post_click(px, py)
            if hasattr(ret, "wait"):
                ret.wait()
            self.mx, self.my = px, py
            self.raise_if_stopped()
            return True
        return self.click(px, py)

    def mouse_down(self, key="left"):
        """发送鼠标按下事件，主要用于长按攻击或鼠标键操作。"""
        if self.direct_input.mouse_down(key=key):
            return
        vk = MOUSE_VK.get(key, MOUSE_VK["left"])
        controller = self.controller
        if controller is not None:
            controller.post_key_down(vk)

    def mouse_up(self, key="left"):
        """发送鼠标抬起事件，配合 mouse_down 结束长按。"""
        if self.direct_input.mouse_up(key=key):
            return
        vk = MOUSE_VK.get(key, MOUSE_VK["left"])
        controller = self.controller
        if controller is not None:
            controller.post_key_up(vk)

    def release_controls(self):
        """释放脚本可能按住的键与鼠标键，防止异常后继续输入。"""
        for key in self._release_keys:
            try:
                self.key_up(key)
            except Exception as exc:
                print(f"{self._log_prefix} failed to release {key}: {exc}")
        for key in MOUSE_VK:
            try:
                self.direct_input.mouse_up(key)
            except Exception as exc:
                print(f"{self._log_prefix} failed to release direct mouse {key}: {exc}")
        controller = self.controller
        if controller is None:
            return
        for vk in MOUSE_VK.values():
            try:
                controller.post_key_up(vk).wait()
            except Exception as exc:
                print(f"{self._log_prefix} failed to release mouse {vk}: {exc}")
