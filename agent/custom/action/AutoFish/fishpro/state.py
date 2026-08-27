"""FishPro 控条的状态估计层。

移植附件 ``autofish.py`` 的 ``ControlState`` / ``Observation`` /
``update_cursor_state`` / ``build_observation``：

- 光标位置与速度 EWMA，含跳变限制与最大速度钳制；
- 绿条中心与宽度平滑，含中心跳变、宽度跳变检测与宽度多帧确认；
- 相对速度、双前瞻预测、制动偏置；
- 误差、预测误差、归一化误差、框内判定、边缘余量与中心静默判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import FishProConfig
from .thresholds import (
    center_edge_margin_px,
    clamp_float,
    dynamic_center_no_move_px,
    effective_safe_margin_px,
)
from .vision import Detection

DIRECTION_LEFT = "left"
DIRECTION_RIGHT = "right"


@dataclass
class Observation:
    """单帧派生观测量，供规则层与学习层共同使用。"""

    timestamp: float
    cursor_x: float
    cursor_vx: float
    predicted_cursor_x: float
    target_center_x: float
    target_vx: float
    predicted_target_center_x: float
    target_left: float
    target_right: float
    target_width: float
    control_target_x: float
    error_px: float
    predicted_error_px: float
    normalized_error: float
    inside_target: bool
    edge_margin_px: float
    predicted_edge_margin_px: float
    relative_vx: float
    in_center_no_move: bool
    held_direction: str
    last_action: float
    last_rule_action: float


@dataclass
class ControlState:
    """一次控条会话内的全部可变状态。"""

    # 光标平滑
    smoothed_cursor_x: float = 0.0
    has_smoothed_cursor: bool = False
    last_cursor_x: float = 0.0
    last_cursor_time: float = 0.0
    smoothed_cursor_velocity: float = 0.0

    # 绿条平滑
    smoothed_target_center_x: float = 0.0
    has_smoothed_target: bool = False
    last_target_center_x: float = 0.0
    last_target_width: float = 0.0
    last_target_time: float = 0.0
    smoothed_target_velocity: float = 0.0
    confirmed_target_width: float = 0.0
    width_candidate: Optional[float] = None
    width_candidate_hits: int = 0

    # 按键与节奏
    held_direction: str = ""
    hold_started_at: float = 0.0
    last_switch_time: float = 0.0
    last_move_direction: str = ""
    last_move_time: float = 0.0
    next_control_time: float = 0.0
    pulse_release_time: float = 0.0
    pulse_direction: str = ""

    # 动作
    last_rule_action: float = 0.0
    last_residual_action: float = 0.0
    last_final_action: float = 0.0
    smoothed_action: float = 0.0
    center_silence_active: bool = False
    active_mode: str = "idle"

    # 日志与丢帧
    last_logged_status: str = ""
    last_log_time: float = 0.0
    last_valid_observation_time: float = 0.0
    cursor_missing_since: float = 0.0
    target_missing_since: float = 0.0
    last_observation: Optional[Observation] = None

    # 学习
    learning_enabled: bool = False
    learning_pending: Optional[dict] = None
    learning_total_reward: float = 0.0
    learning_last_reward: float = 0.0
    learning_last_loss: float = 0.0
    learning_sample_count: int = 0
    learning_update_count: int = 0

    # 调试
    last_debug_frame_time: float = 0.0
    debug_frame_count: int = 0
    stats: dict = field(default_factory=dict)


def reset_control_state(state: ControlState) -> None:
    """重置全部瞬时状态，保留学习开关与统计。"""
    learning_enabled = state.learning_enabled
    stats = state.stats
    fresh = ControlState()
    for name, value in vars(fresh).items():
        setattr(state, name, value)
    state.learning_enabled = learning_enabled
    state.stats = stats


def update_cursor_state(
    cursor: Detection,
    state: ControlState,
    config: FishProConfig,
    now: float,
) -> tuple[float, float]:
    """更新光标平滑位置与平滑速度，返回 ``(平滑位置, 平滑速度)``。

    单帧跳点通常来自识别误选，位置仍然更新但速度不跟随跳变。
    """
    raw_center_x = float(cursor.center_x)

    if not state.has_smoothed_cursor:
        smoothed = raw_center_x
    else:
        alpha = config.cursor_smooth_alpha
        delta_from_last = raw_center_x - state.last_cursor_x
        if abs(delta_from_last) > config.cursor_jump_max_px:
            alpha *= config.cursor_jump_alpha_scale
        smoothed = state.smoothed_cursor_x * (1.0 - alpha) + raw_center_x * alpha

    velocity = 0.0
    if state.last_cursor_time > 0:
        dt = max(1e-4, now - state.last_cursor_time)
        delta_x = raw_center_x - state.last_cursor_x
        if abs(delta_x) > config.cursor_jump_max_px:
            velocity = state.smoothed_cursor_velocity
        else:
            velocity = delta_x / dt
        velocity = clamp_float(
            velocity,
            -config.cursor_max_velocity_px,
            config.cursor_max_velocity_px,
        )

    if state.last_cursor_time <= 0:
        smoothed_velocity = velocity
    else:
        velocity_alpha = config.velocity_smooth_alpha
        smoothed_velocity = (
            state.smoothed_cursor_velocity * (1.0 - velocity_alpha)
            + velocity * velocity_alpha
        )
        smoothed_velocity = clamp_float(
            smoothed_velocity,
            -config.cursor_max_velocity_px,
            config.cursor_max_velocity_px,
        )

    state.smoothed_cursor_x = smoothed
    state.has_smoothed_cursor = True
    state.last_cursor_x = raw_center_x
    state.last_cursor_time = now
    state.smoothed_cursor_velocity = smoothed_velocity
    return smoothed, smoothed_velocity


def _confirm_target_width(
    raw_width: float, state: ControlState, config: FishProConfig
) -> float:
    """宽度多帧确认：小幅变化平滑跟随，大幅跳变需连续命中才采纳。"""
    if state.confirmed_target_width <= 0.0:
        state.confirmed_target_width = raw_width
        state.width_candidate = None
        state.width_candidate_hits = 0
        return state.confirmed_target_width

    jump_threshold = max(
        config.target_width_jump_min_px,
        state.confirmed_target_width * config.target_width_jump_ratio,
    )
    delta = abs(raw_width - state.confirmed_target_width)

    if delta < jump_threshold:
        state.confirmed_target_width += (
            raw_width - state.confirmed_target_width
        ) * 0.30
        state.width_candidate = None
        state.width_candidate_hits = 0
        return state.confirmed_target_width

    if (
        state.width_candidate is not None
        and abs(raw_width - state.width_candidate) < jump_threshold * 0.5
    ):
        state.width_candidate_hits += 1
    else:
        state.width_candidate = raw_width
        state.width_candidate_hits = 1

    if state.width_candidate_hits >= config.target_width_confirm_frames:
        state.confirmed_target_width = float(state.width_candidate)
        state.width_candidate = None
        state.width_candidate_hits = 0

    return state.confirmed_target_width


def build_observation(
    target: Detection,
    cursor_x: float,
    cursor_vx: float,
    state: ControlState,
    config: FishProConfig,
    timestamp: float,
) -> Observation:
    """更新绿条状态并构造完整观测量。"""
    target_left = float(target.left)
    target_right = float(target.right)
    raw_target_center_x = float(target.center_x)
    raw_target_width = max(1.0, target_right - target_left)

    if not state.has_smoothed_target:
        target_center_x = raw_target_center_x
        target_vx = 0.0
    else:
        alpha = config.target_smooth_alpha
        delta_from_last = raw_target_center_x - state.last_target_center_x
        width_delta = abs(raw_target_width - state.last_target_width)
        width_jump = state.last_target_width > 0.0 and width_delta > max(
            config.target_width_jump_min_px,
            state.last_target_width * config.target_width_jump_ratio,
        )
        center_jump = abs(delta_from_last) > config.target_jump_max_px
        if center_jump or width_jump:
            alpha *= config.target_jump_alpha_scale
        target_center_x = (
            state.smoothed_target_center_x * (1.0 - alpha)
            + raw_target_center_x * alpha
        )

        dt = max(1e-4, timestamp - state.last_target_time)
        if center_jump or width_jump:
            instant_target_vx = state.smoothed_target_velocity
        else:
            instant_target_vx = delta_from_last / dt
        target_vx = (
            state.smoothed_target_velocity
            * (1.0 - config.target_velocity_smooth_alpha)
            + instant_target_vx * config.target_velocity_smooth_alpha
        )
        target_vx = clamp_float(
            target_vx,
            -config.cursor_max_velocity_px,
            config.cursor_max_velocity_px,
        )

    target_width = _confirm_target_width(raw_target_width, state, config)

    state.smoothed_target_center_x = target_center_x
    state.has_smoothed_target = True
    state.last_target_center_x = raw_target_center_x
    state.last_target_width = raw_target_width
    state.last_target_time = timestamp
    state.smoothed_target_velocity = target_vx

    relative_vx = cursor_vx - target_vx
    predicted_target_center_x = (
        target_center_x + target_vx * config.target_predictive_lookahead_sec
    )
    predicted_target_left = predicted_target_center_x - target_width * 0.5
    predicted_target_right = predicted_target_center_x + target_width * 0.5

    predicted_cursor_x = (
        cursor_x + relative_vx * config.relative_overshoot_lookahead_sec
    )
    max_prediction_offset = target_width * 1.2
    predicted_cursor_x = clamp_float(
        predicted_cursor_x,
        predicted_target_center_x - max_prediction_offset,
        predicted_target_center_x + max_prediction_offset,
    )

    brake_bias = clamp_float(
        -relative_vx * config.predictive_lookahead_sec,
        -target_width * config.predictive_brake_px,
        target_width * config.predictive_brake_px,
    )
    control_target_x = predicted_target_center_x + brake_bias

    error_px = cursor_x - control_target_x
    predicted_error_px = predicted_cursor_x - control_target_x
    normalized_error = clamp_float(
        error_px / max(1.0, target_width * 0.5), -1.5, 1.5
    )
    inside_target = target_left <= cursor_x <= target_right
    edge_margin_px = min(cursor_x - target_left, target_right - cursor_x)
    predicted_edge_margin_px = min(
        predicted_cursor_x - predicted_target_left,
        predicted_target_right - predicted_cursor_x,
    )

    safe_margin_px = effective_safe_margin_px(target_width, target_vx, config)
    effective_edge_margin_px = min(edge_margin_px, predicted_edge_margin_px)
    center_edge_px = center_edge_margin_px(safe_margin_px, config)
    in_center_no_move = inside_target and (
        effective_edge_margin_px >= safe_margin_px
        or (
            effective_edge_margin_px >= center_edge_px
            and abs(cursor_x - target_center_x)
            <= dynamic_center_no_move_px(target_width, config)
        )
    )

    return Observation(
        timestamp=timestamp,
        cursor_x=cursor_x,
        cursor_vx=cursor_vx,
        predicted_cursor_x=predicted_cursor_x,
        target_center_x=target_center_x,
        target_vx=target_vx,
        predicted_target_center_x=predicted_target_center_x,
        target_left=target_left,
        target_right=target_right,
        target_width=target_width,
        control_target_x=control_target_x,
        error_px=error_px,
        predicted_error_px=predicted_error_px,
        normalized_error=normalized_error,
        inside_target=inside_target,
        edge_margin_px=edge_margin_px,
        predicted_edge_margin_px=predicted_edge_margin_px,
        relative_vx=relative_vx,
        in_center_no_move=in_center_no_move,
        held_direction=state.held_direction,
        last_action=state.last_final_action,
        last_rule_action=state.last_rule_action,
    )


def clear_missing_tracking(state: ControlState) -> None:
    state.cursor_missing_since = 0.0
    state.target_missing_since = 0.0


def mark_missing_state(
    state: ControlState,
    now: float,
    *,
    target_missing: bool = False,
    cursor_missing: bool = False,
) -> None:
    if target_missing and state.target_missing_since <= 0.0:
        state.target_missing_since = now
    if cursor_missing and state.cursor_missing_since <= 0.0:
        state.cursor_missing_since = now


def can_recover_from_missing(
    state: ControlState,
    config: FishProConfig,
    now: float,
    *,
    target_missing: bool = False,
    cursor_missing: bool = False,
) -> bool:
    """判断丢帧是否仍在宽限时间内，可以按上次观测继续输出。"""
    if state.last_observation is None or state.last_valid_observation_time <= 0.0:
        return False

    elapsed = now - state.last_valid_observation_time
    if target_missing and elapsed > config.missing_target_grace_sec:
        return False
    if cursor_missing and elapsed > config.missing_cursor_grace_sec:
        return False
    return True


def missing_fallback_action(
    state: ControlState, config: FishProConfig, *, target_missing: bool
) -> float:
    """丢帧衰减：按上次动作衰减输出，低于死区归零。"""
    decay = (
        config.missing_target_decay if target_missing else config.missing_action_decay
    )
    fallback = clamp_float(state.last_final_action * decay, -0.85, 0.85)
    if abs(fallback) < max(config.action_deadzone, config.missing_release_action):
        return 0.0
    return fallback
