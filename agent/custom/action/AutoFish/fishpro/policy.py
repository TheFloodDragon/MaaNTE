"""FishPro 控条的规则控制层。

移植附件 ``autofish.py`` 的 ``compute_rule_action`` /
``action_cap_for_observation`` / ``merge_policy_action``：

- 目标宽度自适应阈值、安全区静默与滞回；
- 反向切换确认与方向冷却；
- 强度曲线分段、速度增益与主动制动；
- 框外恢复、接近降档、边缘恢复；
- 宽目标与移动目标补偿；
- 动作平滑 EWMA 与按观测动态计算的动作上限钳制。

本层为纯函数 + ``ControlState`` 副作用，不涉及按键与截图。
"""

from __future__ import annotations

import random
from typing import Optional

from .config import FishProConfig
from .state import ControlState, Observation
from .thresholds import (
    center_edge_margin_px,
    clamp_float,
    clamp_int,
    dynamic_center_no_move_px,
    dynamic_center_reentry_px,
    effective_safe_margin_px,
)


def action_cap_for_observation(
    observation: Observation, config: FishProConfig
) -> float:
    """按观测状态计算动作强度上限。"""
    predicted_edge_margin_px = observation.predicted_edge_margin_px

    if not observation.inside_target:
        approaching_target = (
            predicted_edge_margin_px >= config.outside_approach_edge_px
            or abs(observation.predicted_error_px)
            <= observation.target_width * config.outside_approach_error_ratio
        )
        if approaching_target:
            cap = config.outside_approach_action_cap
            if observation.target_width >= config.wide_target_width_px:
                cap *= config.wide_target_approach_action_scale
            return cap
        return config.outside_action_cap

    if (
        min(observation.edge_margin_px, predicted_edge_margin_px)
        <= config.edge_recovery_margin_px
    ):
        cap = config.inside_edge_action_cap
    else:
        cap = config.inside_action_cap
    if observation.target_width >= config.wide_target_width_px:
        cap *= config.wide_target_inside_action_scale
    return cap


