"""FishPro 控条的运行层。

串联识别、状态估计、规则策略、残差学习与按键执行，并负责：

- 丢帧分级宽限与动作衰减，超限后释放按键并重置状态；
- 控制日志限频与状态变化触发；
- 可选调试帧落盘（不弹窗，避免干扰前台输入与无窗口环境）；
- 会话结束、异常与任务停止时强制释放 A/D。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from utils import screen
from utils.logger import logger

from .config import FishProConfig
from .executor import ActionExecutor
from .learning import ResidualPolicy
from .policy import compute_rule_action, merge_policy_action
from .state import (
    ControlState,
    Observation,
    build_observation,
    can_recover_from_missing,
    clear_missing_tracking,
    mark_missing_state,
    missing_fallback_action,
    reset_control_state,
    update_cursor_state,
)
from .thresholds import dynamic_center_reentry_px, effective_safe_margin_px
from .vision import Detection, clip_roi, detect_control, resolve_roi

# 会话结束原因。
REASON_CONTROL_FINISHED = "control_finished"
REASON_LOST_ABORT = "lost_abort"
REASON_STOPPING = "stopping"
REASON_TIMEOUT = "timeout"
REASON_ERROR = "error"


@dataclass
class SessionResult:
    """一次控条会话的结果摘要。"""

    success: bool
    reason: str
    frames: int
    valid_frames: int
    inside_frames: int
    elapsed_sec: float

    @property
    def inside_ratio(self) -> float:
        if self.valid_frames <= 0:
            return 0.0
        return self.inside_frames / self.valid_frames


class FishProSession:
    """单次控条会话。"""

    def __init__(
        self,
        config: FishProConfig,
        executor: ActionExecutor,
        screencap: Callable[[], Optional[np.ndarray]],
        should_stop: Callable[[], bool],
        residual_policy: Optional[ResidualPolicy] = None,
        debug_dir: Optional[Path] = None,
        rng: Optional[random.Random] = None,
        clock: Callable[[], float] = time.perf_counter,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._config = config
        self._executor = executor
        self._screencap = screencap
        self._should_stop = should_stop
        self._policy = residual_policy
        self._debug_dir = debug_dir
        self._rng = rng or random.Random()
        self._clock = clock
        self._sleep = sleeper
        self._state = ControlState()
        self._state.learning_enabled = bool(
            config.learning_enabled and residual_policy is not None
        )

    @property
    def state(self) -> ControlState:
        return self._state

    # ---- 主循环 ----

    def run(self) -> SessionResult:
        config = self._config
        state = self._state
        started_at = self._clock()

        frames = 0
        valid_frames = 0
        inside_frames = 0
        has_seen_control = False
        last_cursor_seen: Optional[float] = None
        lost_since: Optional[float] = None
        reason = REASON_STOPPING
        success = False

        try:
            while True:
                if self._should_stop():
                    reason = REASON_STOPPING
                    success = False
                    break

                now = self._clock()
                if (
                    config.session_timeout_ms > 0
                    and (now - started_at) * 1000.0 >= config.session_timeout_ms
                ):
                    logger.warning("FishPro 控条会话超时，交给 Pipeline 恢复")
                    reason = REASON_TIMEOUT
                    success = False
                    break

                image = self._screencap()
                roi_rect = self._resolve_roi_rect(image)
                roi = clip_roi(image, roi_rect) if roi_rect is not None else None
                now = self._clock()
                frames += 1

                last_cursor_hint = (
                    state.smoothed_cursor_x if state.has_smoothed_cursor else None
                )
                target, cursor = detect_control(roi, config, last_cursor_hint)

                if cursor is not None:
                    last_cursor_seen = now
                if target is not None and cursor is not None:
                    has_seen_control = True

                # 控条开始后光标持续消失，视为进入结算阶段。
                if (
                    has_seen_control
                    and last_cursor_seen is not None
                    and (now - last_cursor_seen) * 1000.0
                    >= config.control_end_grace_ms
                ):
                    logger.debug("FishPro 光标持续消失，进入结果处理")
                    reason = REASON_CONTROL_FINISHED
                    success = True
                    break

                observation: Optional[Observation] = None
                action = "idle"

                if target is not None and cursor is not None:
                    cursor_x, cursor_vx = update_cursor_state(
                        cursor, state, config, now
                    )
                    observation = build_observation(
                        target, cursor_x, cursor_vx, state, config, now
                    )
                    action = self._control(observation)
                    valid_frames += 1
                    if observation.inside_target:
                        inside_frames += 1
                    lost_since = None
                else:
                    target_missing = target is None
                    cursor_missing = cursor is None
                    mark_missing_state(
                        state,
                        now,
                        target_missing=target_missing,
                        cursor_missing=cursor_missing,
                    )
                    if lost_since is None:
                        lost_since = now

                    if can_recover_from_missing(
                        state,
                        config,
                        now,
                        target_missing=target_missing,
                        cursor_missing=cursor_missing,
                    ):
                        action = self._handle_missing(target_missing)
                    else:
                        self._executor.release_all()
                        self._reset_after_lost()
                        action = (
                            "target_missing" if target_missing else "cursor_missing"
                        )

                    lost_ms = (now - lost_since) * 1000.0
                    if (
                        not has_seen_control
                        and config.lost_abort_ms > 0
                        and lost_ms > config.lost_abort_ms
                    ):
                        logger.warning("FishPro 控条识别超时，交给 Pipeline 恢复")
                        reason = REASON_LOST_ABORT
                        success = False
                        break

                self._log_control(action, observation)
                self._maybe_dump_debug(roi, target, cursor, observation, action)

                delay = self._rng.uniform(config.loop_delay_min, config.loop_delay_max)
                if delay > 0:
                    self._sleep(delay)

        except Exception:
            logger.exception("FishPro 控条异常")
            reason = REASON_ERROR
            success = False
        finally:
            self._executor.release_all()
            self._finalize_learning()

        elapsed = self._clock() - started_at
        result = SessionResult(
            success=success,
            reason=reason,
            frames=frames,
            valid_frames=valid_frames,
            inside_frames=inside_frames,
            elapsed_sec=elapsed,
        )
        logger.info(
            "FishPro 控条结束: reason=%s frames=%d valid=%d inside=%.1f%% "
            "elapsed=%.2fs",
            result.reason,
            result.frames,
            result.valid_frames,
            result.inside_ratio * 100.0,
            result.elapsed_sec,
        )
        return result

    # ---- 内部步骤 ----

    def _resolve_roi_rect(
        self, image: Optional[np.ndarray]
    ) -> Optional[tuple[int, int, int, int]]:
        if image is None or getattr(image, "ndim", 0) != 3:
            return None
        height, width = image.shape[:2]
        config = self._config
        if config.use_ratio_roi:
            return resolve_roi(config, width, height)
        return tuple(screen.map_rect(config.roi_px))

    def _control(self, observation: Observation) -> str:
        """单帧控制：更新学习、算规则动作、合并残差并驱动按键。"""
        config = self._config
        state = self._state
        state.last_observation = observation
        state.last_valid_observation_time = observation.timestamp
        clear_missing_tracking(state)

        if (
            self._policy is not None
            and state.learning_enabled
            and state.learning_pending is not None
        ):
            try:
                transition = self._policy.update_from_transition(
                    state.learning_pending, observation
                )
                state.learning_total_reward += float(transition.get("reward", 0.0))
                state.learning_last_reward = self._policy.last_reward
                state.learning_last_loss = self._policy.last_loss
                state.learning_sample_count = self._policy.sample_count
                state.learning_update_count = self._policy.update_count
            except Exception as exc:
                logger.warning("FishPro 学习更新失败: %s", exc)
            finally:
                state.learning_pending = None
        elif not state.learning_enabled:
            state.learning_pending = None

        rule_action = compute_rule_action(
            observation, state, config, observation.timestamp, self._rng
        )

        residual = 0.0
        if self._policy is not None:
            try:
                residual = self._policy.predict(observation)
                if state.learning_enabled:
                    residual = self._policy.explore(
                        residual, observation, self._rng
                    )
            except Exception as exc:
                logger.warning("FishPro 残差预测失败: %s", exc)
                residual = 0.0

        final_action = merge_policy_action(
            observation, rule_action, residual, state, config
        )
        action = self._executor.apply(observation, final_action, state)

        if self._policy is not None and state.learning_enabled:
            try:
                state.learning_pending = self._policy.build_pending_record(
                    observation,
                    rule_action,
                    state.last_residual_action,
                    final_action,
                )
            except Exception as exc:
                logger.warning("FishPro 记录学习样本失败: %s", exc)
                state.learning_pending = None

        return action

    def _handle_missing(self, target_missing: bool) -> str:
        """丢帧宽限内：静默态保持静默，否则按衰减动作继续输出。"""
        state = self._state
        observation = state.last_observation
        if observation is None:
            state.active_mode = "missing"
            return "missing_no_obs"

        reason = "target" if target_missing else "cursor"
        if state.center_silence_active or observation.in_center_no_move:
            state.active_mode = f"silent_{reason}"
            return self._executor.apply(observation, 0.0, state)

        fallback = missing_fallback_action(
            state, self._config, target_missing=target_missing
        )
        state.active_mode = f"recover_{reason}"
        return self._executor.apply(observation, fallback, state)

    def _reset_after_lost(self) -> None:
        """丢帧超限：重置平滑与节奏状态，保留学习进度与调试计数。"""
        state = self._state
        preserved = {
            "learning_total_reward": state.learning_total_reward,
            "learning_sample_count": state.learning_sample_count,
            "learning_update_count": state.learning_update_count,
            "learning_last_reward": state.learning_last_reward,
            "learning_last_loss": state.learning_last_loss,
            "debug_frame_count": state.debug_frame_count,
        }

        reset_control_state(state)

        # 上一条待评估样本对应的后继观测已丢失，直接丢弃避免污染学习。
        state.learning_pending = None
        for name, value in preserved.items():
            setattr(state, name, value)

    def _finalize_learning(self) -> None:
        if self._policy is None or not self._state.learning_enabled:
            return
        try:
            saved = self._policy.save_all(force_history=True)
            logger.info(
                "FishPro 学习产物%s: %s",
                "已保存" if saved else "保存失败",
                self._policy.summary(),
            )
        except Exception as exc:
            logger.warning("FishPro 保存学习产物失败: %s", exc)

    # ---- 观测输出 ----

    def _log_control(
        self, action: str, observation: Optional[Observation]
    ) -> None:
        config = self._config
        state = self._state
        now = self._clock()
        status = (
            f"{state.active_mode}|{action}|{state.held_direction}"
            f"|{state.center_silence_active}"
        )
        changed = status != state.last_logged_status
        expired = now - state.last_log_time >= config.control_log_interval_sec
        if not changed and not expired:
            return

        state.last_logged_status = status
        state.last_log_time = now

        if observation is None:
            logger.debug(
                "FishPro 控条: mode=%s action=%s held=%s",
                state.active_mode,
                action,
                state.held_direction or "-",
            )
            return

        logger.debug(
            "FishPro 控条: mode=%s action=%s learn=%s cursor=%.1f pred=%.1f "
            "target=%.1f/%.1f error=%+.1f perr=%+.1f vx=%+.1f tvx=%+.1f "
            "rvx=%+.1f rule=%+.3f residual=%+.3f final=%+.3f inside=%s "
            "edge=%+.1f reward=%+.3f held=%s",
            state.active_mode,
            action,
            "ON" if state.learning_enabled else "OFF",
            observation.cursor_x,
            observation.predicted_cursor_x,
            observation.target_center_x,
            observation.control_target_x,
            observation.error_px,
            observation.predicted_error_px,
            observation.cursor_vx,
            observation.target_vx,
            observation.relative_vx,
            state.last_rule_action,
            state.last_residual_action,
            state.last_final_action,
            "Y" if observation.inside_target else "N",
            observation.edge_margin_px,
            state.learning_last_reward,
            state.held_direction or "-",
        )

    def _maybe_dump_debug(
        self,
        roi: Optional[np.ndarray],
        target: Optional[Detection],
        cursor: Optional[Detection],
        observation: Optional[Observation],
        action: str,
    ) -> None:
        config = self._config
        if not config.debug_enabled or self._debug_dir is None or roi is None:
            return
        state = self._state
        if config.debug_frame_limit and state.debug_frame_count >= config.debug_frame_limit:
            return

        now = self._clock()
        if (
            state.last_debug_frame_time > 0.0
            and now - state.last_debug_frame_time < config.debug_frame_interval_sec
        ):
            return
        state.last_debug_frame_time = now

        try:
            frame = render_debug_frame(
                roi, target, cursor, observation, state, action, config
            )
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            path = self._debug_dir / f"frame_{state.debug_frame_count:05d}.png"
            cv2.imwrite(str(path), frame)
            state.debug_frame_count += 1
        except Exception as exc:
            logger.warning("FishPro 调试帧写入失败: %s", exc)


def render_debug_frame(
    roi: np.ndarray,
    target: Optional[Detection],
    cursor: Optional[Detection],
    observation: Optional[Observation],
    state: ControlState,
    action: str,
    config: FishProConfig,
) -> np.ndarray:
    """绘制 ROI、绿条、光标、安全区与控制/学习指标。"""
    frame = roi.copy()
    roi_h, roi_w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (roi_w - 1, roi_h - 1), (255, 0, 0), 1)

    if target is not None:
        tx, ty, tw, th = target.box
        cv2.rectangle(frame, (tx, ty), (tx + tw, ty + th), (0, 255, 0), 1)
        center = int(round(target.center_x))
        cv2.line(frame, (center, 0), (center, roi_h - 1), (0, 255, 0), 1)
        if observation is not None:
            safe_margin = effective_safe_margin_px(
                observation.target_width, observation.target_vx, config
            )
            safe_left = int(round(observation.target_left + safe_margin))
            safe_right = int(round(observation.target_right - safe_margin))
            cv2.line(frame, (safe_left, 0), (safe_left, roi_h - 1), (0, 180, 0), 1)
            cv2.line(
                frame, (safe_right, 0), (safe_right, roi_h - 1), (0, 180, 0), 1
            )

    if cursor is not None:
        cx, cy, cw, ch = cursor.box
        cv2.rectangle(frame, (cx, cy), (cx + cw, cy + ch), (0, 255, 255), 1)
        center = int(round(cursor.center_x))
        cv2.line(frame, (center, 0), (center, roi_h - 1), (0, 255, 255), 1)
    if state.has_smoothed_cursor:
        smoothed = int(round(state.smoothed_cursor_x))
        if 0 <= smoothed < roi_w:
            cv2.line(frame, (smoothed, 0), (smoothed, roi_h - 1), (255, 255, 0), 1)

    header = np.zeros((118, roi_w, 3), dtype=np.uint8)
    lines = [
        f"mode={state.active_mode} action={action} "
        f"held={state.held_direction or '-'} "
        f"silent={'Y' if state.center_silence_active else 'N'}",
        f"rule={state.last_rule_action:+.3f} "
        f"residual={state.last_residual_action:+.3f} "
        f"final={state.last_final_action:+.3f} "
        f"smooth={state.smoothed_action:+.3f}",
    ]
    if observation is not None:
        lines.append(
            f"err={observation.error_px:+.1f} "
            f"perr={observation.predicted_error_px:+.1f} "
            f"vx={observation.cursor_vx:+.1f} "
            f"tvx={observation.target_vx:+.1f} "
            f"inside={'Y' if observation.inside_target else 'N'} "
            f"edge={observation.edge_margin_px:+.1f}"
        )
        lines.append(
            f"center={observation.target_center_x:.1f} "
            f"ctrl={observation.control_target_x:.1f} "
            f"nm={'Y' if observation.in_center_no_move else 'N'} "
            f"reentry={dynamic_center_reentry_px(observation.target_width, config)}px"
        )
    else:
        lines.append("obs=<none>")
        lines.append(
            f"missing target={state.target_missing_since > 0.0} "
            f"cursor={state.cursor_missing_since > 0.0}"
        )
    lines.append(
        f"learn={'ON' if state.learning_enabled else 'OFF'} "
        f"samples={state.learning_sample_count} "
        f"updates={state.learning_update_count} "
        f"reward={state.learning_last_reward:+.3f} "
        f"loss={state.learning_last_loss:.4f}"
    )

    for index, text in enumerate(lines):
        cv2.putText(
            header,
            text,
            (8, 20 + index * 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (255, 255, 255),
            1,
        )

    return np.vstack([header, frame])
