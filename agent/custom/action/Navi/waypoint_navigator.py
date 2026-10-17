import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from maa.context import Context
from utils.task_control import wait_until_cancelled

from ..Common.logger import get_logger
from .angle_predictor import AnglePredictionResult, AnglePredictor
from .coordinate_position import CoordinatePositionProvider
from .debug_windows import close_debug_windows, pump_debug_windows
from .map_locator import MapLocator

logger = get_logger(__name__)

_KEY_W = 87
_NETWORK_FRAME_INTERVAL = 1.0 / 60.0
_DEFAULT_MAX_DURATION = 120.0


class _ControllerFailedError(RuntimeError):
    pass


@dataclass
class AnglePidController:
    """将角度误差平滑转换为受限的鼠标转向指令。"""

    kp: float = 0.85
    ki: float = 0.04
    kd: float = 0.10
    output_limit: float = 35.0
    integral_limit: float = 120.0
    deadband: float = 4.0
    max_dt: float = 0.25
    _integral: float = 0.0
    _last_error: float | None = None
    _last_time: float | None = None

    def reset(self) -> None:
        self._integral = 0.0
        self._last_error = None
        self._last_time = None

    def update(self, error: float, now: float) -> float:
        if abs(error) <= self.deadband:
            self.reset()
            return 0.0

        if self._last_time is None:
            dt = self.max_dt
            derivative = 0.0
        else:
            dt = max(1e-3, min(self.max_dt, now - self._last_time))
            derivative = (
                0.0 if self._last_error is None else (error - self._last_error) / dt
            )

        if self._last_error is not None and error * self._last_error < 0:
            self._integral = 0.0
        self._integral += error * dt
        self._integral = max(
            -self.integral_limit,
            min(self.integral_limit, self._integral),
        )

        output = self.kp * error + self.ki * self._integral + self.kd * derivative
        output = max(-self.output_limit, min(self.output_limit, output))

        self._last_error = error
        self._last_time = now
        return output


