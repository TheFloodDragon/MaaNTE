import math
import threading
import time
from typing import Any, Callable

from maa.context import Context
from utils.logger import logger

from .map_locator import MapLocator
from .waypoint_navigator import WaypointNavigator
from .route_model import RouteSession, SourceSize, Waypoint


class RouteRunner:
    """按顺序执行可变路线会话中的路径点。"""

    def __init__(
        self,
        context: Context,
        route: RouteSession,
        *,
        angle_backend: str = "auto",
        position_backend: str = "map",
        tolerance: float = 80.0,
        frame_interval: float = 0.1,
        max_duration: float = 120.0,
        sample_timeout: float = 5.0,
        debug: bool = False,
        on_frame: Callable[[Any, Any], None] | None = None,
    ) -> None:
        self.context = context
        self.route = route
        self.angle_backend = angle_backend
        self.position_backend = position_backend
        self.tolerance = float(tolerance)
        self.frame_interval = float(frame_interval)
        self.max_duration = float(max_duration)
        self.sample_timeout = float(sample_timeout)
        if (
            not math.isfinite(self.tolerance)
            or self.tolerance <= 0
            or any(
                not math.isfinite(value) or value <= 0
                for value in (
                    self.frame_interval,
                    self.max_duration,
                    self.sample_timeout,
                )
            )
        ):
            raise ValueError(
                "navigation tolerance and time budgets must be finite and valid"
            )
        self.frame_interval = max(0.05, self.frame_interval)
        self.debug = debug
        self.on_frame = on_frame
        self.navigator: WaypointNavigator | None = None
        self._source_size: SourceSize = MapLocator.MAP_SIZE
        self._current_point: Waypoint | None = None
        self._moving_target: tuple[int, int, Waypoint] | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.context.tasker.stopping or self.navigator is not None:
            return
        self.navigator = WaypointNavigator(
            self.context,
            angle_backend=self.angle_backend,
            position_backend=self.position_backend,
            tolerance=self.tolerance,
            max_duration=self.max_duration,
            sample_timeout=self.sample_timeout,
            frame_interval=self.frame_interval,
            debug=self.debug,
            on_frame=self._on_frame,
            should_cancel=self._should_cancel,
        )
        if self.navigator.locator is None:
            self._source_size = MapLocator.MAP_SIZE
        else:
            self._source_size = (
                self.navigator.locator.origin_w,
                self.navigator.locator.origin_h,
            )

    def run_until_stopped(
        self,
        *,
        on_tick: Callable[[], None] | None = None,
        stop_when_route_done: bool = False,
    ) -> str:
        try:
            self.start()
            if self.context.tasker.stopping:
                self.route.stop()
                return "stopped"
            assert self.navigator is not None

            while not self.context.tasker.stopping:
                if on_tick is not None:
                    on_tick()
                if self.context.tasker.stopping:
                    break
                target = self.route.target_snapshot()
                if target is None:
                    payload = self.route.payload()
                    if stop_when_route_done and payload["status"] in {
                        "arrived",
                        "cleared",
                        "empty",
                        "stopped",
                        "failed",
                    }:
                        return str(payload["status"])
                    started = time.perf_counter()
                    self.navigator.update()
                    self.navigator.sleep_remaining(started)
                    continue

                with self._lock:
                    self._moving_target = target
                try:
                    arrived = self.navigator.move_to(target[2])
                except Exception as exc:
                    arrived = False
                    self.navigator.reason = (
                        "configuration_invalid"
                        if isinstance(exc, (ValueError, TypeError, AssertionError))
                        else "error"
                    )
                    logger.error(
                        "RouteRunner movement failed: target=%s error=%s", target, exc
                    )
                finally:
                    with self._lock:
                        self._moving_target = None

                # move_to may leave W held when its final key-up fails. Give
                # cleanup one more attempt before idle sampling or a new route.
                # Persistent input failure ends the service instead of retrying
                # business movement or polling indefinitely with a held key.
                if not self.navigator.release():
                    self.route.fail(*target, "controller_failed")
                    logger.error("RouteRunner could not release movement input")
                    return "failed"
                if self.context.tasker.stopping:
                    break
                if arrived:
                    self.route.advance(*target)
                elif self.route.fail(*target, self.navigator.reason or "failed"):
                    logger.warning(
                        "RouteRunner route failed: target=%s reason=%s",
                        target,
                        self.navigator.reason,
                    )

            self.route.stop()
            return "stopped"
        finally:
            with self._lock:
                self._moving_target = None
            if self.navigator is not None:
                self.navigator.release()

    def close(self) -> None:
        if self.navigator is not None:
            self.navigator.close()
            self.navigator = None

    def update_current_frame(self) -> Waypoint | None:
        if self.context.tasker.stopping:
            return None
        self.start()
        if self.context.tasker.stopping:
            return None
        assert self.navigator is not None
        self.navigator.update()
        return None if self.context.tasker.stopping else self.current_point()

    def source_size(self) -> SourceSize:
        return self._source_size

    def current_point(self) -> Waypoint | None:
        with self._lock:
            return self._current_point

    def _on_frame(self, location: Any, angle: Any) -> None:
        if self._should_cancel():
            return
        if location.found and location.point is not None:
            with self._lock:
                self._current_point = location.point
        if self.on_frame is not None:
            self.on_frame(location, angle)

    def _should_cancel(self) -> bool:
        if self.context.tasker.stopping:
            return True
        with self._lock:
            target = self._moving_target
        return target is not None and not self.route.matches_target(*target)
