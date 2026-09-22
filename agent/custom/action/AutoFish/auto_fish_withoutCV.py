"""新版钓鱼的 Maa 适配：识别、有限输入段、再次识别，流程仍由 Pipeline 管理。"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from utils import pienv
from utils.logger import logger
from utils.maafocus import PrintT

from .fish_control import KEY_A, KEY_D, FishDecision, FishingEngine, should_finish_control
from .fish_learning import FishLearningSession
from .fish_params import FishControlConfig, MAX_KEY_HOLD_MS, load_fish_engine_config
from .fish_vision import CONTROL_ROI, detect_control_boxes, normalize_control_image

__all__ = ["AutoFishWithoutCV", "FishControlVisible"]


class _Stopped(Exception):
    pass


class _KeyInput:
    """不跨截图持键；原生 Job 自身的阻塞不属于 Python 可中断的范围。"""

    def __init__(self, controller, stopping, clock, sleep):
        self.controller = controller
        self.stopping = stopping
        self.clock = clock
        self.sleep = sleep

    @staticmethod
    def _wait(job) -> None:
        job.wait()
        if not job.succeeded:
            raise RuntimeError("Maa controller job failed")

    def wait_interruptibly(self, seconds: float) -> None:
        deadline = self.clock() + max(0.0, seconds)
        while True:
            if self.stopping():
                raise _Stopped()
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.sleep(min(0.005, remaining))

    def execute(self, decision: FishDecision) -> tuple[float, float, float]:
        if self.stopping():
            raise _Stopped()
        if decision.key is None:
            now = self.clock()
            return now, now, 0.0
        if decision.key not in (KEY_A, KEY_D) or not np.isfinite(decision.duration_ms):
            raise ValueError("invalid fishing input")
        started = self.clock()
        release_started = started
        try:
            # 即便按下 Job 报错，也可能已发生部分输入，因此 finally 总是尝试松键。
            self._wait(self.controller.post_key_down(decision.key))
            started = self.clock()
            self.wait_interruptibly(min(MAX_KEY_HOLD_MS, max(0.0, decision.duration_ms)) / 1000)
        finally:
            release_started = self.clock()
            self._wait(self.controller.post_key_up(decision.key))
        if self.stopping():
            raise _Stopped()
        return started, self.clock(), max(0.0, release_started - started) * 1000

    def release_all(self) -> bool:
        success = True
        for key in (KEY_A, KEY_D):
            try:
                self._wait(self.controller.post_key_up(key))
            except Exception as exc:
                success = False
                logger.warning("释放钓鱼按键失败: key=%s error=%s", key, type(exc).__name__)
        return success


def _controller_profile(controller) -> str:
    configured = pienv.controller()
    if configured is None:
        # 无 PI 控制器信息时仅供本次规则控制；调用处不启用跨设备模型。
        return ""
    return json.dumps(asdict(configured), ensure_ascii=True, sort_keys=True)


class _FishingRuntime:
    def __init__(self, context, config: FishControlConfig, *, clock=None, sleep=None):
        self.context = context
        self.config = config
        self.controller = context.tasker.controller
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.input = _KeyInput(self.controller, lambda: context.tasker.stopping, self.clock, self.sleep)
        self.engine = FishingEngine(config)
        self.learning: FishLearningSession | None = None
        self._learning_issue_reported = False

    def _report_learning_issue(self) -> None:
        if self.learning is not None and self.learning.issue and not self._learning_issue_reported:
            self._learning_issue_reported = True
            logger.warning("钓鱼学习降级: %s", self.learning.issue)
            PrintT(self.context, "fish.learning_unavailable")

    def run(self) -> bool:
        success = False
        try:
            if not self.input.release_all():
                raise RuntimeError("failed to clear fishing input")
            if self.context.tasker.stopping:
                raise _Stopped()
            profile = _controller_profile(self.controller)
            if self.config.learning_mode != "off" and not profile:
                logger.warning("钓鱼缺少 PI 控制器配置，本轮仅使用规则")
                PrintT(self.context, "fish.learning_unavailable")
            else:
                self.learning = FishLearningSession(self.config, profile, Path.cwd())
                self._report_learning_issue()
            success = self._loop()
        except _Stopped:
            logger.debug("钓鱼控条因任务停止退出")
        except Exception:
            logger.exception("钓鱼控条异常，交由 Pipeline 处理")
        finally:
            released = self.input.release_all()
            success = success and released and not self.context.tasker.stopping
            # 若输入释放失败，不进行潜在阻塞的存储操作。
            if self.learning is not None and released:
                try:
                    if not self.learning.finish():
                        logger.warning("钓鱼学习保存失败: %s", self.learning.issue)
                        PrintT(self.context, "fish.learning_save_failed")
                except Exception:
                    logger.exception("钓鱼学习保存异常")
                    PrintT(self.context, "fish.learning_save_failed")
        return success

    def _invalidate(self, reason: str) -> None:
        if self.learning is not None:
            self.learning.invalidate(reason)

    def _loop(self) -> bool:
        cfg = self.config
        last_cursor_seen = None
        has_seen_control = False
        lost_since = None
        unchanged_since = None
        previous_roi = None
        awaiting_change = False
        frame_id = 0
        last_log = self.clock()
        logger.debug("钓鱼预测引擎开始: learning=%s", cfg.learning_mode)
        while not self.context.tasker.stopping:
            capture_started = self.clock()
            job = self.controller.post_screencap()
            self.input._wait(job)
            if self.context.tasker.stopping:
                raise _Stopped()
            # 失败的截图绝不读取旧缓存；本动作没有另一个抢截图的线程。
            image = self.controller.cached_image
            capture_finished = self.clock()
            image = normalize_control_image(image)
            if image is None:
                PrintT(self.context, "fish.unsupported_frame")
                return False
            green, cursor = detect_control_boxes(image, self.engine.last_cursor_center)
            now = self.clock()
            frame_id += 1
            if cursor is not None:
                last_cursor_seen = now
            if should_finish_control(has_seen_control, last_cursor_seen, now, cfg.control_end_grace_ms):
                self._invalidate("control_finished")
                logger.debug("钓鱼光标持续消失，交由 Pipeline 判断钓获或逃脱")
                return True

            observation = None
            if green is not None and cursor is not None:
                observation = self.engine.observe(
                    green, cursor, (capture_started + capture_finished) / 2,
                    capture_ms=(capture_finished - capture_started) * 1000,
                    frame_id=frame_id,
                )
            if observation is None:
                self._invalidate("missing_detection")
                previous_roi = None
                awaiting_change = False
                if lost_since is None:
                    lost_since = now
                lost_ms = (now - lost_since) * 1000
                if lost_ms >= cfg.lost_timeout_ms:
                    self.engine.reset_tracking()
                if lost_ms >= cfg.lost_abort_ms:
                    logger.warning("钓鱼控条持续无有效观测，停止本轮控制")
                    return False
                if cfg.loop_interval_ms:
                    self.input.wait_interruptibly(cfg.loop_interval_ms / 1000)
                continue

            has_seen_control = True
            lost_since = None
            x, y, width, height = CONTROL_ROI
            roi = image[y : y + height, x : x + width]
            duplicate = previous_roi is not None and np.array_equal(previous_roi, roi)
            previous_roi = roi.copy()
            if duplicate:
                self._invalidate("duplicate_frame")
            else:
                awaiting_change = False
                unchanged_since = None
            if duplicate and awaiting_change:
                # 上一次输入后画面完全未变，先等待新的视觉证据，不盲目重复按键。
                if unchanged_since is None:
                    unchanged_since = now
                if (now - unchanged_since) * 1000 >= cfg.lost_abort_ms:
                    logger.warning("钓鱼输入后控条画面持续不变，停止本轮控制")
                    return False
                if cfg.loop_interval_ms:
                    self.input.wait_interruptibly(cfg.loop_interval_ms / 1000)
                continue

            residual = 0.0
            if self.learning is not None:
                if not duplicate:
                    self.learning.observe(observation)
                residual = self.learning.predict(observation)
                self._report_learning_issue()
            decision = self.engine.decide(observation, residual)
            started, finished, held_ms = self.input.execute(decision)
            self.engine.remember_execution(decision)
            if self.learning is not None:
                self.learning.record_action(
                    observation, decision, started_at=started, finished_at=finished, held_ms=held_ms
                )
            awaiting_change = decision.key is not None
            if now - last_log >= 0.5:
                last_log = now
                logger.debug(
                    "钓鱼预测状态: frame=%d capture_ms=%.1f mode=%s error=%.1f "
                    "predicted=%.1f relative_v=%.1f key=%s held_ms=%.1f residual=%.3f",
                    frame_id, observation.capture_ms, decision.mode, observation.error_px,
                    observation.predicted_error_px, observation.relative_vx,
                    decision.key, held_ms, decision.residual,
                )
            if cfg.loop_interval_ms:
                self.input.wait_interruptibly(cfg.loop_interval_ms / 1000)
        raise _Stopped()


@AgentServer.custom_action("auto_fish_without_cv")
class AutoFishWithoutCV(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        config = load_fish_engine_config(argv.custom_action_param)
        return CustomAction.RunResult(success=_FishingRuntime(context, config).run())


@AgentServer.custom_recognition("fish_control_visible")
class FishControlVisible(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        if context.tasker.stopping:
            return None
        try:
            green, cursor = detect_control_boxes(argv.image)
            if green is None or cursor is None:
                return None
            height, width = argv.image.shape[:2]
            x, y, box_width, box_height = cursor
            box = [
                int(round(x * width / 1280)), int(round(y * height / 720)),
                max(1, int(round(box_width * width / 1280))),
                max(1, int(round(box_height * height / 720))),
            ]
            return CustomRecognition.AnalyzeResult(
                box=box, detail={"reference_size": [1280, 720], "green_box": green, "cursor_box": cursor}
            )
        except Exception:
            logger.exception("钓鱼控条入口识别异常")
            return None