class WaypointNavigator:
    """根据实时位置和朝向，将角色移动到单个地图路径点。"""

    def __init__(
        self,
        context: Context,
        *,
        angle_backend: str = "auto",
        position_backend: str = "map",
        tolerance: float = 80.0,
        max_duration: float | None = _DEFAULT_MAX_DURATION,
        sample_timeout: float = 5.0,
        frame_interval: float = 0.1,
        debug: bool = False,
        on_frame: Callable[[Any, Any], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.context = context
        self.controller = context.tasker.controller
        self.tolerance = float(tolerance)
        self.max_duration = float(
            _DEFAULT_MAX_DURATION if max_duration is None else max_duration
        )
        self.sample_timeout = float(sample_timeout)
        self.frame_interval = float(frame_interval)
        if (
            not math.isfinite(self.tolerance)
            or self.tolerance <= 0
            or not math.isfinite(self.max_duration)
            or self.max_duration <= 0
            or not math.isfinite(self.sample_timeout)
            or self.sample_timeout <= 0
            or not math.isfinite(self.frame_interval)
            or self.frame_interval <= 0
        ):
            raise ValueError(
                "navigation tolerance and time budgets must be finite and valid"
            )
        self.frame_interval = max(0.05, self.frame_interval)
        self.debug = debug
        self.on_frame = on_frame
        self.should_cancel = should_cancel
        self.reason: str | None = None
        self.turn_pixels_per_degree = 10.0
        self.max_turn_degrees = 35.0
        self.align_threshold = 4.0
        self.move_pulse = 0.12
        self.key_refresh_interval = 0.5
        self.turn_pid = AnglePidController(
            output_limit=self.max_turn_degrees,
            deadband=self.align_threshold,
        )
        self.w_down = False
        self._last_w_down_at = 0.0
        self.current_point: tuple[int, int] | None = None
        self.locator: MapLocator | None = None
        self.position_provider: CoordinatePositionProvider | None = None
        self.predictor: AnglePredictor | None = None
        try:
            if self._cancelled():
                self.reason = "cancelled"
                return
            self.position_provider = CoordinatePositionProvider(
                position_backend,
                debug=debug,
            )
            if self._cancelled():
                self.reason = "cancelled"
                return
            if self.position_provider.uses_visual_positioning():
                self.locator = MapLocator(debug=debug)
                if self._cancelled():
                    self.reason = "cancelled"
                    return
                self.predictor = AnglePredictor(
                    backend=angle_backend,
                    threshold=0.0,
                    debug=debug,
                )
            else:
                self.frame_interval = _NETWORK_FRAME_INTERVAL
                logger.info(
                    "Navi network pose active: visual angle predictor disabled, "
                    "control_rate=60Hz"
                )
        except Exception:
            self.close()
            raise

    def _cancelled(self) -> bool:
        return self.context.tasker.stopping or (
            self.should_cancel is not None and self.should_cancel()
        )

    def _controller_job(self, post: Callable[..., Any], *args: Any) -> Any:
        try:
            job = post(*args)
            job.wait()
            if not job.succeeded:
                raise _ControllerFailedError("controller job did not succeed")
            return job
        except _ControllerFailedError:
            raise
        except Exception as exc:
            raise _ControllerFailedError(str(exc)) from exc

    def update(self) -> tuple[Any, Any] | None:
        if self._cancelled():
            self.reason = "cancelled"
            return None
        assert self.position_provider is not None
        if self.position_provider.uses_visual_positioning():
            job = self._controller_job(self.controller.post_screencap)
            if self._cancelled():
                self.reason = "cancelled"
                return None
            frame = job.get()
            if self._cancelled():
                self.reason = "cancelled"
                return None
            if frame is None:
                self.reason = "frame_missing"
                return None
            assert self.predictor is not None
            location = self.position_provider.locate(self.locator, frame)
            if self._cancelled():
                self.reason = "cancelled"
                return None
            angle = self.predictor.predict(frame)
        else:
            location = self.position_provider.locate(None, None)
            if self._cancelled():
                self.reason = "cancelled"
                return None
            angle = AnglePredictionResult(
                found=location.found and location.camera_heading is not None,
                angle=location.camera_heading,
                confidence=1.0 if location.camera_heading is not None else 0.0,
            )
        if self._cancelled():
            self.reason = "cancelled"
            return None
        if location.found and location.point is not None:
            self.current_point = location.point
        if self.on_frame is not None:
            self.on_frame(location, angle)
        if self._cancelled():
            self.reason = "cancelled"
            return None
        return location, angle

    def move_to(self, target: tuple[int, int]) -> bool:
        self.reason = None
        arrived = False
        try:
            arrived = self._move_to(target)
        except _ControllerFailedError as exc:
            self.reason = "controller_failed"
            logger.error(
                "WaypointNavigator controller failed: target=%s error=%s", target, exc
            )
        except Exception as exc:
            self.reason = (
                "configuration_invalid"
                if isinstance(exc, (ValueError, TypeError, AssertionError))
                else "error"
            )
            logger.error(
                "WaypointNavigator failed: target=%s reason=%s error=%s",
                target,
                self.reason,
                exc,
            )
        finally:
            if not self.release():
                arrived = False
        if self._cancelled():
            self.reason = "cancelled"
            return False
        return arrived

    def _move_to(self, target: tuple[int, int]) -> bool:
        target_x, target_y = target
        if not math.isfinite(target_x) or not math.isfinite(target_y):
            raise ValueError("target coordinates must be finite")
        deadline = time.monotonic() + self.max_duration
        missing_since: float | None = None
        last_log_time = 0.0
        self.turn_pid.reset()

        while True:
            if self._cancelled():
                self.reason = "cancelled"
                return False
            if time.monotonic() >= deadline:
                self.reason = "timeout"
                return False
            if (
                missing_since is not None
                and time.monotonic() >= missing_since + self.sample_timeout
            ):
                return False
            started = time.perf_counter()
            result = self.update()
            if self._cancelled():
                self.reason = "cancelled"
                return False
            if time.monotonic() >= deadline:
                self.reason = "timeout"
                return False

            missing_reason = None
            if result is None:
                missing_reason = self.reason or "frame_missing"
            else:
                location, angle = result
                if (
                    not location.found
                    or location.point is None
                    or not all(math.isfinite(value) for value in location.point)
                ):
                    missing_reason = "position_missing"
                elif (
                    not angle.found
                    or angle.angle is None
                    or not math.isfinite(angle.angle)
                ):
                    missing_reason = "angle_missing"
            if missing_reason is not None:
                if not self.release():
                    return False
                self.reason = missing_reason
                now = time.monotonic()
                if missing_since is None:
                    missing_since = now
                if now >= missing_since + self.sample_timeout:
                    return False
                if not self.sleep_remaining(
                    started, deadline=min(deadline, missing_since + self.sample_timeout)
                ):
                    self.reason = "cancelled"
                    return False
                continue

            missing_since = None
            self.reason = None
            current_x, current_y = location.point
            dx = target_x - current_x
            dy = target_y - current_y
            distance = math.hypot(dx, dy)
            if distance <= self.tolerance:
                logger.info(
                    "WaypointNavigator arrived: target=%s current=%s distance=%.1f",
                    target,
                    location.point,
                    distance,
                )
                self.reason = "arrived"
                return True

            desired_angle = math.degrees(math.atan2(dx, -dy)) % 360.0
            angle_delta = (desired_angle - angle.angle + 540.0) % 360.0 - 180.0
            turn_degrees = self.turn_pid.update(angle_delta, time.monotonic())
            turn_dx = int(round(turn_degrees * self.turn_pixels_per_degree))
            if not self.press_forward():
                self.reason = "cancelled"
                return False
            if turn_dx != 0:
                if self._cancelled():
                    self.reason = "cancelled"
                    return False
                self._controller_job(self.controller.post_relative_move, turn_dx, 0)
            if not self.sleep_interruptible(
                min(self.move_pulse, max(0.0, deadline - time.monotonic()))
            ):
                self.reason = "cancelled"
                return False

            now = time.monotonic()
            if now - last_log_time >= 2.0:
                logger.debug(
                    "WaypointNavigator moving: target=%s current=%s distance=%.1f "
                    "angle_delta=%.1f turn=%.1f",
                    target,
                    location.point,
                    distance,
                    angle_delta,
                    turn_degrees,
                )
                last_log_time = now
            if not self.sleep_remaining(started, deadline=deadline):
                self.reason = "cancelled"
                return False

    def press_forward(self) -> bool:
        if self._cancelled():
            return False
        now = time.monotonic()
        if self.w_down and now - self._last_w_down_at < self.key_refresh_interval:
            return True
        # A failed/raising key-down may already have reached the controller.
        self.w_down = True
        self._controller_job(self.controller.post_key_down, _KEY_W)
        self._last_w_down_at = now
        return not self._cancelled()

    def release(self) -> bool:
        self.turn_pid.reset()
        self._last_w_down_at = 0.0
        if self.w_down:
            try:
                self._controller_job(self.controller.post_key_up, _KEY_W)
            except _ControllerFailedError as exc:
                self.reason = "controller_failed"
                logger.error("WaypointNavigator release failed: %s", exc)
                return False
            self.w_down = False
        return True

    def close(self) -> None:
        try:
            self.release()
        finally:
            try:
                if self.position_provider is not None:
                    self.position_provider.close()
                    self.position_provider = None
            finally:
                try:
                    if self.locator is not None:
                        self.locator.close()
                        self.locator = None
                finally:
                    try:
                        if self.predictor is not None:
                            self.predictor.close()
                            self.predictor = None
                    finally:
                        if self.debug:
                            close_debug_windows()

    def sleep_remaining(self, started: float, *, deadline: float | None = None) -> bool:
        sleep_time = max(0.0, self.frame_interval - (time.perf_counter() - started))
        if deadline is not None:
            sleep_time = min(sleep_time, max(0.0, deadline - time.monotonic()))
        return self.sleep_interruptible(sleep_time)

    def sleep_interruptible(self, duration: float) -> bool:
        def cancelled() -> bool:
            if self._cancelled():
                return True
            if self.debug:
                pump_debug_windows()
            return self._cancelled()

        completed = wait_until_cancelled(duration, cancelled)
        if not completed:
            self.release()
            self.reason = "cancelled"
        return completed


def load_params(custom_action_param: Any) -> dict[str, Any]:
    if not custom_action_param:
        return {}
    if isinstance(custom_action_param, dict):
        return custom_action_param
    try:
        params = json.loads(custom_action_param)
    except Exception as exc:
        logger.warning("Parse custom_action_param failed: %s", exc)
        return {}
    return params if isinstance(params, dict) else {}
