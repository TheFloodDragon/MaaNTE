"""战斗内核：把等待、输入、队伍识别、切人确认组合成一个可复用对象。

设计约束（见 docs/zh_cn/develop/combat-engine.md）：

- 内核只提供**机制**，不含任何业务策略；粉爪的捡箱/局内检测/交互监听
  通过 ``on_poll`` 钩子注入。
- 所有取值来自 ``constants.py``，与 ``pinkpaw_core3`` 逐一对齐。
- ``sleep`` 会驱动切人确认的异步轮询，因此任何等待都不能绕过它。
"""

from __future__ import annotations

import time

from .constants import (
    DIRECT_KEY_TAP_DURATION,
    DIRECT_ACTION_KEY_MIN_TAP_DURATION,
    DIRECT_ACTION_KEYS,
    LONG_PRESS_THRESHOLD,
    NEXT_FRAME_INTERVAL,
    TIMING_SENSITIVE_KEYS,
    WAIT_UNTIL_POLL_INTERVAL,
)
from . import team as team_mod
from .errors import AbortException, TaskerStoppedException
from .frames import (
    TemplateCache,
    as_bgr_image,
    crop_roi,
    crop_scaled_roi,
    fast_color_match,
    fast_template_match,
    is_hit,
    scale_roi,
    screencap,
)
from .input import ActionHelper, DirectInputSender, norm_key, normalize_key_sequence
from .switching import CharacterSwitcher, CharacterSwitchState, SwitchHooks
from .waiting import PollInfo, interruptible_sleep, scale_duration, wait_until

__all__ = [
    "AbortException",
    "ActionHelper",
    "CharacterSwitchState",
    "CharacterSwitcher",
    "CombatKernel",
    "DirectInputSender",
    "PollInfo",
    "SwitchHooks",
    "TaskerStoppedException",
    "TemplateCache",
    "as_bgr_image",
    "crop_roi",
    "crop_scaled_roi",
    "fast_color_match",
    "fast_template_match",
    "is_hit",
    "norm_key",
    "normalize_key_sequence",
    "scale_roi",
    "screencap",
    "team",
]

team = team_mod


