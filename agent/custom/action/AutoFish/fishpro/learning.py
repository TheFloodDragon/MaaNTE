"""FishPro 控条的在线残差学习层。

移植附件 ``autofish.py`` 的 ``ResidualPolicy``：13 维特征线性残差、
``tanh`` 限幅、奖励函数、优势更新、权重衰减、奖励 EMA 负值缩放、
探索噪声、JSONL 样本与 NPZ 模型持久化、历史回放。

与附件的差异：
- 产物写入调用方传入的目录（默认 ``debug/fishpro/``），不落在源码目录；
- 默认关闭学习，只有开启后才加载与写盘；
- 日志走 ``logger``，不使用 ``print``。
"""

from __future__ import annotations

import json
import random
from collections import deque
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from utils.logger import logger

from .config import FishProConfig
from .state import Observation
from .thresholds import clamp_float, dynamic_safe_margin_px

FEATURE_COUNT = 13


class ResidualPolicy:
    """轻量线性残差策略，叠加在规则层输出之上。"""

    def __init__(self, config: FishProConfig, artifact_dir: Path):
        self.config = config
        self.feature_count = FEATURE_COUNT
        self.weights = np.zeros(self.feature_count, dtype=np.float32)
        self.reward_ema = 0.0
        self.sample_count = 0
        self.update_count = 0
        self.last_reward = 0.0
        self.last_loss = 0.0
        self.loaded = False
        self.history_buffer: deque[dict] = deque()
        self.artifact_dir = Path(artifact_dir)
        self.model_path = self.artifact_dir / config.learning_model_name
        self.data_path = self.artifact_dir / config.learning_data_name

    # ---- 特征与预测 ----

    def feature_vector(self, observation: Observation) -> np.ndarray:
        config = self.config
        target_width = max(1.0, float(observation.target_width))
        half_width = max(1.0, target_width * 0.5)

        error_norm = clamp_float(observation.error_px / half_width, -1.5, 1.5)
        predicted_error_norm = clamp_float(
            observation.predicted_error_px / half_width, -1.5, 1.5
        )
        normalized_error = clamp_float(observation.normalized_error, -1.5, 1.5)
        velocity_norm = clamp_float(observation.cursor_vx / 650.0, -1.5, 1.5)
        edge_margin_norm = clamp_float(
            observation.edge_margin_px / half_width, -1.5, 1.5
        )
        target_width_norm = clamp_float(target_width / 140.0, 0.2, 2.5)
        inside_flag = 1.0 if observation.inside_target else 0.0
        near_edge_flag = (
            1.0
            if observation.edge_margin_px <= config.edge_recovery_margin_px
            else 0.0
        )
        center_offset_norm = clamp_float(
            (observation.control_target_x - observation.target_center_x)
            / half_width,
            -1.0,
            1.0,
        )
        last_action = clamp_float(observation.last_action, -1.0, 1.0)
        last_rule_action = clamp_float(observation.last_rule_action, -1.0, 1.0)
        if observation.held_direction == "right":
            held_sign = 1.0
        elif observation.held_direction == "left":
            held_sign = -1.0
        else:
            held_sign = 0.0

        return np.array(
            [
                1.0,
                error_norm,
                predicted_error_norm,
                normalized_error,
                velocity_norm,
                edge_margin_norm,
                target_width_norm,
                inside_flag,
                near_edge_flag,
                center_offset_norm,
                last_action,
                last_rule_action,
                held_sign,
            ],
            dtype=np.float32,
        )

    def predict(self, observation: Observation) -> float:
        config = self.config
        features = self.feature_vector(observation)
        raw_value = float(np.dot(self.weights, features))
        limit = config.learning_residual_limit
        residual = clamp_float(float(np.tanh(raw_value)) * limit, -limit, limit)
        if (
            self.sample_count >= config.learning_negative_ema_min_samples
            and self.reward_ema < 0.0
        ):
            residual *= config.learning_negative_ema_residual_scale
        return residual

    def explore(
        self,
        residual: float,
        observation: Observation,
        rng: Optional[random.Random] = None,
    ) -> float:
        """叠加探索噪声，框外放大噪声幅度。"""
        rng = rng or random
        config = self.config
        noise = rng.uniform(
            -config.learning_exploration_noise, config.learning_exploration_noise
        )
        if not observation.inside_target:
            noise *= config.learning_outside_exploration_scale
        limit = config.learning_residual_limit
        return clamp_float(residual + noise, -limit, limit)

    # ---- 样本与奖励 ----

    def build_pending_record(
        self,
        observation: Observation,
        rule_action: float,
        residual: float,
        final_action: float,
    ) -> dict:
        features = self.feature_vector(observation)
        return {
            "timestamp": observation.timestamp,
            "features": features.tolist(),
            "error_px": float(observation.error_px),
            "predicted_error_px": float(observation.predicted_error_px),
            "target_width": float(observation.target_width),
            "inside_target": bool(observation.inside_target),
            "edge_margin_px": float(observation.edge_margin_px),
            "last_final_action": float(observation.last_action),
            "last_rule_action": float(observation.last_rule_action),
            "rule_action": float(rule_action),
            "residual": float(residual),
            "final_action": float(final_action),
        }

    def reward_from_transition(
        self, pending: dict, next_observation: Observation
    ) -> float:
        """分级奖励：框内正奖励，框外按距离分级惩罚并奖励改善过程。"""
        config = self.config
        prev_abs_error = abs(float(pending.get("error_px", 0.0)))
        next_abs_error = abs(float(next_observation.error_px))
        target_width = max(
            1.0,
            float(pending.get("target_width", next_observation.target_width)),
        )
        half_width = max(1.0, target_width * 0.5)
        safe_margin_px = dynamic_safe_margin_px(target_width, config)

        prev_inside = bool(pending.get("inside_target", False))
        next_inside = bool(next_observation.inside_target)
        prev_edge_margin = float(pending.get("edge_margin_px", 0.0))
        next_edge_margin = float(next_observation.edge_margin_px)
        final_action = abs(float(pending.get("final_action", 0.0)))
        last_final_action = abs(float(pending.get("last_final_action", 0.0)))

        progress = clamp_float(
            (prev_abs_error - next_abs_error) / half_width, -2.0, 2.0
        )
        edge_progress = clamp_float(
            (next_edge_margin - prev_edge_margin) / half_width, -1.0, 1.0
        )
        safe_edge_ratio = clamp_float(
            next_edge_margin / max(1.0, safe_margin_px), -2.0, 2.0
        )
        smooth_penalty = config.learning_action_smooth_penalty * abs(
            final_action - last_final_action
        )
        residual_penalty = 0.02 * abs(float(pending.get("residual", 0.0)))

        reward = 0.0
        if next_inside:
            reward += 0.60
            if next_edge_margin >= safe_margin_px:
                reward += 0.35
            else:
                reward += 0.20 * safe_edge_ratio
            reward += 0.25 * edge_progress
            reward -= 0.12 * final_action
            if prev_inside:
                reward -= 0.08 * final_action
        else:
            # 一帧未进框不应过度惩罚正确的恢复动作，改为按距离分级。
            if next_edge_margin >= -10:
                reward -= 0.15
            elif next_edge_margin >= -30:
                reward -= 0.28
            else:
                reward -= config.learning_outside_penalty
            if progress > 0:
                reward += progress * 0.75
            else:
                reward += progress * 0.25
            reward += edge_progress * 0.65
            if progress > 0 and edge_progress > 0:
                reward += 0.20

        if prev_inside and not next_inside:
            if next_edge_margin >= -5:
                reward -= 0.50
            elif next_edge_margin >= -15:
                reward -= 0.80
            else:
                reward -= 1.20
        elif not prev_inside and next_inside:
            reward += 1.50

        if next_observation.in_center_no_move:
            reward += config.learning_center_bonus

        reward -= smooth_penalty
        reward -= residual_penalty
        return clamp_float(reward, -1.5, 1.5)

    # ---- 在线更新 ----

    def _apply_update(
        self, features: np.ndarray, residual: float, reward: float
    ) -> float:
        config = self.config
        features = np.asarray(features, dtype=np.float32)
        if features.shape[0] != self.feature_count:
            raise ValueError(
                f"学习特征维度不匹配: {features.shape[0]} != {self.feature_count}"
            )

        limit = max(1e-4, config.learning_residual_limit)
        residual_norm = clamp_float(residual / limit, -1.0, 1.0)
        raw_value = float(np.dot(self.weights, features))
        output_norm = float(np.tanh(raw_value))
        advantage = reward - self.reward_ema
        gradient_scale = 1.0 - output_norm * output_norm
        step = config.learning_online_lr * advantage * residual_norm

        self.weights += step * gradient_scale * features
        self.weights *= 1.0 - config.learning_weight_decay
        np.clip(self.weights, -5.0, 5.0, out=self.weights)

        alpha = config.learning_reward_ema_alpha
        self.reward_ema = self.reward_ema * (1.0 - alpha) + reward * alpha
        self.sample_count += 1
        self.update_count += 1
        self.last_reward = reward
        self.last_loss = float((reward - self.reward_ema) ** 2)
        return self.last_loss

    def update_from_transition(
        self, pending: dict, next_observation: Observation
    ) -> dict:
        features = np.asarray(pending.get("features", []), dtype=np.float32)
        reward = self.reward_from_transition(pending, next_observation)
        loss = self._apply_update(
            features, float(pending.get("residual", 0.0)), reward
        )

        transition = dict(pending)
        transition.update(
            {
                "next_timestamp": float(next_observation.timestamp),
                "next_error_px": float(next_observation.error_px),
                "next_predicted_error_px": float(
                    next_observation.predicted_error_px
                ),
                "next_inside_target": bool(next_observation.inside_target),
                "next_edge_margin_px": float(next_observation.edge_margin_px),
                "next_cursor_x": float(next_observation.cursor_x),
                "next_cursor_vx": float(next_observation.cursor_vx),
                "reward": float(reward),
                "reward_ema": float(self.reward_ema),
                "loss": float(loss),
            }
        )
        self.history_buffer.append(transition)
        if len(self.history_buffer) >= self.config.learning_buffer_flush_threshold:
            self.flush_history()
        return transition

    def replay_record(self, record: dict) -> bool:
        features = np.asarray(record.get("features", []), dtype=np.float32)
        if features.ndim != 1 or features.shape[0] != self.feature_count:
            return False
        reward = float(record.get("reward", 0.0))
        residual = float(record.get("residual", 0.0))
        self._apply_update(features, residual, reward)
        return True

    # ---- 持久化 ----

    def load_artifacts(self, replay_history: bool = False) -> dict:
        loaded = False
        history_count = 0

        if self.model_path.exists():
            try:
                with np.load(self.model_path, allow_pickle=False) as data:
                    weights = np.asarray(
                        data["weights"] if "weights" in data else self.weights,
                        dtype=np.float32,
                    )
                    if weights.ndim == 1 and weights.shape[0] == self.feature_count:
                        self.weights = weights
                    else:
                        logger.warning(
                            "FishPro 学习模型维度不匹配，已重置: shape=%s",
                            getattr(weights, "shape", "<unknown>"),
                        )
                        self.weights = np.zeros(
                            self.feature_count, dtype=np.float32
                        )
                    if "reward_ema" in data:
                        self.reward_ema = float(data["reward_ema"])
                    if "sample_count" in data:
                        self.sample_count = int(data["sample_count"])
                    if "update_count" in data:
                        self.update_count = int(data["update_count"])
                    if "last_reward" in data:
                        self.last_reward = float(data["last_reward"])
                    if "last_loss" in data:
                        self.last_loss = float(data["last_loss"])
                    loaded = True
            except Exception as exc:
                logger.warning("FishPro 加载学习模型失败: %s", exc)

        if self.data_path.exists():
            try:
                history_count = self.count_history_records(
                    limit=self.config.learning_history_replay_limit
                )
                if replay_history and not loaded:
                    for record in self.iter_history_records(
                        limit=self.config.learning_history_replay_limit
                    ):
                        self.replay_record(record)
            except Exception as exc:
                logger.warning("FishPro 读取学习数据失败: %s", exc)

        self.loaded = loaded or (replay_history and history_count > 0)
        return {"loaded": loaded, "history_count": history_count}

    def count_history_records(self, limit: Optional[int] = None) -> int:
        if not self.data_path.exists():
            return 0

        count = 0
        with self.data_path.open("r", encoding="utf-8") as handle:
            if limit is None:
                for _line in handle:
                    count += 1
            else:
                window: deque[str] = deque(maxlen=limit)
                for line in handle:
                    window.append(line)
                count = len(window)
        return count

    def iter_history_records(
        self, limit: Optional[int] = None
    ) -> Iterable[dict]:
        if not self.data_path.exists():
            return

        if limit is None:
            with self.data_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    record = _parse_json_line(line)
                    if record is not None:
                        yield record
            return

        window: deque[str] = deque(maxlen=limit)
        with self.data_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                window.append(line)
        for line in window:
            record = _parse_json_line(line)
            if record is not None:
                yield record

    def flush_history(self, force: bool = False) -> int:
        if not self.history_buffer:
            return 0
        if (
            not force
            and len(self.history_buffer)
            < self.config.learning_buffer_flush_threshold
        ):
            return 0

        try:
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with self.data_path.open("a", encoding="utf-8") as handle:
                while self.history_buffer:
                    record = self.history_buffer.popleft()
                    handle.write(
                        json.dumps(
                            record, ensure_ascii=False, separators=(",", ":")
                        )
                    )
                    handle.write("\n")
                    written += 1
            return written
        except OSError as exc:
            logger.warning("FishPro 写入学习样本失败: %s", exc)
            self.history_buffer.clear()
            return 0

    def save_model(self) -> bool:
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                self.model_path,
                weights=self.weights.astype(np.float32),
                reward_ema=np.array(self.reward_ema, dtype=np.float32),
                sample_count=np.array(self.sample_count, dtype=np.int64),
                update_count=np.array(self.update_count, dtype=np.int64),
                last_reward=np.array(self.last_reward, dtype=np.float32),
                last_loss=np.array(self.last_loss, dtype=np.float32),
                feature_count=np.array(self.feature_count, dtype=np.int64),
            )
            return True
        except Exception as exc:
            logger.warning("FishPro 保存学习模型失败: %s", exc)
            return False

    def save_all(self, force_history: bool = True) -> bool:
        self.flush_history(force=force_history)
        return self.save_model()

    def summary(self) -> str:
        return (
            f"loaded={'Y' if self.loaded else 'N'} "
            f"samples={self.sample_count} updates={self.update_count} "
            f"reward_ema={self.reward_ema:+.3f} "
            f"last_reward={self.last_reward:+.3f} "
            f"last_loss={self.last_loss:.4f} "
            f"buffer={len(self.history_buffer)}"
        )


def _parse_json_line(line: str) -> Optional[dict]:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
