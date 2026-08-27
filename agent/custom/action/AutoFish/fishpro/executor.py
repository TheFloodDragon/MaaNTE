"""FishPro 控条的执行层。

移植附件 ``autofish.py`` 的按键状态机：方向死区（含中心区死区放大）、
切换保持保护、脉冲按压时长插值、长按判定、脉冲延长、人类化节奏与
反应延迟、最小保持时间。

与附件的差异：按键动作通过注入的 ``KeyAdapter`` 执行，从而复用
MaaFramework 控制器，同时兼容桌面前台/后台与云异环前台控制器。
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional, Protocol

from .config import FishProConfig
from .state import ControlState, Observation
from .thresholds import clamp_float, dynamic_center_reentry_px

KEY_A = 65
KEY_D = 68

DIRECTION_KEYS = {"left": KEY_A, "right": KEY_D}


class KeyAdapter(Protocol):
    """按键下发接口，便于离线测试注入假实现。"""

    def key_down(self, key: int) -> None: ...

    def key_up(self, key: int) -> None: ...


class ControllerKeyAdapter:
    """把按键动作转发给 MaaFramework 控制器。"""

    def __init__(self, controller):
        self._controller = controller

    def key_down(self, key: int) -> None:
        self._controller.post_key_down(key).wait()

    def key_up(self, key: int) -> None:
        self._controller.post_key_up(key).wait()


class ActionExecutor:
    """按动作强度驱动 A/D 按键的状态机。"""

    def __init__(
        self,
        adapter: KeyAdapter,
        config: FishProConfig,
        rng: Optional[random.Random] = None,
        clock: Callable[[], float] = time.perf_counter,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._adapter = adapter
        self._config = config
        self._rng = rng or random.Random()
        self._clock = clock
        self._sleep = sleeper

    # ---- 按键原语 ----

    def release_all(self) -> None:
        """无条件释放 A/D，用于会话结束、异常与任务停止。"""
        from utils.logger import logger

        for key in (KEY_A, KEY_D):
            try:
                self._adapter.key_up(key)
            except Exception as exc:
                logger.warning("FishPro 释放按键失败: key=%s error=%s", key, exc)

    def release_held(self, state: ControlState, reason: str = "release") -> str:
        if not state.held_direction:
            state.pulse_release_time = 0.0
            state.pulse_direction = ""
            return reason

        key = DIRECTION_KEYS[state.held_direction]
        try:
            self._adapter.key_up(key)
        except Exception as exc:
            from utils.logger import logger

            logger.warning("FishPro keyUp 失败: dir=%s error=%s", state.held_direction, exc)
        finally:
            state.held_direction = ""
            state.hold_started_at = 0.0
            state.pulse_release_time = 0.0
            state.pulse_direction = ""
        return reason

    def press_direction(self, direction: str, state: ControlState) -> str:
        if state.held_direction == direction:
            return f"hold_{direction}"

        now = self._clock()
        if state.held_direction:
            self.release_held(state, reason=f"switch_to_{direction}")

        try:
            self._adapter.key_down(DIRECTION_KEYS[direction])
        except Exception as exc:
            from utils.logger import logger

            logger.warning("FishPro keyDown 失败: dir=%s error=%s", direction, exc)
            state.held_direction = ""
            state.hold_started_at = 0.0
            state.pulse_release_time = 0.0
            state.pulse_direction = ""
            return f"error_{direction}"

        state.held_direction = direction
        state.hold_started_at = now
        state.last_switch_time = now
        return f"hold_{direction}"

    # ---- 决策辅助 ----

    def choose_direction(
        self, final_action: float, observation: Observation, state: ControlState
    ) -> str:
        """动作强度到方向的死区判定，含中心区死区放大与切换保护。"""
        config = self._config
        if state.center_silence_active or observation.in_center_no_move:
            return ""

        abs_action = abs(final_action)
        deadzone = config.action_deadzone

        center_reentry_px = dynamic_center_reentry_px(
            observation.target_width, config
        )
        if (
            observation.inside_target
            and abs(observation.cursor_x - observation.target_center_x)
            <= center_reentry_px
        ):
            deadzone *= config.action_release_multiplier
            deadzone *= config.center_deadzone_multiplier

        if abs_action <= deadzone:
            return ""

        desired_direction = "right" if final_action > 0 else "left"
        held_direction = state.held_direction
        if held_direction and desired_direction != held_direction:
            since_hold = self._clock() - state.hold_started_at
            switch_limit = max(
                float(config.far_error_px), observation.target_width * 0.6
            )
            if (
                since_hold < config.switch_hold_time
                and abs(observation.error_px) < switch_limit
            ):
                return held_direction

        return desired_direction

    def is_urgent(self, observation: Observation) -> bool:
        config = self._config
        return (
            not observation.inside_target
            or abs(observation.error_px) >= config.recovery_fast_error_px
            or min(observation.edge_margin_px, observation.predicted_edge_margin_px)
            <= config.edge_recovery_margin_px
        )

    def next_control_interval(self, urgent: bool = False) -> float:
        config = self._config
        if urgent:
            return self._rng.uniform(
                config.urgent_control_interval_min,
                config.urgent_control_interval_max,
            )
        return self._rng.uniform(
            config.control_interval_min, config.control_interval_max
        )

    def pulse_press_duration(
        self, final_action: float, observation: Observation
    ) -> float:
        """按动作强度插值脉冲时长，框内使用更短区间并在安全时再缩短。"""
        config = self._config
        abs_action = clamp_float(abs(final_action), 0.0, 1.0)
        normalized = clamp_float(
            (abs_action - config.action_deadzone)
            / max(1e-4, config.pulse_long_hold_action - config.action_deadzone),
            0.0,
            1.0,
        )
        if observation.inside_target:
            min_press = config.inside_pulse_min_press_sec
            max_press = config.inside_pulse_max_press_sec
        else:
            min_press = config.pulse_min_press_sec
            max_press = config.pulse_max_press_sec

        duration = min_press + normalized * (max_press - min_press)
        if (
            observation.inside_target
            and observation.edge_margin_px >= config.edge_recovery_margin_px
        ):
            duration *= 0.70
        if config.pulse_jitter_sec > 0:
            duration += self._rng.uniform(
                -config.pulse_jitter_sec, config.pulse_jitter_sec
            )
        return clamp_float(duration, min_press, max_press)

    def should_use_long_hold(
        self, final_action: float, observation: Observation
    ) -> bool:
        """长按判定：框内仅极端危险时允许，其余情况一律脉冲。"""
        config = self._config
        if observation.inside_target:
            moving_away = (
                observation.error_px > 0 and observation.relative_vx > 0
            ) or (observation.error_px < 0 and observation.relative_vx < 0)
            very_near_edge = observation.edge_margin_px <= max(
                4.0, config.edge_recovery_margin_px * 0.45
            )
            predicted_danger = observation.predicted_edge_margin_px <= 0.0
            large_error = (
                abs(observation.error_px) >= observation.target_width * 0.55
            )
            if not (
                very_near_edge and predicted_danger and moving_away and large_error
            ):
                return False

        return (
            abs(final_action) >= config.pulse_long_hold_action
            or abs(observation.error_px) >= config.recovery_hard_error_px
        )

    # ---- 主状态机 ----

    def apply(
        self, observation: Observation, final_action: float, state: ControlState
    ) -> str:
        config = self._config
        now = self._clock()
        desired_direction = self.choose_direction(final_action, observation, state)
        held_direction = state.held_direction
        urgent = self.is_urgent(observation)
        long_hold = self.should_use_long_hold(final_action, observation)

        # 脉冲到期：仍需同方向且紧急时延长按压，否则松开并进入释放节奏。
        if (
            state.held_direction
            and state.pulse_release_time > 0.0
            and now >= state.pulse_release_time
        ):
            near_center = observation.inside_target and abs(
                observation.cursor_x - observation.target_center_x
            ) <= dynamic_center_reentry_px(observation.target_width, config)
            if (
                desired_direction
                and held_direction == desired_direction
                and not observation.inside_target
                and (urgent or long_hold)
                and not near_center
            ):
                state.pulse_release_time = 0.0
                state.pulse_direction = ""
                state.next_control_time = now + self.next_control_interval(urgent)
                return f"extend_{state.held_direction}"

            finished_direction = state.held_direction
            self.release_held(state, reason="pulse_done")
            state.next_control_time = now + self._rng.uniform(
                config.pulse_release_min_sec, config.pulse_release_max_sec
            )
            return f"pulse_done_{finished_direction}"

        if not desired_direction:
            if state.held_direction and now - state.hold_started_at >= config.min_hold_time:
                state.last_move_direction = ""
                if state.center_silence_active:
                    state.active_mode = "silent"
                state.next_control_time = now + self.next_control_interval(False)
                return self.release_held(state, reason="deadzone")
            if state.held_direction:
                return f"keep_{state.held_direction}"
            state.last_move_direction = ""
            if state.center_silence_active:
                state.active_mode = "silent"
            return "idle"

        if now < state.next_control_time and not urgent:
            if held_direction == desired_direction:
                return f"keep_{state.held_direction}"
            if state.held_direction:
                return f"hold_wait_{state.held_direction}"
            return "wait_human_cadence"

        if (
            config.hesitation_chance > 0
            and not urgent
            and self._rng.random() < config.hesitation_chance
        ):
            delay = self._rng.uniform(
                config.hesitation_delay_min, config.hesitation_delay_max
            )
            if delay > 0:
                self._sleep(delay)
            state.next_control_time = self._clock() + self.next_control_interval(
                False
            )
            return "hesitate"

        reaction_delay = self._rng.uniform(
            config.reaction_delay_min, config.reaction_delay_max
        )
        if urgent:
            reaction_delay *= config.urgent_reaction_scale
        if reaction_delay > 0:
            self._sleep(reaction_delay)
            now = self._clock()

        if held_direction == desired_direction:
            state.last_move_direction = desired_direction
            state.last_move_time = now
            if long_hold:
                state.pulse_release_time = 0.0
                state.pulse_direction = ""
                state.next_control_time = now + self.next_control_interval(urgent)
                return f"keep_{state.held_direction}"

            if state.pulse_release_time <= 0.0:
                state.pulse_direction = desired_direction
                state.pulse_release_time = now + self.pulse_press_duration(
                    final_action, observation
                )
            state.next_control_time = now + self.next_control_interval(False)
            return f"pulse_keep_{state.held_direction}"

        action = self.press_direction(desired_direction, state)
        state.last_move_direction = desired_direction
        state.last_move_time = now

        if long_hold:
            state.pulse_release_time = 0.0
            state.pulse_direction = ""
            state.next_control_time = now + self.next_control_interval(urgent)
        else:
            state.pulse_direction = desired_direction
            state.pulse_release_time = now + self.pulse_press_duration(
                final_action, observation
            )
            state.next_control_time = now + self.next_control_interval(False)

        return action