class CombatKernel:
    """战斗与路线共用的底层能力集合。

    参数
    ----
    ctx
        MAA ``Context``。
    on_poll
        业务钩子，签名 ``(PollInfo) -> None``，由 ``sleep`` 每轮调用。
        钩子里请自行做节流，内核不会替你限频。
    on_poll_early
        高优先级业务钩子，签名 ``() -> None``，在切人确认轮询**之前**调用。
        粉爪的自动捡箱走这里，以保持与原实现相同的调用顺序。
    timing_scale
        等待时长的自适应微调倍率，语义与粉爪 ``timing_scale`` 一致。
    node_prefix
        临时 pipeline 节点名前缀，粉爪必须传 ``"PinkPawHeist"`` 以保持
        原有节点名不变。
    """

    def __init__(
        self,
        ctx,
        *,
        on_poll=None,
        on_poll_early=None,
        timing_scale=1.0,
        direct_input=True,
        node_prefix="Combat",
        log_prefix="[Combat/Kernel]",
        release_keys=("w", "a", "s", "d", "e", "f", "space", "lshift"),
        stop_message=None,
        switch_hooks_factory=None,
    ):
        self.ctx = ctx
        self.timing_scale = float(timing_scale)
        self.log_prefix = log_prefix
        self.ah = ActionHelper(
            ctx,
            direct_input=direct_input,
            node_prefix=node_prefix,
            log_prefix=log_prefix,
            release_keys=release_keys,
            stop_message=stop_message,
        )
        self._on_poll = on_poll
        self._on_poll_early = on_poll_early
        self._held_keys: set[str] = set()
        self._last_action_at: dict[str, float] = {}
        self.switcher = CharacterSwitcher(
            (switch_hooks_factory or self._default_switch_hooks)()
        )

    def set_poll_hooks(self, on_poll=None, on_poll_early=None):
        """在构造之后补设业务钩子。

        钩子常常需要引用宿主对象的方法，而宿主又要先持有内核，
        因此允许分两步接线，避免调用方去改内核私有字段。
        """
        if on_poll is not None:
            self._on_poll = on_poll
        if on_poll_early is not None:
            self._on_poll_early = on_poll_early

    def set_switch_hooks(self, hooks: SwitchHooks):
        """替换切人状态机的钩子集合。

        只允许在没有切人进行中时调用（构造后接线阶段），
        否则会丢掉正在确认的候选。
        """
        if self.switcher.state is not None:
            raise RuntimeError("cannot replace switch hooks while switching")
        self.switcher = CharacterSwitcher(hooks)

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    def log_info(self, *args):
        print(self.log_prefix, *args)

    def log_warning(self, *args):
        print(f"{self.log_prefix}[WARN]", *args)

    def log_error(self, *args):
        print(f"{self.log_prefix}[ERROR]", *args)

    # ------------------------------------------------------------------
    # 截图与识别
    # ------------------------------------------------------------------

    def screencap(self):
        """通过控制器截取当前游戏画面。"""
        return screencap(self.ctx)

    # ------------------------------------------------------------------
    # 等待
    # ------------------------------------------------------------------

    @property
    def held_keys(self) -> set[str]:
        return self._held_keys

    def has_timing_sensitive_key_held(self) -> bool:
        """判断当前是否按着移动、冲刺、跳跃等会影响走位精度的键。"""
        return bool(self._held_keys & TIMING_SENSITIVE_KEYS)

    def scale_duration(self, duration: float) -> float:
        """按 timing_scale 对等待时长做小幅自适应修正。"""
        return scale_duration(duration, self.timing_scale)

    def sleep(self, timeout, allow_slow_poll=True, scaled=True):
        """可中断等待：保持时间精度，同时推进切人确认与业务钩子。"""
        duration = max(float(timeout), 0.0)
        if scaled:
            duration = self.scale_duration(duration)
        return interruptible_sleep(
            duration,
            is_stopping=self.ah.is_stopping,
            raise_if_stopped=self.ah.raise_if_stopped,
            poll_early=self._poll_early,
            poll_kernel=self.switcher.poll,
            poll_late=self._poll_late,
            is_timing_sensitive=self.has_timing_sensitive_key_held,
            allow_slow_poll=allow_slow_poll,
        )

    def poll_sleep(self, timeout=WAIT_UNTIL_POLL_INTERVAL):
        """短轮询等待：不做时长缩放，也不触发低频重识别。"""
        return self.sleep(timeout, allow_slow_poll=False, scaled=False)

    def next_frame(self):
        """等待一个很短的轮询间隔，用在持续检测循环里。"""
        self.sleep(NEXT_FRAME_INTERVAL)
        return True

    def _poll_early(self):
        """在切人轮询之前插入的高优先级业务动作。"""
        if self._on_poll_early is not None:
            self._on_poll_early()

    def _poll_late(self, info: PollInfo):
        if self._on_poll is not None:
            self._on_poll(info)

    def wait_until(
        self,
        condition,
        time_out=0,
        pre_action=None,
        post_action=None,
        settle_time=-1,
        raise_if_not_found=False,
        **kwargs,
    ):
        """通用轮询等待函数，可在每轮检测前后插入动作并要求稳定命中。"""
        return wait_until(
            condition,
            sleep=self.poll_sleep,
            raise_if_stopped=self.ah.raise_if_stopped,
            time_out=time_out,
            pre_action=pre_action,
            post_action=post_action,
            settle_time=settle_time,
            raise_if_not_found=raise_if_not_found,
        )

    # ------------------------------------------------------------------
    # 按键
    # ------------------------------------------------------------------

    def check_interval(self, name: str, interval: float) -> bool:
        """按动作名做节流，避免同一个按键或点击在短时间内重复触发。"""
        if interval is None or interval < 0:
            return True
        now = time.monotonic()
        last = self._last_action_at.get(name, 0.0)
        if now - last < interval:
            return False
        self._last_action_at[name] = now
        return True

    def send_key(
        self, key, down_time=0.02, interval=-1, after_sleep=0, action_name=None
    ):
        """发送短按或长按按键，并支持动作节流和按后等待。"""
        key = norm_key(key)
        name = action_name or f"key:{key}"
        if not self.check_interval(name, interval):
            return False
        if down_time and down_time > LONG_PRESS_THRESHOLD:
            self.send_key_down(key)
            self.sleep(down_time)
            self.send_key_up(key)
        else:
            tap_duration = max(float(down_time or 0.0), DIRECT_KEY_TAP_DURATION)
            if key in DIRECT_ACTION_KEYS:
                tap_duration = max(tap_duration, DIRECT_ACTION_KEY_MIN_TAP_DURATION)
            self.ah.click_key(key, duration=tap_duration)
        if after_sleep:
            self.sleep(after_sleep)
        return True

    def send_key_down(self, key, after_sleep=0):
        """按下按键并记录内部状态。"""
        key = norm_key(key)
        self._held_keys.add(key)
        ret = self.ah.key_down(key)
        if after_sleep:
            self.sleep(after_sleep)
        return ret

    def send_key_up(self, key, after_sleep=0):
        """抬起按键并清理内部状态。"""
        key = norm_key(key)
        try:
            return self.ah.key_up(key)
        finally:
            self._held_keys.discard(key)
            if after_sleep:
                self.sleep(after_sleep)

    def release_held_keys(self):
        """释放内部记录为按住状态的键，防止异常后持续输入。"""
        held = list(self._held_keys)
        self._held_keys.clear()
        for key in held:
            try:
                self.ah.key_up(key)
            except Exception as exc:
                self.log_error(f"release held key {key} failed", exc)

    def release_controls(self):
        """释放全部按键与鼠标键。"""
        self.release_held_keys()
        self.ah.release_controls()

    # ------------------------------------------------------------------
    # 鼠标
    # ------------------------------------------------------------------

    def mouse_down(self, key="left"):
        self.ah.mouse_down(key=key)

    def mouse_up(self, key="left"):
        self.ah.mouse_up(key=key)

    def click(
        self,
        x=-1,
        y=-1,
        name=None,
        interval=-1,
        key="left",
        down_time=0.01,
        after_sleep=0,
    ):
        """点击指定坐标；小于 0 表示画面中心，浮点数表示相对比例。"""
        from .constants import DEFAULT_HEIGHT, DEFAULT_WIDTH

        name = name or f"click:{key}"
        if not self.check_interval(name, interval):
            return False
        if x == -1:
            x = 0.5
        if y == -1:
            y = 0.5
        px = int(x * DEFAULT_WIDTH) if isinstance(x, float) and x <= 1 else int(x)
        py = int(y * DEFAULT_HEIGHT) if isinstance(y, float) and y <= 1 else int(y)
        if key == "left" and down_time <= 0.05:
            ret = self.ah.click(px, py)
        else:
            self.ah.move_to(px, py)
            self.ah.mouse_down(key=key)
            self.sleep(max(down_time, 0.01))
            self.ah.mouse_up(key=key)
            ret = True
        if after_sleep:
            self.sleep(after_sleep)
        return ret

    # ------------------------------------------------------------------
    # 队伍 UI
    # ------------------------------------------------------------------

    def is_in_team(self, image=None):
        """截图并判断当前是否处于队伍可操作界面。"""
        if image is None:
            image = self.screencap()
        if image is None:
            return True
        return team_mod.is_in_team(image)

    def is_black_screen(self, image=None):
        """判断是否处于黑屏/加载状态。"""
        if image is None:
            image = self.screencap()
        if image is None:
            return False
        return team_mod.is_black_screen(image)

    def current_slot_index(self, image=None):
        """返回当前高亮的角色槽位索引；无法可靠判断时返回 -1。"""
        if image is None:
            image = self.screencap()
        if image is None:
            return -1
        return team_mod.current_slot_index(image)

    def is_slot_active(self, index, image=None):
        """判断当前高亮角色是否为指定槽位。"""
        if image is None:
            image = self.screencap()
        if image is None:
            return False
        return team_mod.is_slot_active(image, index)

    def ensure_in_team(self, time_out=2.0):
        """尝试按 Esc 关闭弹窗或复活界面，直到回到队伍 UI。"""
        deadline = time.monotonic() + time_out
        while time.monotonic() < deadline:
            if self.is_in_team():
                return True
            self.send_key("esc", action_name="ensure_in_team", interval=0.3)
            self.sleep(0.05, allow_slow_poll=False, scaled=False)
        return self.is_in_team()

    def wait_team_ui_settle(self):
        """等待加载、黑屏或楼层切换结束，直到队伍 UI 重新稳定出现。"""
        self.wait_until(
            lambda: not self.is_in_team(),
            time_out=1,
            raise_if_not_found=False,
        )
        self.wait_until(
            self.is_in_team,
            time_out=30,
            settle_time=0.25,
            raise_if_not_found=False,
        )
        self.sleep(0.1, allow_slow_poll=False)
        return True

    # ------------------------------------------------------------------
    # 切人（战斗引擎 v1 不使用，仅供路线复用）
    # ------------------------------------------------------------------

    def _default_switch_hooks(self) -> SwitchHooks:
        return SwitchHooks(
            screencap=self.screencap,
            is_black_screen=team_mod.is_black_screen,
            is_in_team=team_mod.is_in_team,
            is_slot_active=team_mod.is_slot_active,
            send_key=self.send_key,
            ensure_in_team=self.ensure_in_team,
            sleep=self.poll_sleep,
            log_warning=self.log_warning,
        )
