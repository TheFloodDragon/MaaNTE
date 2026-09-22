"""720p 钓鱼预测内核；不调用 Controller、真实时钟或文件系统。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

from .fish_params import FishControlConfig, MAX_KEY_HOLD_MS

KEY_A = 65
KEY_D = 68


def estimate_error_velocity(
    last_error: Optional[float],
    error: float,
    delta_seconds: float,
    last_velocity: float = 0.0,
    alpha: float = 0.35,
    max_velocity: float = 4000.0,
) -> float:
    """用误差变化估计相对速度，并用 EWMA 限制识别噪声。"""
    if last_error is None or delta_seconds <= 0:
        return last_velocity

    raw_velocity = (error - last_error) / delta_seconds
    raw_velocity = max(-max_velocity, min(max_velocity, raw_velocity))
    alpha = max(0.0, min(1.0, alpha))
    return last_velocity + alpha * (raw_velocity - last_velocity)


def choose_control_key(
    current_key: Optional[int],
    predicted_error: float,
    enter_deadzone: float = 15.0,
    release_deadzone: float = 7.0,
) -> Optional[int]:
    """根据预测误差决定保持、切换或释放 A/D。

    正误差表示光标在绿条右侧，需要按 A；负误差需要按 D。
    ``enter_deadzone`` 与 ``release_deadzone`` 形成施密特滞回，避免边界抖动。
    """
    enter_deadzone = max(0.0, float(enter_deadzone))
    release_deadzone = max(0.0, min(float(release_deadzone), enter_deadzone))

    if current_key is None:
        if predicted_error > enter_deadzone:
            return KEY_A
        if predicted_error < -enter_deadzone:
            return KEY_D
        return None

    if current_key == KEY_A:
        if predicted_error < -enter_deadzone:
            return KEY_D
        if predicted_error < release_deadzone:
            return None
        return KEY_A

    if current_key == KEY_D:
        if predicted_error > enter_deadzone:
            return KEY_A
        if predicted_error > -release_deadzone:
            return None
        return KEY_D

    return choose_control_key(None, predicted_error, enter_deadzone, release_deadzone)


def choose_tracking_key(
    current_key: Optional[int],
    cursor_center: float,
    cursor_velocity: float,
    green_left: float,
    green_right: float,
    green_velocity: float = 0.0,
    lookahead_seconds: float = 0.16,
    safe_margin: float = 5.0,
    center_band_ratio: float = 0.4,
    switch_margin: float = 3.0,
) -> Optional[int]:
    """光标预测会离开绿条中心走廊时才进行控制。

    A 将光标向左移动，D 将光标向右移动。绿条中心与光标使用同一个
    预测时间窗，从而在绿条移动时提前跟随。
    """
    lookahead_seconds = max(0.0, float(lookahead_seconds))
    safe_margin = max(0.0, float(safe_margin))
    center_band_ratio = max(0.0, min(1.0, float(center_band_ratio)))
    switch_margin = max(0.0, float(switch_margin))

    predicted_cursor = cursor_center + cursor_velocity * lookahead_seconds
    safe_left, safe_right = predict_tracking_interval(
        green_left,
        green_right,
        green_velocity,
        lookahead_seconds,
        safe_margin,
        center_band_ratio,
    )

    # 已在安全区内时松键，让下一帧观测决定是否需要跟随。
    if current_key == KEY_A:
        if predicted_cursor < safe_left - switch_margin:
            return KEY_D
        if predicted_cursor <= safe_right:
            return None
        return KEY_A

    if current_key == KEY_D:
        if predicted_cursor > safe_right + switch_margin:
            return KEY_A
        if predicted_cursor >= safe_left:
            return None
        return KEY_D

    if predicted_cursor > safe_right:
        return KEY_A
    if predicted_cursor < safe_left:
        return KEY_D
    return None


def predict_tracking_interval(
    green_left: float,
    green_right: float,
    green_velocity: float,
    lookahead_seconds: float,
    safe_margin: float,
    center_band_ratio: float,
) -> tuple[float, float]:
    """返回预测时刻绿条中心走廊的左右边界。"""
    lookahead_seconds = max(0.0, float(lookahead_seconds))
    safe_margin = max(0.0, float(safe_margin))
    center_band_ratio = max(0.0, min(1.0, float(center_band_ratio)))
    green_width = max(0.0, float(green_right) - float(green_left))
    predicted_center = (float(green_left) + float(green_right)) / 2.0 + float(
        green_velocity
    ) * lookahead_seconds
    usable_width = max(0.0, green_width - safe_margin * 2.0)
    half_band = usable_width * center_band_ratio / 2.0
    return predicted_center - half_band, predicted_center + half_band


def should_finish_control(
    has_seen_control: bool,
    last_cursor_seen: Optional[float],
    now: float,
    grace_ms: float,
) -> bool:
    """控条开始后光标持续消失，视为进入结果阶段。"""
    if not has_seen_control or last_cursor_seen is None:
        return False
    return (float(now) - float(last_cursor_seen)) * 1000 >= max(0.0, float(grace_ms))


@dataclass(frozen=True)
class FishObservation:
    timestamp: float
    dt: float
    frame_id: int
    capture_ms: float
    cursor_x: float
    cursor_vx: float
    target_center_x: float
    target_vx: float
    target_left: float
    target_right: float
    target_width: float
    predicted_cursor_x: float
    predicted_target_center_x: float
    error_px: float
    predicted_error_px: float
    edge_margin_px: float
    predicted_edge_margin_px: float
    safe_margin_px: float
    inside_target: bool
    in_safe_zone: bool
    reliable: bool
    last_action: float = 0.0
    last_rule_action: float = 0.0

    @property
    def relative_vx(self) -> float:
        return self.cursor_vx - self.target_vx


@dataclass(frozen=True)
class FishDecision:
    key: int | None = None
    duration_ms: float = 0.0
    action: float = 0.0
    rule_action: float = 0.0
    residual: float = 0.0
    requested_residual: float = 0.0
    mode: str = "idle"


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _time_alpha(alpha: float, dt: float) -> float:
    """将 30Hz 下的平滑系数换算到实际采样间隔。"""
    return 1.0 - (1.0 - _clip(alpha, 0.0, 1.0)) ** (max(dt, 0.0) * 30.0)


def _valid_box(box) -> bool:
    try:
        x, y, width, height = (float(value) for value in box)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        all(math.isfinite(value) for value in (x, y, width, height))
        and 0 <= x < x + width <= 1280
        and 0 <= y < y + height <= 720
    )


class FishingEngine:
    """一局一实例；观测、规则决策与执行确认各自独立。"""

    def __init__(self, config: FishControlConfig | None = None):
        self.config = config or FishControlConfig()
        self.reset_tracking()

    def reset_tracking(self) -> None:
        self._last_time: float | None = None
        self._last_frame_id = 0
        self._cursor_raw = self._target_raw = 0.0
        self._cursor_x = self._target_x = 0.0
        self._cursor_v = self._target_v = 0.0
        self._width = 0.0
        self._width_candidate: float | None = None
        self._width_hits = 0
        self._jump_candidate: tuple[float, float] | None = None
        self._silent = False
        self.last_action = self.last_rule_action = 0.0

    @property
    def last_cursor_center(self) -> float | None:
        return self._cursor_raw if self._last_time is not None else None

    def observe(
        self,
        green_box,
        cursor_box,
        timestamp: float,
        *,
        capture_ms: float = 0.0,
        frame_id: int = 0,
    ) -> FishObservation | None:
        cfg = self.config
        if not _valid_box(green_box) or not _valid_box(cursor_box):
            return None
        if not math.isfinite(timestamp) or not math.isfinite(capture_ms):
            return None
        if timestamp < 0 or not 0 <= capture_ms <= cfg.observation_gap_ms:
            return None
        if self._last_time is not None and timestamp <= self._last_time:
            return None
        if frame_id and frame_id <= self._last_frame_id:
            return None

        left, _, width, _ = (float(value) for value in green_box)
        cursor = float(cursor_box[0]) + float(cursor_box[2]) / 2.0
        center = left + width / 2.0
        dt = timestamp - self._last_time if self._last_time is not None else 0.0
        discontinuity = self._last_time is None or dt * 1000 > cfg.observation_gap_ms
        if not discontinuity:
            jump_limit = cfg.max_velocity * dt + cfg.jump_slack
            jumped = (
                abs(cursor - self._cursor_raw) > jump_limit
                or abs(center - self._target_raw) > jump_limit
            )
            if jumped:
                candidate = self._jump_candidate
                self._jump_candidate = (cursor, center)
                if candidate is None or max(abs(cursor - candidate[0]), abs(center - candidate[1])) > jump_limit:
                    return None
                discontinuity = True
            self._jump_candidate = None

        width_changed = False
        if discontinuity:
            self.reset_tracking()
            self._cursor_x, self._target_x = cursor, center
            self._width = width
            dt = 0.0
        else:
            width_changed = abs(width - self._width) >= cfg.width_change_threshold
            if width_changed:
                if self._width_candidate is not None and abs(width - self._width_candidate) < cfg.width_change_threshold / 2:
                    self._width_hits += 1
                else:
                    self._width_candidate, self._width_hits = width, 1
                if self._width_hits >= cfg.width_confirm_frames:
                    self._width = width
                    self._width_candidate, self._width_hits = None, 0
                # 宽度突变可能移动 bbox 中心，不能当成目标速度。
                self._target_x, self._target_v = center, 0.0
                self._silent = False
            else:
                self._width += (width - self._width) * _time_alpha(0.2, dt)
                self._width_candidate, self._width_hits = None, 0
                self._target_v = estimate_error_velocity(
                    self._target_raw, center, dt, self._target_v,
                    _time_alpha(cfg.green_velocity_alpha, dt), cfg.max_velocity,
                )
                self._target_x += (center - self._target_x) * _time_alpha(cfg.green_center_alpha, dt)
            self._cursor_v = estimate_error_velocity(
                self._cursor_raw, cursor, dt, self._cursor_v,
                _time_alpha(cfg.velocity_alpha, dt), cfg.max_velocity,
            )
            self._cursor_x += (cursor - self._cursor_x) * _time_alpha(cfg.cursor_center_alpha, dt)

        self._cursor_raw, self._target_raw = cursor, center
        self._last_time, self._last_frame_id = timestamp, frame_id
        # 安全边界始终包含当前检测约束，不能用平滑后的旧宽条掩盖收缩。
        usable_width = min(width, self._width)
        # 预测中心与光标采用对称的平滑过程，实际安全边界仍取本帧原始检测。
        target_x = self._target_x
        target_left, target_right = center - usable_width / 2, center + usable_width / 2
        horizon = min(
            cfg.max_prediction_ms / 1000.0,
            max(cfg.prediction_ms / 1000.0, dt * 0.75, capture_ms / 1000.0),
        )
        predicted_cursor = self._cursor_x + self._cursor_v * horizon
        predicted_target = target_x + self._target_v * horizon
        error = self._cursor_x - target_x
        predicted_error = predicted_cursor - predicted_target
        # 实际边缘用原始光标，避免位置平滑掩盖已经越界的危险。
        edge = min(cursor - target_left, target_right - cursor)
        predicted_edge = usable_width / 2.0 - abs(predicted_error)
        safe_margin = min(max(cfg.safe_margin, usable_width * cfg.safe_margin_ratio), usable_width * 0.4)
        inside = edge >= 0.0
        return FishObservation(
            timestamp=timestamp, dt=dt, frame_id=frame_id, capture_ms=capture_ms,
            cursor_x=self._cursor_x, cursor_vx=self._cursor_v,
            target_center_x=target_x, target_vx=self._target_v,
            target_left=target_left, target_right=target_right, target_width=usable_width,
            predicted_cursor_x=predicted_cursor, predicted_target_center_x=predicted_target,
            error_px=error, predicted_error_px=predicted_error,
            edge_margin_px=edge, predicted_edge_margin_px=predicted_edge,
            safe_margin_px=safe_margin, inside_target=inside,
            in_safe_zone=inside and min(edge, predicted_edge) >= safe_margin,
            reliable=not (discontinuity or width_changed),
            last_action=self.last_action, last_rule_action=self.last_rule_action,
        )

    def _rule(self, obs: FishObservation) -> tuple[float, float, str]:
        cfg = self.config
        if not obs.reliable:
            self._silent = False
            return 0.0, cfg.inside_action_cap, "reacquire"
        edge = min(obs.edge_margin_px, obs.predicted_edge_margin_px)
        quiet_margin = obs.safe_margin_px * cfg.release_margin_ratio
        if obs.in_safe_zone or (
            obs.inside_target and edge >= quiet_margin
            and obs.predicted_edge_margin_px >= obs.edge_margin_px
            and (self._silent or abs(obs.error_px) <= obs.target_width * cfg.center_width_ratio)
        ):
            self._silent = True
            return 0.0, cfg.inside_action_cap, "safe"
        self._silent = False

        error = obs.error_px * 0.4 + obs.predicted_error_px * 0.6
        moving_toward = obs.error_px * obs.relative_vx < 0.0
        approaching = not obs.inside_target and moving_toward and obs.predicted_edge_margin_px >= 0.0
        if approaching and obs.predicted_edge_margin_px >= obs.safe_margin_px:
            return 0.0, cfg.approach_action_cap, "coast"
        if obs.inside_target:
            cap = cfg.inside_edge_action_cap if edge < obs.safe_margin_px else cfg.inside_action_cap
            if obs.target_width >= cfg.wide_target_width:
                cap = min(1.0, cap * cfg.wide_target_scale)
            strength = (min(1.0, abs(error) / max(55.0, obs.target_width * 0.75))) ** 1.35
            strength += cfg.edge_recovery_boost * max(0.0, 1.0 - edge / max(1.0, obs.safe_margin_px))
            mode = "edge" if obs.edge_margin_px < obs.safe_margin_px else "brake"
            if obs.predicted_edge_margin_px < 0:
                error = obs.predicted_error_px
        else:
            cap = cfg.approach_action_cap if approaching else cfg.outside_action_cap
            strength = (min(1.0, abs(error) / max(64.0, obs.target_width * 0.4))) ** 1.15
            strength += cfg.outside_recovery_boost * _clip(abs(obs.error_px) / max(1.0, obs.target_width), 0.5, 2.0)
            mode = "approach" if approaching else "recover"
            if moving_toward:
                strength *= 0.7
                if obs.error_px * obs.predicted_error_px < 0:
                    error, cap, mode = obs.predicted_error_px, cfg.approach_action_cap, "brake"
            else:
                # 尚未朝目标移动时不允许预测平滑残留把恢复方向反转。
                error = obs.error_px
        if error * obs.relative_vx > 0:
            strength += 0.18
        floor = min(cap, max(cfg.action_deadzone * 2.0, cap * 0.25))
        strength = _clip(strength, floor, cap)
        if abs(error) < 1e-6:
            return 0.0, cap, "idle"
        return (-strength if error > 0 else strength), cap, mode

    def _pulse(self, action: float, obs: FishObservation, cap: float) -> tuple[int | None, float]:
        cfg = self.config
        if abs(action) <= cfg.action_deadzone:
            return None, 0.0
        if obs.inside_target:
            minimum, maximum = cfg.inside_pulse_min_ms, cfg.inside_pulse_max_ms
        else:
            minimum, maximum = cfg.outside_pulse_min_ms, cfg.outside_pulse_max_ms
        ratio = _clip((abs(action) - cfg.action_deadzone) / max(1e-6, cap - cfg.action_deadzone), 0.0, 1.0)
        duration = minimum + ratio * (maximum - minimum)
        if cfg.pulse_error_gain:
            duration = max(duration, minimum + max(0.0, -obs.predicted_edge_margin_px) * cfg.pulse_error_gain)
        duration = min(MAX_KEY_HOLD_MS, maximum, max(minimum, round(duration)))
        return (KEY_A if action < 0 else KEY_D), duration

    def decide(self, obs: FishObservation, residual: float = 0.0) -> FishDecision:
        rule, cap, mode = self._rule(obs)
        if not rule:
            return FishDecision(mode=mode)
        cfg = self.config
        requested = float(residual) if math.isfinite(residual) else 0.0
        requested = _clip(requested, -cfg.learning_residual_limit, cfg.learning_residual_limit)
        action = _clip(rule + requested, -cap, cap)
        # 模型可以微调强度，不能推翻已确认危险的纠偏方向或消灭最小恢复动作。
        floor = min(cap, max(cfg.action_deadzone * 2.0, cap * 0.25))
        action = math.copysign(_clip(action * math.copysign(1.0, rule), floor, cap), rule)
        key, duration = self._pulse(action, obs, cap)
        baseline_key, baseline_duration = self._pulse(rule, obs, cap)
        effective = action - rule if (key, duration) != (baseline_key, baseline_duration) else 0.0
        if not obs.inside_target and abs(action) >= 0.8:
            mode = "hold"
        return FishDecision(
            key=key, duration_ms=duration, action=action, rule_action=rule,
            residual=effective, requested_residual=requested, mode=mode,
        )

    def remember_execution(self, decision: FishDecision) -> None:
        """只在输入成功并已松键后记录，避免把计划动作当成实际动作。"""
        self.last_action = decision.action if decision.key is not None else 0.0
        self.last_rule_action = decision.rule_action
