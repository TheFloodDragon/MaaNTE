"""角色切换确认状态机。

从 ``pinkpaw_core3`` 原样搬移，状态转移与重试次数逐一对齐：

- ``CharacterSwitchState``          <- 同名 dataclass
- ``CharacterSwitcher.begin``      <- ``_begin_character_switch``
- ``CharacterSwitcher.poll``       <- ``_poll_character_switch``
- ``CharacterSwitcher._wait``      <- ``_wait_character_switch_success``
- ``CharacterSwitcher._handle_dead`` <- ``_handle_dead_switch_candidate``

角色相关的业务簿记（例如粉爪记录"哪个战斗位死了"）通过
``on_key_sent`` / ``on_candidate_dead`` 回调外置，内核只管状态机。

注意：v1 战斗引擎**不使用**本模块，它只被粉爪路线复用。战斗侧的队伍
切换按设计计划留到后续版本。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .constants import (
    SWITCH_BLACK_SCREEN_EXTENSION,
    SWITCH_CHECK_DURATION,
    SWITCH_CONFIRM_RETRY_COUNT,
    SWITCH_CONFIRM_RETRY_WINDOW,
    SWITCH_DEAD_SETTLE,
    SWITCH_FIRST_POLL_DELAY,
    SWITCH_POLL_INTERVAL,
    WAIT_UNTIL_POLL_INTERVAL,
)
from .errors import AbortException


@dataclass
class CharacterSwitchState:
    role: str
    keys: list[str]
    index: int = 0
    deadline: float = 0

    @property
    def current_key(self):
        """返回当前正在尝试切换的角色按键。"""
        return self.keys[self.index]

    def advance(self):
        """把角色切换候选推进到下一个按键，并返回是否还有候选可试。"""
        self.index += 1
        return self.index < len(self.keys)


@dataclass
class SwitchHooks:
    """状态机需要的外部能力。全部必填，便于离线用假实现替换。"""

    screencap: callable
    is_black_screen: callable
    is_in_team: callable
    is_slot_active: callable
    send_key: callable
    ensure_in_team: callable
    sleep: callable
    log_warning: callable
    on_key_sent: callable = field(default=lambda role, key: None)
    on_candidate_dead: callable = field(default=lambda role, key: None)
    monotonic: callable = field(default=time.monotonic)


class CharacterSwitcher:
    """驱动"按下角色键 -> 确认槽位高亮"的异步确认过程。"""

    def __init__(self, hooks: SwitchHooks):
        self._h = hooks
        self.state: CharacterSwitchState | None = None
        self.handling = False
        self._next_poll_at = 0.0

    # --- 状态维护 ---

    def clear(self):
        """清空正在进行的角色切换状态。"""
        self.state = None
        self._next_poll_at = 0.0

    def _send_current_key(self):
        """发送当前候选角色键，并刷新切换确认截止时间。"""
        state = self.state
        if state is None:
            return None
        key = state.current_key
        self._h.on_key_sent(state.role, key)
        now = self._h.monotonic()
        state.deadline = now + SWITCH_CHECK_DURATION
        self._next_poll_at = now + SWITCH_FIRST_POLL_DELAY
        self._h.send_key(key)
        return key

    def _handle_dead(self, state: CharacterSwitchState):
        """切人后疑似不在队伍 UI 时，按角色死亡处理并尝试下一个候选。"""
        role = state.role
        key = state.current_key
        self._h.log_warning(f"{role} char {key} may be dead, try next")
        self._h.on_candidate_dead(role, key)
        self._h.ensure_in_team()
        if not state.advance():
            self.clear()
            raise AbortException(f"{role} {state.keys} dead or empty")
        self._send_current_key()

    # --- 由 sleep 驱动的异步轮询 ---

    def poll(self):
        """后台监控未确认切人过程，处理黑屏、死亡和复活界面。"""
        if self.state is None or self.handling:
            return
        now = self._h.monotonic()
        if now < self._next_poll_at:
            return
        self._next_poll_at = now + SWITCH_POLL_INTERVAL

        state = self.state
        if now > state.deadline:
            self.clear()
            return

        image = self._h.screencap()
        if image is not None and self._h.is_black_screen(image):
            state.deadline = max(
                state.deadline,
                self._h.monotonic() + SWITCH_BLACK_SCREEN_EXTENSION,
            )
            return
        if image is None or self._h.is_in_team(image):
            return

        self.handling = True
        try:
            self._handle_dead(state)
        finally:
            self.handling = False

    # --- 同步确认 ---

    def _wait(self, role, key):
        """等待目标槽位高亮确认；没确认时重按，疑似死亡时换下一个候选。"""
        last_key = str(key)
        retry_count = 0
        retry_key = last_key
        not_team_since = None
        old_handling = self.handling
        self.handling = True
        try:
            while self.state is not None:
                state = self.state
                last_key = state.current_key
                if retry_key != last_key:
                    retry_key = last_key
                    retry_count = 0
                now = self._h.monotonic()
                if now > state.deadline:
                    if retry_count < SWITCH_CONFIRM_RETRY_COUNT:
                        retry_count += 1
                        self._h.log_warning(
                            f"{role} switch to {last_key} not confirmed, retry {retry_count}"
                        )
                        self._h.send_key(
                            last_key,
                            action_name=f"switch_char_retry:{last_key}",
                            interval=-1,
                        )
                        state.deadline = (
                            self._h.monotonic() + SWITCH_CONFIRM_RETRY_WINDOW
                        )
                        not_team_since = None
                        continue
                    self._h.log_warning(f"{role} switch to {last_key} not confirmed")
                    self.clear()
                    return last_key

                self._h.send_key(last_key, action_name="switch_char", interval=0.5)
                image = self._h.screencap()
                if image is not None and self._h.is_slot_active(
                    image, int(last_key) - 1
                ):
                    self.clear()
                    return last_key

                if image is not None and self._h.is_black_screen(image):
                    state.deadline = max(
                        state.deadline,
                        self._h.monotonic() + SWITCH_BLACK_SCREEN_EXTENSION,
                    )
                    not_team_since = None
                    self._h.sleep(WAIT_UNTIL_POLL_INTERVAL)
                    continue

                in_team = True if image is None else self._h.is_in_team(image)
                if in_team:
                    not_team_since = None
                else:
                    if not_team_since is None:
                        not_team_since = now
                    elif now - not_team_since >= SWITCH_DEAD_SETTLE:
                        self._handle_dead(state)
                        not_team_since = None

                self._h.sleep(WAIT_UNTIL_POLL_INTERVAL)
        finally:
            self.handling = old_handling

        return last_key

    def begin(self, role, keys, check_switched=False):
        """创建切人状态并发出首个候选按键，必要时等待高亮确认。"""
        keys = [str(key) for key in keys]
        if not keys:
            raise AbortException(f"{role} {keys} dead or empty")
        self.state = CharacterSwitchState(role=role, keys=keys)
        key = self._send_current_key()
        if check_switched:
            return self._wait(role, key)
        return key
