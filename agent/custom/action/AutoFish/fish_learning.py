"""可关闭的有界残差学习；只在控条结束、按键释放后写入本地产物。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import uuid
import zipfile

import numpy as np

from .fish_control import FishDecision, FishObservation, KEY_A, KEY_D
from .fish_params import FishControlConfig, MAX_KEY_HOLD_MS

SCHEMA_VERSION = 1
FEATURE_VERSION = "relative-motion-1"
REWARD_VERSION = "geometry-time-1"
FEATURE_NAMES = (
    "bias", "error", "predicted_error", "relative_velocity", "target_velocity",
    "edge_margin", "predicted_edge_margin", "target_width", "inside", "safe",
    "last_action", "last_rule_action", "relative_displacement",
)
FEATURE_COUNT = len(FEATURE_NAMES)


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _valid_observation(observation: FishObservation) -> bool:
    return (
        all(
            math.isfinite(float(getattr(observation, field.name)))
            for field in fields(observation)
        )
        and observation.timestamp >= 0
        and observation.dt >= 0
        and observation.frame_id >= 0
        and observation.target_width > 0
        and observation.target_right > observation.target_left
    )


def feature_vector(observation: FishObservation) -> np.ndarray:
    if not _valid_observation(observation):
        raise ValueError("invalid observation")
    half_width = max(1.0, observation.target_width / 2)
    values = (
        1.0,
        observation.error_px / half_width,
        observation.predicted_error_px / half_width,
        observation.relative_vx / 650.0,
        observation.target_vx / 650.0,
        observation.edge_margin_px / half_width,
        observation.predicted_edge_margin_px / half_width,
        observation.target_width / 140.0,
        float(observation.inside_target),
        float(observation.in_safe_zone),
        observation.last_action,
        observation.last_rule_action,
        (observation.predicted_error_px - observation.error_px) / half_width,
    )
    return np.clip(np.asarray(values, dtype=np.float64), -1.5, 1.5)


@dataclass(frozen=True)
class _Pending:
    observation: FishObservation
    decision: FishDecision
    started_at: float
    finished_at: float
    held_ms: float
    features: np.ndarray


class FishLearningSession:
    """每局新建；off 不加载、不推理、不训练、不写入，infer 不更新。"""

    def __init__(self, config: FishControlConfig, profile: str, root: Path):
        self.config = config
        self.mode = config.learning_mode
        self.issue: str | None = None
        self._disabled = False
        self._finished = False
        self._pending: _Pending | None = None
        self._records: list[dict] = []
        self._drops: Counter[str] = Counter()
        self.round_samples = 0
        self.dropped_samples = 0
        self.sample_count = 0
        self.update_count = 0
        self.reward_ema = 0.0
        self.weights = np.zeros(FEATURE_COUNT, dtype=np.float64)
        self._rng = np.random.default_rng(config.learning_seed)
        self.session_id = uuid.uuid4().hex
        self.signature = hashlib.sha256(
            (str(profile) + ":" + config.fingerprint()).encode("utf-8")
        ).hexdigest()[:24]
        self.metadata = {
            "schema": SCHEMA_VERSION,
            "feature_version": FEATURE_VERSION,
            "reward_version": REWARD_VERSION,
            "coordinates": [1280, 720],
            "signature": self.signature,
            "config": config.fingerprint(),
        }
        self.model_path = Path(root) / "config" / "autofish" / (self.signature + ".npz")
        self.history_path = Path(root) / "debug" / "custom" / "autofish" / (self.signature + ".jsonl")
        if self.mode != "off":
            self._load()

    @property
    def active(self) -> bool:
        return self.mode in {"infer", "learn"} and not self._disabled and not self._finished

    def _disable(self, reason: str) -> None:
        self.invalidate("disabled")
        self.issue = reason
        self._disabled = True

    def _load(self) -> None:
        try:
            if not self.model_path.exists():
                if self.mode == "infer":
                    self._disable("compatible model not found")
                return
            # 同时限制压缩前大小，避免损坏或意外的大型 NPZ 占满内存。
            if self.model_path.stat().st_size > 1024 * 1024:
                raise ValueError("model too large")
            with zipfile.ZipFile(self.model_path) as archive:
                if sum(item.file_size for item in archive.infolist()) > 1024 * 1024:
                    raise ValueError("expanded model too large")
            with np.load(self.model_path, allow_pickle=False) as data:
                if set(data.files) != {"metadata", "features", "weights", "sample_count", "update_count", "reward_ema"}:
                    raise ValueError("model fields mismatch")
                metadata = data["metadata"]
                if metadata.shape != () or metadata.dtype.kind not in "US":
                    raise ValueError("invalid model metadata")
                if json.loads(str(metadata.item())) != self.metadata:
                    raise ValueError("model version or profile mismatch")
                names = data["features"]
                if names.shape != (FEATURE_COUNT,) or tuple(names.tolist()) != FEATURE_NAMES:
                    raise ValueError("model feature order mismatch")
                weights = data["weights"]
                if weights.shape != (FEATURE_COUNT,) or weights.dtype.kind not in "fiu":
                    raise ValueError("model weight shape mismatch")
                if not np.all(np.isfinite(weights)) or np.any(np.abs(weights) > 5):
                    raise ValueError("invalid model weights")
                counts = []
                for name in ("sample_count", "update_count"):
                    value = data[name]
                    if value.shape != () or value.dtype.kind not in "iu":
                        raise ValueError("invalid model count")
                    count = int(value.item())
                    if not 0 <= count <= 10**12:
                        raise ValueError("model count out of range")
                    counts.append(count)
                ema = data["reward_ema"]
                if ema.shape != () or ema.dtype.kind not in "fiu":
                    raise ValueError("invalid model reward")
                reward_ema = float(ema.item())
                if not math.isfinite(reward_ema) or abs(reward_ema) > 1.5 or counts[1] > counts[0]:
                    raise ValueError("invalid model statistics")
                self.weights = weights.astype(np.float64, copy=True)
                self.sample_count, self.update_count = counts
                self.reward_ema = reward_ema
        except (OSError, ValueError, TypeError, KeyError, EOFError, OverflowError, zipfile.BadZipFile) as exc:
            self._disable("model rejected: " + type(exc).__name__)

    def predict(self, observation: FishObservation) -> float:
        if not self.active or not observation.reliable or observation.in_safe_zone:
            return 0.0
        try:
            features = feature_vector(observation)
            if self.weights.shape != (FEATURE_COUNT,) or not np.all(np.isfinite(self.weights)):
                raise ValueError("non-finite policy")
            residual = float(np.tanh(self.weights @ features)) * self.config.learning_residual_limit
            if self.sample_count >= 1000 and self.reward_ema < 0:
                residual *= 0.2
            if self.mode == "learn":
                residual += float(self._rng.uniform(-self.config.learning_noise, self.config.learning_noise))
            return _clip(residual, -self.config.learning_residual_limit, self.config.learning_residual_limit)
        except (ValueError, TypeError, OverflowError, FloatingPointError):
            self._disable("policy numerical error")
            return 0.0

    def invalidate(self, reason: str) -> None:
        if self._pending is not None:
            self.dropped_samples += 1
            self._drops[reason] += 1
        self._pending = None

    def record_action(
        self,
        observation: FishObservation,
        decision: FishDecision,
        *,
        started_at: float,
        finished_at: float,
        held_ms: float,
    ) -> None:
        """仅由已确认输入成功、且已松键的适配器调用。"""
        if self.mode != "learn" or not self.active:
            return
        if not observation.reliable or not _valid_observation(observation):
            self.invalidate("unreliable")
            return
        numbers = (started_at, finished_at, held_ms, decision.action, decision.rule_action,
                   decision.residual, decision.requested_residual, decision.duration_ms)
        valid = (
            all(math.isfinite(value) for value in numbers)
            and observation.timestamp <= started_at <= finished_at
            and 0 <= held_ms <= MAX_KEY_HOLD_MS + 10
            and held_ms <= (finished_at - started_at) * 1000 + 0.01
            and decision.key in (None, KEY_A, KEY_D)
            and abs(decision.action) <= 1.0
            and abs(decision.residual) <= self.config.learning_residual_limit + 1e-9
        )
        if decision.key is None:
            valid = valid and held_ms == 0 and decision.action == 0
        else:
            valid = valid and held_ms > 0 and decision.duration_ms > 0
            valid = valid and (decision.action < 0 if decision.key == KEY_A else decision.action > 0)
        if not valid:
            self.invalidate("execution_invalid")
            self.dropped_samples += 1
            self._drops["execution_invalid"] += 1
            return
        if self._pending is not None:
            if decision.key is None:
                # 反馈时间窗太短且没有新输入时，保留上一条已执行动作等待观测。
                return
            self.invalidate("overlapping_action")
        self._pending = _Pending(
            observation, decision, started_at, finished_at, held_ms, feature_vector(observation)
        )

    def observe(self, observation: FishObservation) -> None:
        if self.mode != "learn" or not self.active or self._pending is None:
            return
        pending = self._pending
        previous = pending.observation
        if not _valid_observation(observation) or not observation.reliable:
            self.invalidate("unreliable")
            return
        dt = observation.timestamp - previous.timestamp
        if dt <= 0 or observation.frame_id <= previous.frame_id or observation.timestamp < pending.finished_at:
            self.invalidate("observation_order")
            return
        if dt * 1000 > self.config.learning_max_dt_ms:
            self.invalidate("feedback_stale")
            return
        if dt * 1000 < self.config.learning_min_dt_ms:
            return
        if abs(observation.target_width - previous.target_width) >= self.config.width_change_threshold:
            self.invalidate("width_changed")
            return
        self._pending = None
        try:
            self._learn(pending, observation, dt)
        except (ValueError, TypeError, OverflowError, FloatingPointError):
            self._disable("learning numerical error")

    def _learn(self, pending: _Pending, following: FishObservation, dt: float) -> None:
        previous, decision = pending.observation, pending.decision
        half_width = max(1.0, previous.target_width / 2)
        rate_scale = 0.05 / dt
        progress = _clip((abs(previous.error_px) - abs(following.error_px)) / half_width * rate_scale, -1, 1)
        edge_progress = _clip((following.edge_margin_px - previous.edge_margin_px) / half_width * rate_scale, -1, 1)
        duty = min(1.0, pending.held_ms / (dt * 1000))
        if following.inside_target:
            reward = 0.6 + (0.35 if following.in_safe_zone else 0.1) + 0.25 * edge_progress
        else:
            reward = -0.15 if following.edge_margin_px >= -10 else -0.36
            reward += progress * 0.75 + edge_progress * 0.65
        if previous.inside_target != following.inside_target:
            reward += 0.75 if following.inside_target else -0.75
        reward -= 0.12 * abs(decision.action) * duty
        reward -= 0.06 * abs(decision.action - previous.last_action)
        reward = _clip(reward, -1.5, 1.5)
        time_weight = _clip(dt / 0.05, 0.1, 4.0)
        advantage = reward - self.reward_ema
        alpha = 1.0 - (1.0 - self.config.learning_reward_alpha) ** time_weight
        effective_residual = decision.residual if decision.key is not None else 0.0
        updated = abs(effective_residual) > 1e-9
        if updated:
            output = float(np.tanh(self.weights @ pending.features))
            scale = effective_residual / max(1e-6, self.config.learning_residual_limit)
            step = self.config.learning_rate * time_weight * advantage * scale
            weights = self.weights + step * (1.0 - output * output) * pending.features
            weights *= (1.0 - 0.0002) ** time_weight
            if not np.all(np.isfinite(weights)):
                raise ValueError("non-finite update")
            self.weights = np.clip(weights, -5.0, 5.0)
            self.update_count += 1
        self.reward_ema += alpha * (reward - self.reward_ema)
        self.sample_count += 1
        self.round_samples += 1
        record = {
            "type": "transition", "schema": SCHEMA_VERSION, "session": self.session_id,
            "mode": self.mode, "signature": self.signature,
            "feature_version": FEATURE_VERSION, "reward_version": REWARD_VERSION,
            "frame_id": previous.frame_id, "next_frame_id": following.frame_id,
            "dt": dt, "capture_ms": previous.capture_ms,
            "action_delay_ms": (pending.started_at - previous.timestamp) * 1000,
            "held_ms": pending.held_ms, "key": decision.key,
            "rule_action": decision.rule_action, "requested_residual": decision.requested_residual,
            "residual": effective_residual, "final_action": decision.action,
            "inside_target": previous.inside_target, "next_inside": following.inside_target,
            "error_px": previous.error_px, "next_error_px": following.error_px,
            "edge_margin_px": previous.edge_margin_px, "next_edge_margin_px": following.edge_margin_px,
            "target_width": previous.target_width, "reward": reward,
            "reward_ema": self.reward_ema, "updated": updated,
        }
        if len(self._records) < self.config.learning_buffer_limit:
            self._records.append(record)
        else:
            self.dropped_samples += 1
            self._drops["buffer_full"] += 1

    def _save_model(self) -> None:
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.model_path.parent, suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                np.savez(
                    output, metadata=np.array(json.dumps(self.metadata, sort_keys=True)),
                    features=np.asarray(FEATURE_NAMES), weights=self.weights,
                    sample_count=np.int64(self.sample_count), update_count=np.int64(self.update_count),
                    reward_ema=np.float64(self.reward_ema),
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.model_path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def finish(self) -> bool:
        """必须在双键均已尝试释放后调用；不影响本局的成功/失败语义。"""
        if self._finished:
            return True
        self.invalidate("round_end")
        writable = self.active and self.mode == "learn"
        self._finished = True
        if not writable or not (self.round_samples or self.dropped_samples):
            return True
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            summary = {
                "type": "summary", "schema": SCHEMA_VERSION, "session": self.session_id,
                "mode": self.mode, "signature": self.signature,
                "samples": self.round_samples, "recorded": len(self._records),
                "dropped": self.dropped_samples, "drop_reasons": dict(self._drops),
                "reward_ema": self.reward_ema, "update_count": self.update_count,
            }
            with self.history_path.open("a", encoding="utf-8") as output:
                for record in self._records:
                    output.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                output.write(json.dumps(summary, ensure_ascii=False, allow_nan=False) + "\n")
            if self.round_samples:
                self._save_model()
            self._records.clear()
            return True
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            self.issue = "learning save failed: " + type(exc).__name__
            return False
