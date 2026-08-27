"""FishPro 控条的参数定义与解析。

参数默认值全部来自附件 ``autofish.py`` 的 ``FishingConfig``（已采用其
参数优化后的取值），仅将窗口枚举、热键、GUI 相关字段替换为 MaaNTE 的
运行方式。所有字段都可以通过 Pipeline 的 ``custom_action_param`` 覆盖，
非法值回退默认并做范围钳制。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from typing import Any


@dataclass
class FishProConfig:
    """FishPro 控条的完整参数集。"""

    # ---- ROI ----
    # 默认使用 1280x720 基准像素 ROI，运行时经 screen.map_rect 归一化。
    roi_px: tuple[int, int, int, int] = (384, 21, 512, 58)
    # 当 use_ratio_roi 为真时改用比例 ROI（附件原始配置），用于兜底。
    use_ratio_roi: bool = False
    roi_left_ratio: float = 0.30
    roi_top_ratio: float = 0.03
    roi_right_ratio: float = 0.70
    roi_bottom_ratio: float = 0.11

    # ---- 绿色目标识别约束 ----
    green_min_hsv: tuple[int, int, int] = (42, 110, 110)
    green_max_hsv: tuple[int, int, int] = (92, 255, 255)
    green_min_area: int = 90
    green_min_width: int = 36
    green_min_height: int = 4
    green_max_height_ratio: float = 0.42
    green_max_area_ratio: float = 0.22
    green_min_aspect_ratio: float = 3.2
    green_max_y_ratio: float = 0.88
    green_min_fill_ratio: float = 0.45
    # 光标压在绿条上会把掩码切成两段，用水平闭运算桥接窄缝后再做形状过滤。
    # 取值需大于光标宽度（yellow_max_width）并留出余量。
    green_bridge_px: int = 21

    # ---- 黄线识别约束（兼顾黄昏/偏暗场景） ----
    yellow_primary_min_hsv: tuple[int, int, int] = (20, 110, 150)
    yellow_primary_max_hsv: tuple[int, int, int] = (38, 255, 255)
    yellow_dusk_min_hsv: tuple[int, int, int] = (16, 70, 100)
    yellow_dusk_max_hsv: tuple[int, int, int] = (42, 255, 255)
    yellow_min_area: int = 9
    yellow_min_height: int = 7
    yellow_max_width: int = 14
    yellow_min_aspect_ratio: float = 2.2
    yellow_min_fill_ratio: float = 0.40
    yellow_blue_gap_min: int = 44
    yellow_rg_diff_max: int = 42
    yellow_green_floor: int = 95
    yellow_red_floor: int = 110
    yellow_red_blue_gap: int = 32
    yellow_green_blue_gap: int = 26
    yellow_track_bias_px: int = 18
    yellow_target_y_tolerance_px: int = 8
    yellow_target_x_margin_px: int = 80
    yellow_target_x_margin_ratio: float = 1.6

    # ---- 中心对准、动态观测与防抖 ----
    center_tolerance_px: int = 5
    center_tolerance_jitter_px: int = 0
    hold_hysteresis_px: int = 5
    reverse_confirm_px: int = 6
    reverse_direction_cooldown: float = 0.065
    anti_shake_error_px: int = 14
    cursor_smooth_alpha: float = 0.50
    velocity_smooth_alpha: float = 0.35
    center_no_move_px: int = 8
    center_reentry_px: int = 22
    center_release_px: int = 10
    center_hold_release_px: int = 14
    center_deadzone_multiplier: float = 2.35
    center_no_move_width_ratio: float = 0.22
    center_reentry_width_ratio: float = 0.42
    center_release_width_ratio: float = 0.22
    safe_margin_px: int = 9
    safe_margin_width_ratio: float = 0.13
    predictive_lookahead_sec: float = 0.040
    predictive_brake_px: float = 0.90
    target_predictive_lookahead_sec: float = 0.065
    target_smooth_alpha: float = 0.45
    target_velocity_smooth_alpha: float = 0.35
    target_jump_max_px: int = 36
    target_width_jump_ratio: float = 0.35
    target_width_jump_min_px: float = 18.0
    target_jump_alpha_scale: float = 0.25
    target_width_confirm_frames: int = 2
    moving_target_speed_px: float = 170.0
    moving_safe_margin_boost_px: int = 10
    relative_overshoot_lookahead_sec: float = 0.070
    relative_away_gain: float = 0.18
    relative_toward_gain: float = 0.30
    cursor_jump_max_px: int = 26
    cursor_jump_alpha_scale: float = 0.30
    cursor_max_velocity_px: float = 1800.0

    # ---- 强拉回调优层 ----
    edge_recovery_margin_px: int = 10
    edge_recovery_boost: float = 0.50
    edge_recovery_min_action: float = 0.28
    outside_recovery_boost: float = 0.55
    recovery_fast_error_px: int = 38
    recovery_hard_error_px: int = 58
    recovery_fast_min_action: float = 0.58
    recovery_hard_min_action: float = 0.86
    recovery_velocity_gain: float = 0.12

    # ---- 智能长按控制 ----
    far_error_px: int = 64
    action_deadzone: float = 0.16
    action_release_multiplier: float = 2.6
    action_hold_boost: float = 0.06
    action_smooth_alpha: float = 0.60
    velocity_away_gain: float = 0.14
    velocity_toward_gain: float = 0.22
    inside_action_cap: float = 0.16
    inside_edge_action_cap: float = 0.32
    inside_edge_min_action: float = 0.20
    outside_action_cap: float = 1.0
    outside_approach_error_ratio: float = 0.35
    outside_approach_edge_px: float = 1.0
    outside_approach_action_cap: float = 0.35
    outside_approach_strength_scale: float = 0.70
    wide_target_width_px: float = 120.0
    wide_target_inside_action_scale: float = 1.35
    wide_target_approach_action_scale: float = 1.10
    min_hold_time: float = 0.010
    switch_hold_time: float = 0.045

    # ---- 真人化节奏控制 ----
    control_interval_min: float = 0.014
    control_interval_max: float = 0.026
    urgent_control_interval_min: float = 0.004
    urgent_control_interval_max: float = 0.010
    pulse_long_hold_action: float = 0.80
    pulse_min_press_sec: float = 0.008
    pulse_max_press_sec: float = 0.070
    inside_pulse_min_press_sec: float = 0.004
    inside_pulse_max_press_sec: float = 0.024
    pulse_release_min_sec: float = 0.008
    pulse_release_max_sec: float = 0.032
    pulse_jitter_sec: float = 0.005

    # ---- 丢帧恢复调优层 ----
    missing_cursor_grace_sec: float = 0.075
    missing_target_grace_sec: float = 0.050
    missing_action_decay: float = 0.72
    missing_target_decay: float = 0.58
    missing_release_action: float = 0.08

    # ---- 循环节奏 ----
    loop_delay_min: float = 0.002
    loop_delay_max: float = 0.004
    reaction_delay_min: float = 0.000
    reaction_delay_max: float = 0.001
    urgent_reaction_scale: float = 0.35
    hesitation_chance: float = 0.0
    hesitation_delay_min: float = 0.006
    hesitation_delay_max: float = 0.012

    # ---- 输出限频与调试 ----
    control_log_interval_sec: float = 0.12
    debug_frame_interval_sec: float = 0.050
    debug_enabled: bool = False
    debug_frame_limit: int = 200

    # ---- 会话生命周期（替代附件的热键控制） ----
    control_end_grace_ms: float = 300.0
    lost_abort_ms: float = 1500.0
    session_timeout_ms: float = 60000.0

    # ---- 学习 / 持久化 ----
    learning_enabled: bool = False
    learning_replay_history: bool = False
    learning_data_name: str = "samples.jsonl"
    learning_model_name: str = "policy.npz"
    learning_history_replay_limit: int = 2500
    learning_buffer_flush_threshold: int = 32
    learning_online_lr: float = 0.010
    learning_weight_decay: float = 0.0002
    learning_reward_ema_alpha: float = 0.025
    learning_residual_limit: float = 0.25
    learning_negative_ema_min_samples: int = 1000
    learning_negative_ema_residual_scale: float = 0.20
    learning_exploration_noise: float = 0.015
    learning_outside_exploration_scale: float = 1.20
    learning_progress_weight: float = 1.25
    learning_inside_bonus: float = 0.28
    learning_center_bonus: float = 0.12
    learning_edge_penalty_weight: float = 0.18
    learning_outside_penalty: float = 0.36
    learning_action_smooth_penalty: float = 0.06


# 需要限制在 [0, 1] 的比例类字段。
_UNIT_RANGE_FIELDS = frozenset(
    {
        "roi_left_ratio",
        "roi_top_ratio",
        "roi_right_ratio",
        "roi_bottom_ratio",
        "green_max_height_ratio",
        "green_max_area_ratio",
        "green_max_y_ratio",
        "green_min_fill_ratio",
        "yellow_min_fill_ratio",
        "cursor_smooth_alpha",
        "velocity_smooth_alpha",
        "target_smooth_alpha",
        "target_velocity_smooth_alpha",
        "target_jump_alpha_scale",
        "cursor_jump_alpha_scale",
        "target_width_jump_ratio",
        "action_smooth_alpha",
        "outside_approach_error_ratio",
        "outside_approach_strength_scale",
        "hesitation_chance",
        "learning_reward_ema_alpha",
        "learning_negative_ema_residual_scale",
        "center_no_move_width_ratio",
        "center_reentry_width_ratio",
        "center_release_width_ratio",
        "safe_margin_width_ratio",
    }
)

# 允许为负的字段（其余数值字段一律钳到非负）。
_SIGNED_FIELDS = frozenset(
    {
        "outside_approach_edge_px",
        "reaction_delay_min",
    }
)

# 不参与数值钳制的字段。
_STRING_FIELDS = frozenset({"learning_data_name", "learning_model_name"})
_BOOL_FIELDS = frozenset(
    {
        "use_ratio_roi",
        "debug_enabled",
        "learning_enabled",
        "learning_replay_history",
    }
)
_HSV_FIELDS = frozenset(
    {
        "green_min_hsv",
        "green_max_hsv",
        "yellow_primary_min_hsv",
        "yellow_primary_max_hsv",
        "yellow_dusk_min_hsv",
        "yellow_dusk_max_hsv",
    }
)


def load_custom_action_params(custom_action_param: Any) -> dict:
    """把 CustomAction 参数统一解析为字典。"""
    if not custom_action_param:
        return {}
    if isinstance(custom_action_param, dict):
        return custom_action_param
    try:
        parsed = json.loads(custom_action_param)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on", "y"):
            return True
        if normalized in ("0", "false", "no", "off", "n"):
            return False
    return default


def _parse_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _parse_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return int(round(parsed))


def _parse_int_tuple(value: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != len(default):
        return default
    parsed: list[int] = []
    for item, fallback in zip(value, default):
        parsed.append(_parse_int(item, fallback))
    return tuple(parsed)


def _clamp_numeric(name: str, value: float) -> float:
    if name in _UNIT_RANGE_FIELDS:
        return max(0.0, min(1.0, value))
    if name in _SIGNED_FIELDS:
        return value
    return max(0.0, value)


def load_fish_pro_config(custom_action_param: Any) -> FishProConfig:
    """逐字段解析并钳制参数，非法值回退默认。"""
    params = load_custom_action_params(custom_action_param)
    config = FishProConfig()
    if not params:
        return _post_validate(config)

    for field in fields(FishProConfig):
        name = field.name
        if name not in params:
            continue
        raw = params[name]
        default = getattr(config, name)

        if name in _BOOL_FIELDS:
            setattr(config, name, _parse_bool(raw, bool(default)))
            continue
        if name in _STRING_FIELDS:
            if isinstance(raw, str) and raw.strip():
                setattr(config, name, raw.strip())
            continue
        if name == "roi_px":
            parsed_roi = _parse_int_tuple(raw, tuple(default))
            if parsed_roi[2] > 0 and parsed_roi[3] > 0:
                setattr(config, name, parsed_roi)
            continue
        if name in _HSV_FIELDS:
            parsed_hsv = _parse_int_tuple(raw, tuple(default))
            setattr(
                config,
                name,
                tuple(max(0, min(255, item)) for item in parsed_hsv),
            )
            continue
        if isinstance(default, int) and not isinstance(default, bool):
            setattr(
                config,
                name,
                int(_clamp_numeric(name, float(_parse_int(raw, default)))),
            )
            continue
        if isinstance(default, float):
            setattr(config, name, _clamp_numeric(name, _parse_float(raw, default)))
            continue

    return _post_validate(config)


def _post_validate(config: FishProConfig) -> FishProConfig:
    """修正字段之间的相对关系，保证下游算法不出现负区间。"""
    config.green_min_height = max(1, config.green_min_height)
    config.green_min_width = max(1, config.green_min_width)
    config.green_min_area = max(1, config.green_min_area)
    config.yellow_min_area = max(1, config.yellow_min_area)
    config.yellow_min_height = max(1, config.yellow_min_height)
    config.yellow_max_width = max(1, config.yellow_max_width)
    config.target_width_confirm_frames = max(1, config.target_width_confirm_frames)

    if config.roi_right_ratio <= config.roi_left_ratio:
        config.roi_left_ratio = FishProConfig.roi_left_ratio
        config.roi_right_ratio = FishProConfig.roi_right_ratio
    if config.roi_bottom_ratio <= config.roi_top_ratio:
        config.roi_top_ratio = FishProConfig.roi_top_ratio
        config.roi_bottom_ratio = FishProConfig.roi_bottom_ratio

    config.control_interval_max = max(
        config.control_interval_min, config.control_interval_max
    )
    config.urgent_control_interval_max = max(
        config.urgent_control_interval_min, config.urgent_control_interval_max
    )
    config.pulse_max_press_sec = max(
        config.pulse_min_press_sec, config.pulse_max_press_sec
    )
    config.inside_pulse_max_press_sec = max(
        config.inside_pulse_min_press_sec, config.inside_pulse_max_press_sec
    )
    config.pulse_release_max_sec = max(
        config.pulse_release_min_sec, config.pulse_release_max_sec
    )
    config.loop_delay_max = max(config.loop_delay_min, config.loop_delay_max)
    config.reaction_delay_max = max(
        config.reaction_delay_min, config.reaction_delay_max
    )
    config.hesitation_delay_max = max(
        config.hesitation_delay_min, config.hesitation_delay_max
    )
    config.learning_residual_limit = max(1e-4, config.learning_residual_limit)
    config.learning_buffer_flush_threshold = max(
        1, config.learning_buffer_flush_threshold
    )
    config.learning_history_replay_limit = max(
        1, config.learning_history_replay_limit
    )
    config.debug_frame_limit = max(0, config.debug_frame_limit)
    return config