def compute_rule_action(
    observation: Observation,
    state: ControlState,
    config: FishProConfig,
    now: float,
    rng: Optional[random.Random] = None,
) -> float:
    """计算规则层动作强度，正值向右（D），负值向左（A）。"""
    rng = rng or random
    center_error = observation.cursor_x - observation.target_center_x
    abs_center_error = abs(center_error)
    target_width = max(1.0, observation.target_width)
    center_no_move_px = dynamic_center_no_move_px(target_width, config)
    center_reentry_px = dynamic_center_reentry_px(target_width, config)
    safe_margin_px = effective_safe_margin_px(
        target_width, observation.target_vx, config
    )
    center_edge_px = center_edge_margin_px(safe_margin_px, config)

    # 安全区判定优先看当前位置，预测仅在接近边缘时作为补充依据，
    # 避免「当前与预测都必须安全」的过严条件导致过度干预。
    if observation.inside_target:
        if observation.edge_margin_px >= safe_margin_px:
            state.center_silence_active = True
            state.active_mode = "safe"
            return 0.0
        if (
            observation.edge_margin_px >= safe_margin_px * 0.6
            and observation.predicted_edge_margin_px >= safe_margin_px
        ):
            state.center_silence_active = True
            state.active_mode = "safe"
            return 0.0

    # 安全区静默的边缘滞回：没有明显危险前不重新介入，减少框内来回抖动。
    if (
        state.center_silence_active
        and observation.inside_target
        and observation.edge_margin_px >= center_edge_px
    ):
        state.active_mode = "safe"
        return 0.0

    if (
        abs_center_error <= center_no_move_px
        and observation.inside_target
        and observation.edge_margin_px >= center_edge_px
    ):
        state.center_silence_active = True
        state.active_mode = "silent"
        return 0.0

    if (
        state.center_silence_active
        and abs_center_error <= center_reentry_px
        and (
            not observation.inside_target
            or observation.edge_margin_px >= center_edge_px
        )
    ):
        state.active_mode = "silent"
        return 0.0

    state.center_silence_active = False

    error = observation.error_px
    predicted_error = observation.predicted_error_px
    blended_error = error * 0.55 + predicted_error * 0.45
    abs_error = abs(blended_error)

    jitter = config.center_tolerance_jitter_px
    base_tolerance = config.center_tolerance_px
    if jitter > 0:
        base_tolerance += rng.randint(-jitter, jitter)
    base_tolerance = clamp_int(
        base_tolerance, 2, max(4, int(target_width // 3))
    )
    hold_tolerance = base_tolerance
    if state.held_direction:
        hold_tolerance += config.hold_hysteresis_px

    if (
        abs_error <= hold_tolerance
        and observation.inside_target
        and observation.edge_margin_px >= center_edge_px
    ):
        state.active_mode = "safe"
        return 0.0

    desired_direction = "right" if blended_error < 0 else "left"
    held_direction = state.held_direction
    reverse_threshold = hold_tolerance + config.reverse_confirm_px
    reverse_elapsed = now - state.last_move_time
    if held_direction and desired_direction != held_direction:
        small_error_limit = max(config.anti_shake_error_px, reverse_threshold)
        if (
            abs_error <= small_error_limit
            and reverse_elapsed < config.reverse_direction_cooldown
        ):
            state.active_mode = "brake"
            return 0.0

    # 强度曲线分段：框内以目标宽度定远距参考并收紧上限，框外放开恢复力度。
    if observation.inside_target:
        far_px = max(target_width * 0.75, 55.0)
        strength_cap = action_cap_for_observation(observation, config)
        if (
            min(observation.edge_margin_px, observation.predicted_edge_margin_px)
            <= config.edge_recovery_margin_px
        ):
            strength_floor = min(config.inside_edge_min_action, strength_cap)
        else:
            strength_floor = 0.0
        strength_curve = 1.35
    else:
        far_px = max(float(config.far_error_px), target_width * 0.40)
        strength_cap = config.outside_action_cap
        strength_floor = 0.0
        strength_curve = 1.15

    normalized = clamp_float(abs_error / max(1.0, far_px), 0.0, 1.0)
    strength = normalized**strength_curve
    if strength_floor and abs_error > hold_tolerance:
        strength = max(strength, strength_floor)

    moving_away = (blended_error > 0 and observation.relative_vx > 0) or (
        blended_error < 0 and observation.relative_vx < 0
    )
    moving_toward = (blended_error > 0 and observation.relative_vx < 0) or (
        blended_error < 0 and observation.relative_vx > 0
    )
    if moving_away:
        strength += config.velocity_away_gain + config.relative_away_gain
    elif moving_toward:
        # 接近控制目标时按相对速度主动制动，避免追上移动绿框后继续冲。
        speed_ratio = clamp_float(
            abs(observation.relative_vx) / 400.0, 0.0, 1.0
        )
        dist_ratio = clamp_float(
            abs_center_error / max(1.0, target_width * 0.4), 0.0, 1.0
        )
        if dist_ratio < 1.0:
            brake = (1.0 - dist_ratio) * speed_ratio * 0.32
            strength -= (
                config.velocity_toward_gain + config.relative_toward_gain + brake
            )
        else:
            strength -= config.velocity_toward_gain + config.relative_toward_gain

    # 框外恢复：远距离强拉，预测即将进框时提前降档刹车。
    if not observation.inside_target:
        distance_ratio = clamp_float(
            abs(observation.error_px) / max(1.0, target_width), 0.5, 2.0
        )
        strength += config.outside_recovery_boost * distance_ratio
        # 分级恢复下限：误差越大越保证起步力度，避免长时间贴在框外。
        if abs(observation.error_px) >= config.recovery_hard_error_px:
            strength = max(strength, config.recovery_hard_min_action)
        elif abs(observation.error_px) >= config.recovery_fast_error_px:
            strength = max(strength, config.recovery_fast_min_action)
        if moving_away:
            strength += config.recovery_velocity_gain * clamp_float(
                abs(observation.relative_vx) / 600.0, 0.0, 1.0
            )

        approaching_target = (
            observation.predicted_edge_margin_px >= config.outside_approach_edge_px
            or abs(observation.predicted_error_px)
            <= target_width * config.outside_approach_error_ratio
        )
        if approaching_target:
            approach_cap = config.outside_approach_action_cap
            if target_width >= config.wide_target_width_px:
                approach_cap *= config.wide_target_approach_action_scale
            strength_cap = min(strength_cap, approach_cap)
            strength *= config.outside_approach_strength_scale
            state.active_mode = "approach"
        else:
            state.active_mode = "recover"
    elif observation.edge_margin_px <= config.edge_recovery_margin_px:
        strength += config.edge_recovery_boost
        strength = max(strength, min(config.edge_recovery_min_action, strength_cap))
        state.active_mode = "edge"
    else:
        state.active_mode = "active"

    if held_direction == desired_direction:
        strength += config.action_hold_boost

    strength = clamp_float(strength, 0.0, strength_cap)
    return strength if desired_direction == "right" else -strength


def merge_policy_action(
    observation: Observation,
    rule_action: float,
    residual: float,
    state: ControlState,
    config: FishProConfig,
) -> float:
    """叠加残差、做动作平滑并按观测上限钳制。"""
    residual = clamp_float(
        residual, -config.learning_residual_limit, config.learning_residual_limit
    )
    raw_action = clamp_float(rule_action + residual, -1.0, 1.0)

    if state.center_silence_active or observation.in_center_no_move:
        residual = 0.0
        smoothed_action = 0.0
        final_action = 0.0
    else:
        alpha = config.action_smooth_alpha
        smoothed_action = (
            state.smoothed_action * (1.0 - alpha) + raw_action * alpha
        )
        action_cap = action_cap_for_observation(observation, config)
        final_action = clamp_float(smoothed_action, -action_cap, action_cap)

    state.smoothed_action = smoothed_action
    state.last_rule_action = rule_action
    state.last_residual_action = residual
    state.last_final_action = final_action
    return final_action
