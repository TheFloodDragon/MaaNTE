"""把 ``Action`` 翻译成内核调用。

这一层是编排层与内核之间**唯一**的桥。分开的理由：

- ``engine`` 决定"该做什么"（纯逻辑，可离线测试）；
- ``primitives`` 决定"怎么做"（调内核，需要真实环境）。

于是引擎的测试完全不需要 mock 内核，只要断言它产出的动作序列即可；
而这一层足够薄，用一个假内核就能验证"动作 -> 调用"的映射是否正确。

**按住的键必须被释放**：``ActionRunner`` 记录自己按下过的键，
``release_all`` 保证异常路径也能松手。战斗中最糟的 bug 是角色卡住一直走。
"""

from __future__ import annotations

from .schema import (
    ACTION_CLICK,
    ACTION_HOLD,
    ACTION_KEY,
    ACTION_MOUSE_DOWN,
    ACTION_MOUSE_UP,
    ACTION_RELEASE,
    ACTION_WAIT,
    Action,
)


class ActionRunner:
    """按顺序执行动作序列，并跟踪自己按住的键。"""

    def __init__(self, kernel, log=None):
        self.kernel = kernel
        self._held: set[str] = set()
        self._held_mouse: set[str] = set()
        self._log = log or (lambda *a: None)

    @property
    def held_keys(self) -> set[str]:
        return set(self._held)

    @property
    def held_mouse(self) -> set[str]:
        return set(self._held_mouse)

    def run(self, actions) -> int:
        """执行一串动作，返回成功执行的条数。"""
        done = 0
        for action in actions:
            self.run_one(action)
            done += 1
        return done

    def run_one(self, action: Action):
        """执行单个动作。类型未知时记日志并跳过，不抛异常。"""
        kind = action.type

        if kind == ACTION_WAIT:
            self.kernel.sleep(action.duration)
            return True

        if kind == ACTION_KEY:
            # 点按：交给内核的 send_key，长按语义由 duration 决定
            self.kernel.send_key(action.key, down_time=action.duration or 0.02)
            return True

        if kind == ACTION_HOLD:
            self.kernel.send_key_down(action.key)
            self._held.add(action.key)
            return True

        if kind == ACTION_RELEASE:
            self.kernel.send_key_up(action.key)
            self._held.discard(action.key)
            return True

        if kind == ACTION_CLICK:
            self.kernel.click(key=action.key, down_time=action.duration or 0.01)
            return True

        if kind == ACTION_MOUSE_DOWN:
            self.kernel.mouse_down(key=action.key)
            self._held_mouse.add(action.key)
            return True

        if kind == ACTION_MOUSE_UP:
            self.kernel.mouse_up(key=action.key)
            self._held_mouse.discard(action.key)
            return True

        self._log(f"未知动作类型被跳过: {kind!r}")
        return False

    def release_all(self):
        """释放本 runner 按住的一切键与鼠标键。

        逐个 try：一个键释放失败不能连累其它键，否则角色可能一直按着 W
        往墙里走。
        """
        for key in sorted(self._held):
            try:
                self.kernel.send_key_up(key)
            except Exception as exc:
                self._log(f"释放按键 {key} 失败: {exc}")
        self._held.clear()
        for key in sorted(self._held_mouse):
            try:
                self.kernel.mouse_up(key=key)
            except Exception as exc:
                self._log(f"释放鼠标键 {key} 失败: {exc}")
        self._held_mouse.clear()
