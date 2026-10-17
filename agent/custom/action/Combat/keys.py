"""战斗按键表。键位与 ok-NTE 默认一致：E 技能、Q 终结技、R 弧盘、空格跳、左 Shift 闪避、1-4 切人。"""

from __future__ import annotations

VK: dict[str, int] = {
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},  # a-z → 0x41-0x5A
    **{str(d): 0x30 + d for d in range(10)},  # 0-9 → 0x30-0x39
    "space": 0x20,
    "tab": 0x09,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "lalt": 0xA4,
    "ralt": 0xA5,
}

# 出招表允许直接使用的按键：不含 Esc、回车、功能键等可能打开菜单或干扰系统的键
ALLOWED_KEYS = frozenset(VK)

# Win32 控制器 touch/click 的 contact 编号：0 左键、1 右键、2 中键
MOUSE_CONTACT: dict[str, int] = {"left": 0, "right": 1, "middle": 2}

DIRECTIONS = frozenset({"w", "a", "s", "d"})

SKILL_KEY = "e"
ULTIMATE_KEY = "q"
ARC_KEY = "r"
JUMP_KEY = "space"
DODGE_KEY = "lshift"
SLOT_KEYS = {1: "1", 2: "2", 3: "3", 4: "4"}

# 点按时长。部分 UE 游戏按低帧率轮询输入，过短的按键可能被吞，统一给 50ms
TAP_SECONDS = 0.05
# 连点间隔，与 ok-NTE continues_normal_attack 一致
CLICK_INTERVAL = 0.1
