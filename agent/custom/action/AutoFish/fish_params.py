"""钓鱼参数解析；旧字典接口保留用于兼容和离线基线对照。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math

FISH_CONTROL_DEFAULTS = {
    "safe_margin": 6.0,
    "center_band_ratio": 0.4,
    "prediction_ms": 140.0,
    "velocity_alpha": 0.5,
    "green_velocity_alpha": 0.5,
    "green_center_alpha": 0.85,
    "pulse_min_ms": 18.0,
    "pulse_max_ms": 36.0,
    "pulse_ms_per_px": 0.45,
    "width_change_threshold": 8.0,
    "width_confirm_frames": 2,
    "control_end_grace_ms": 300.0,
    "lost_timeout_ms": 120.0,
    "lost_abort_ms": 1500.0,
    "loop_interval_ms": 0.0,
}


def load_custom_action_params(custom_action_param) -> dict:
    """将 CustomAction 参数统一解析为字典。"""
    if not custom_action_param:
        return {}
    if isinstance(custom_action_param, dict):
        return custom_action_param
    try:
        params = json.loads(custom_action_param)
    except (TypeError, ValueError):
        return {}
    return params if isinstance(params, dict) else {}


def _float_param(params: dict, name: str) -> float:
    default = float(FISH_CONTROL_DEFAULTS[name])
    value = params.get(name, default)
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _int_param(params: dict, name: str) -> int:
    default = int(FISH_CONTROL_DEFAULTS[name])
    value = params.get(name, default)
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def load_fish_control_params(custom_action_param) -> dict:
    """逐项解析控条参数，无效字段回退为默认值。"""
    params = load_custom_action_params(custom_action_param)
    pulse_min_ms = max(0.0, _float_param(params, "pulse_min_ms"))
    return {
        "safe_margin": max(0.0, _float_param(params, "safe_margin")),
        "center_band_ratio": max(
            0.0, min(1.0, _float_param(params, "center_band_ratio"))
        ),
        "prediction_ms": max(0.0, _float_param(params, "prediction_ms")),
        "velocity_alpha": _float_param(params, "velocity_alpha"),
        "green_velocity_alpha": _float_param(params, "green_velocity_alpha"),
        "green_center_alpha": max(
            0.0, min(1.0, _float_param(params, "green_center_alpha"))
        ),
        "pulse_min_ms": pulse_min_ms,
        "pulse_max_ms": max(pulse_min_ms, _float_param(params, "pulse_max_ms")),
        "pulse_ms_per_px": max(0.0, _float_param(params, "pulse_ms_per_px")),
        "width_change_threshold": max(
            0.1, _float_param(params, "width_change_threshold")
        ),
        "width_confirm_frames": max(1, _int_param(params, "width_confirm_frames")),
        "control_end_grace_ms": max(0.0, _float_param(params, "control_end_grace_ms")),
        "lost_timeout_ms": max(0.0, _float_param(params, "lost_timeout_ms")),
        "lost_abort_ms": max(0.0, _float_param(params, "lost_abort_ms")),
        "loop_interval_ms": max(0.0, _float_param(params, "loop_interval_ms")),
    }


# 应用层的按键持续时间上限；不代表原生 Controller 调用具备硬实时保证。
MAX_KEY_HOLD_MS = 70.0


@dataclass(frozen=True)
class FishControlConfig:
    """所有像素和速度均基于 1280×720；时间参数以毫秒计。"""

    prediction_ms: float = 70.0
    max_prediction_ms: float = 200.0
    velocity_alpha: float = 0.35
    green_velocity_alpha: float = 0.35
    cursor_center_alpha: float = 0.65
    green_center_alpha: float = 0.65
    safe_margin: float = 9.0
    safe_margin_ratio: float = 0.13
    center_width_ratio: float = 0.22
    release_margin_ratio: float = 0.65
    action_deadzone: float = 0.04
    inside_action_cap: float = 0.16
    inside_edge_action_cap: float = 0.32
    outside_action_cap: float = 1.0
    edge_recovery_boost: float = 0.50
    outside_recovery_boost: float = 0.55
    approach_action_cap: float = 0.35
    wide_target_width: float = 120.0
    wide_target_scale: float = 1.0
    inside_pulse_min_ms: float = 4.0
    inside_pulse_max_ms: float = 24.0
    outside_pulse_min_ms: float = 8.0
    outside_pulse_max_ms: float = 70.0
    pulse_error_gain: float = 0.0
    width_change_threshold: float = 8.0
    width_confirm_frames: int = 2
    max_velocity: float = 1800.0
    jump_slack: float = 12.0
    observation_gap_ms: float = 350.0
    control_end_grace_ms: float = 300.0
    lost_timeout_ms: float = 120.0
    lost_abort_ms: float = 1500.0
    loop_interval_ms: float = 0.0
    learning_mode: str = "off"
    learning_rate: float = 0.010
    learning_residual_limit: float = 0.25
    learning_noise: float = 0.015
    learning_reward_alpha: float = 0.025
    learning_min_dt_ms: float = 15.0
    learning_max_dt_ms: float = 300.0
    learning_buffer_limit: int = 4096
    learning_seed: int | None = None

    def fingerprint(self) -> str:
        """推理和学习共用相同控制配置的模型，随机种子不影响兼容性。"""
        values = asdict(self)
        for key in ("learning_mode", "learning_seed", "learning_buffer_limit"):
            values.pop(key)
        encoded = json.dumps(values, sort_keys=True, allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]


_ENGINE_BOUNDS = {
    "prediction_ms": (0.0, 250.0),
    "max_prediction_ms": (1.0, 250.0),
    "velocity_alpha": (0.0, 1.0),
    "green_velocity_alpha": (0.0, 1.0),
    "cursor_center_alpha": (0.05, 1.0),
    "green_center_alpha": (0.05, 1.0),
    "safe_margin": (0.0, 100.0),
    "safe_margin_ratio": (0.0, 0.45),
    "center_width_ratio": (0.01, 0.45),
    "release_margin_ratio": (0.1, 0.95),
    "action_deadzone": (0.001, 0.5),
    "inside_action_cap": (0.02, 1.0),
    "inside_edge_action_cap": (0.02, 1.0),
    "outside_action_cap": (0.02, 1.0),
    "edge_recovery_boost": (0.0, 1.0),
    "outside_recovery_boost": (0.0, 1.0),
    "approach_action_cap": (0.02, 1.0),
    "wide_target_width": (10.0, 486.0),
    "wide_target_scale": (0.5, 1.5),
    "inside_pulse_min_ms": (1.0, MAX_KEY_HOLD_MS),
    "inside_pulse_max_ms": (1.0, MAX_KEY_HOLD_MS),
    "outside_pulse_min_ms": (1.0, MAX_KEY_HOLD_MS),
    "outside_pulse_max_ms": (1.0, MAX_KEY_HOLD_MS),
    "pulse_error_gain": (0.0, 5.0),
    "width_change_threshold": (0.5, 100.0),
    "width_confirm_frames": (1, 8),
    "max_velocity": (50.0, 4000.0),
    "jump_slack": (1.0, 100.0),
    "observation_gap_ms": (20.0, 1000.0),
    "control_end_grace_ms": (50.0, 2000.0),
    "lost_timeout_ms": (0.0, 1000.0),
    "lost_abort_ms": (100.0, 5000.0),
    "loop_interval_ms": (0.0, 100.0),
    "learning_rate": (0.00001, 0.05),
    "learning_residual_limit": (0.0, 0.25),
    "learning_noise": (0.0, 0.025),
    "learning_reward_alpha": (0.001, 0.2),
    "learning_min_dt_ms": (5.0, 100.0),
    "learning_max_dt_ms": (20.0, 1000.0),
    "learning_buffer_limit": (1, 16384),
}


def load_fish_engine_config(custom_action_param) -> FishControlConfig:
    """解析新引擎参数；显式新字段优先于历史别名，禁止非有限值。"""
    params = dict(load_custom_action_params(custom_action_param))
    if "center_width_ratio" not in params and "center_band_ratio" in params:
        value = _float_param(params, "center_band_ratio")
        params["center_width_ratio"] = value / 2.0
    for old, names in {
        "pulse_min_ms": ("inside_pulse_min_ms", "outside_pulse_min_ms"),
        "pulse_max_ms": ("inside_pulse_max_ms", "outside_pulse_max_ms"),
        "pulse_ms_per_px": ("pulse_error_gain",),
        "safe_margin_width_ratio": ("safe_margin_ratio",),
    }.items():
        if old in params:
            for name in names:
                params.setdefault(name, params[old])
    if "learning_mode" not in params and isinstance(params.get("learning_enabled"), bool):
        params["learning_mode"] = "learn" if params["learning_enabled"] else "off"

    values = asdict(FishControlConfig())
    for name, (lower, upper) in _ENGINE_BOUNDS.items():
        default = values[name]
        raw = params.get(name, default)
        if isinstance(raw, bool):
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(number):
            continue
        if isinstance(default, int):
            if not number.is_integer():
                continue
            number = int(number)
        values[name] = max(lower, min(upper, number))

    mode = params.get("learning_mode", "off")
    values["learning_mode"] = mode if isinstance(mode, str) and mode in {"off", "infer", "learn"} else "off"
    seed = params.get("learning_seed")
    if isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed < 2**32:
        values["learning_seed"] = seed

    values["prediction_ms"] = min(values["prediction_ms"], values["max_prediction_ms"])
    values["inside_edge_action_cap"] = max(values["inside_action_cap"], values["inside_edge_action_cap"])
    values["outside_action_cap"] = max(values["inside_edge_action_cap"], values["outside_action_cap"])
    values["approach_action_cap"] = min(values["approach_action_cap"], values["outside_action_cap"])
    # 即使用户把上限设得很低，危险边缘的纠偏也不能被死区全部吞掉。
    smallest_cap = min(values["inside_action_cap"] * min(1.0, values["wide_target_scale"]), values["approach_action_cap"])
    values["action_deadzone"] = min(values["action_deadzone"], smallest_cap * 0.5)
    for prefix in ("inside", "outside"):
        values[prefix + "_pulse_max_ms"] = max(values[prefix + "_pulse_min_ms"], values[prefix + "_pulse_max_ms"])
    values["lost_abort_ms"] = max(values["lost_abort_ms"], values["lost_timeout_ms"] + 1.0, values["control_end_grace_ms"] + 1.0)
    values["learning_min_dt_ms"] = min(values["learning_min_dt_ms"], values["observation_gap_ms"])
    values["learning_max_dt_ms"] = max(values["learning_min_dt_ms"], min(values["learning_max_dt_ms"], values["observation_gap_ms"]))
    return FishControlConfig(**values)
