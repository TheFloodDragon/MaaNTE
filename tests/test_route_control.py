import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
NAVI = ROOT / "agent" / "custom" / "action" / "Navi"
PACKAGE = "_route_control_tests"
SIZE = (11264, 11264)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def module(name, **members):
    result = types.ModuleType(name)
    result.__dict__.update(members)
    return result


class Clock:
    def __init__(self):
        self.now = 100.0
        self.on_sleep = None

    def monotonic(self):
        return self.now

    perf_counter = monotonic

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


class Job:
    def __init__(self, succeeded=True, result=None, on_wait=None):
        self.succeeded = succeeded
        self.result = result
        self.on_wait = on_wait
        self.get_calls = 0

    def wait(self):
        if self.on_wait is not None:
            self.on_wait()
        return self

    def get(self):
        self.get_calls += 1
        return self.result


class MapLocator:
    MAP_SIZE = SIZE
    origin_w, origin_h = SIZE

    def __init__(self, **kwargs):
        self.close = Mock()


class RouteControlTests(unittest.TestCase):
    def setUp(self):
        self.provider = Mock()
        self.provider.uses_visual_positioning.return_value = False
        self.predictor = Mock()
        self.provider_factory = Mock(return_value=self.provider)
        self.predictor_factory = Mock(return_value=self.predictor)
        self.raw_to_map = Mock(return_value=(9, 10))
        logger = Mock()
        stubs = {
            PACKAGE: module(PACKAGE, __path__=[]),
            PACKAGE + ".Navi": module(PACKAGE + ".Navi", __path__=[]),
            PACKAGE + ".Common": module(PACKAGE + ".Common", __path__=[]),
            PACKAGE
            + ".Common.logger": module(
                PACKAGE + ".Common.logger", get_logger=lambda name: logger
            ),
            PACKAGE
            + ".Navi.coordinate_position": module(
                PACKAGE + ".Navi.coordinate_position",
                COORDINATE_MAP_SIZE=SIZE,
                raw_coordinate_to_map=self.raw_to_map,
                CoordinatePositionProvider=self.provider_factory,
            ),
            PACKAGE
            + ".Navi.map_locator": module(
                PACKAGE + ".Navi.map_locator", MapLocator=MapLocator
            ),
            PACKAGE
            + ".Navi.angle_predictor": module(
                PACKAGE + ".Navi.angle_predictor",
                AnglePredictionResult=types.SimpleNamespace,
                AnglePredictor=self.predictor_factory,
            ),
            PACKAGE
            + ".Navi.debug_windows": module(
                PACKAGE + ".Navi.debug_windows",
                close_debug_windows=Mock(),
                pump_debug_windows=Mock(),
            ),
            PACKAGE
            + ".Navi.resource_paths": module(
                PACKAGE + ".Navi.resource_paths",
                resource_base_path=lambda: ROOT / "assets/resource/base",
            ),
            "maa": module("maa", __path__=[]),
            "maa.context": module("maa.context", Context=object),
            "maa.custom_action": module(
                "maa.custom_action",
                CustomAction=type(
                    "CustomAction",
                    (),
                    {
                        "RunArg": types.SimpleNamespace,
                        "RunResult": types.SimpleNamespace,
                    },
                ),
            ),
            "maa.agent": module("maa.agent", __path__=[]),
            "maa.agent.agent_server": module(
                "maa.agent.agent_server",
                AgentServer=types.SimpleNamespace(
                    custom_action=lambda name: lambda cls: cls
                ),
            ),
            "utils": module("utils", __path__=[]),
            "utils.logger": module("utils.logger", logger=logger),
        }
        # Restore every module, including any real maa/utils imports from other tests.
        isolated = patch.dict(sys.modules, stubs)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.control = load_module(
            "utils.task_control", ROOT / "agent/utils/task_control.py"
        )
        self.model = load_module(PACKAGE + ".Navi.route_model", NAVI / "route_model.py")
        self.navigation = load_module(
            PACKAGE + ".Navi.waypoint_navigator", NAVI / "waypoint_navigator.py"
        )
        self.runners = load_module(
            PACKAGE + ".Navi.route_runner", NAVI / "route_runner.py"
        )
        self.local = load_module(
            PACKAGE + ".Navi.local_route_navigation", NAVI / "local_route_navigation.py"
        )
        self.clock = Clock()
        self.control.time = self.clock
        self.navigation.time = self.clock
        self.runners.time = self.clock
        self.controller = Mock()
        self.controller.post_key_down.return_value = Job()
        self.controller.post_key_up.return_value = Job()
        self.controller.post_relative_move.return_value = Job()
        self.controller.post_screencap.return_value = Job(result=object())
        self.context = types.SimpleNamespace(
            tasker=types.SimpleNamespace(stopping=False, controller=self.controller)
        )

    def pose(self, point=(0, 0), heading=0.0, found=True):
        return types.SimpleNamespace(found=found, point=point, camera_heading=heading)

    def navigator(self, **kwargs):
        return self.navigation.WaypointNavigator(
            self.context, tolerance=1.0, max_duration=0.5, sample_timeout=0.1, **kwargs
        )

    def runner(self, waypoints=None):
        route = self.model.RouteSession()
        route.reset(waypoints or [(10, 10), (20, 20)], True, None)
        runner = self.runners.RouteRunner(self.context, route)
        runner.navigator = Mock(reason=None)
        return route, runner

    def test_session_revision_invalidates_every_control_mutation(self):
        route = self.model.RouteSession()
        original = [(10, 10), (20, 20)]
        route.reset(original, True, None)
        original.append((30, 30))
        self.assertEqual(len(route.waypoints), 2)
        for mutate in (
            lambda: route.reset([(10, 10), (20, 20)], True, None),
            lambda: route.start(None),
            lambda: route.add((30, 30)),
            lambda: (route.stop(), route.start(None)),
            lambda: (route.clear(), route.reset([(10, 10)], True, None)),
        ):
            snapshot = route.target_snapshot()
            revision = route.payload()["revision"]
            mutate()
            self.assertGreater(route.payload()["revision"], revision)
            self.assertFalse(route.matches_target(*snapshot))
            self.assertFalse(route.advance(*snapshot))
            self.assertFalse(route.fail(*snapshot, "timeout"))
        snapshot = route.target_snapshot()
        self.assertTrue(route.advance(*snapshot))
        self.assertEqual(route.payload()["status"], "arrived")
        self.assertFalse(route.advance(*snapshot))

    def test_legacy_websocket_messages_increment_revision(self):
        route = self.model.RouteSession()
        messages = [
            {
                "type": "route-set",
                "waypoints": [{"pixelX": 10, "pixelY": 10}],
                "start": True,
            },
            {"type": "navi-route-add", "pixelX": 20, "pixelY": 20},
            {"type": "route-stop"},
            {"type": "route-start"},
            {"type": "navi-route-clear"},
        ]
        for revision, message in enumerate(messages, 1):
            reply = self.model.handle_route_message(message, route, SIZE)
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["route"]["revision"], revision)
        self.assertFalse(
            self.model.handle_route_message({"type": "unknown"}, route, SIZE)["ok"]
        )

    def test_arrival_races_revisit_new_generation_start(self):
        for mutation in ("reset_start", "stop_start", "add", "clear_reset"):
            with self.subTest(mutation=mutation):
                route, runner = self.runner()
                visited = []

                def move(target):
                    visited.append(target)
                    if len(visited) == 1:
                        if mutation == "reset_start":
                            route.reset([(10, 10), (20, 20)], False, None)
                            route.start(None)
                        elif mutation == "stop_start":
                            route.stop()
                            route.start(None)
                        elif mutation == "add":
                            route.add((30, 30))
                        else:
                            route.clear()
                            route.reset([(10, 10), (20, 20)], True, None)
                        self.assertTrue(runner._should_cancel())
                    return True

                runner.navigator.move_to.side_effect = move
                self.assertEqual(
                    runner.run_until_stopped(stop_when_route_done=True), "arrived"
                )
                expected = [(10, 10), (10, 10), (20, 20)]
                if mutation == "add":
                    expected.append((30, 30))
                self.assertEqual(visited, expected)
                self.assertIsNone(runner._moving_target)
                runner.navigator.release.assert_called()

    def test_cancelled_old_target_cannot_fail_new_route(self):
        route, runner = self.runner()
        visits = []

        def move(target):
            visits.append(target)
            if len(visits) == 1:
                route.reset([(10, 10)], True, None)
                runner.navigator.reason = "cancelled"
                return False
            return True

        runner.navigator.move_to.side_effect = move
        self.assertEqual(runner.run_until_stopped(stop_when_route_done=True), "arrived")
        self.assertEqual(visits, [(10, 10), (10, 10)])

    def test_route_stop_does_not_retry_cancelled_target(self):
        route, runner = self.runner()
        runner.navigator.move_to.side_effect = lambda target: route.stop() or False
        self.assertEqual(runner.run_until_stopped(stop_when_route_done=True), "stopped")
        runner.navigator.move_to.assert_called_once()

    def test_terminal_failures_do_not_retry_move_to(self):
        for reason in (
            "timeout",
            "frame_missing",
            "position_missing",
            "angle_missing",
            "controller_failed",
            "configuration_invalid",
        ):
            with self.subTest(reason=reason):
                route, runner = self.runner()
                runner.navigator.reason = reason
                runner.navigator.move_to.return_value = False
                self.assertEqual(
                    runner.run_until_stopped(stop_when_route_done=True), "failed"
                )
                self.assertEqual(route.payload()["reason"], reason)
                self.assertFalse(route.payload()["active"])
                runner.navigator.move_to.assert_called_once()
                runner.navigator.release.assert_called()

    def test_runner_exception_releases_and_marks_failed(self):
        route, runner = self.runner()
        runner.navigator.move_to.side_effect = ValueError("invalid configuration")
        self.assertEqual(runner.run_until_stopped(stop_when_route_done=True), "failed")
        self.assertEqual(route.reason, "configuration_invalid")
        runner.navigator.release.assert_called()
        self.assertIsNone(runner._moving_target)

    def test_runner_stop_after_arrival_cannot_advance(self):
        route, runner = self.runner()

        def move(target):
            self.context.tasker.stopping = True
            return True

        runner.navigator.move_to.side_effect = move
        self.assertEqual(runner.run_until_stopped(stop_when_route_done=True), "stopped")
        self.assertEqual(route.current_index, 0)
        runner.navigator.release.assert_called()

    def test_stopped_runner_does_not_initialize_or_sample(self):
        self.context.tasker.stopping = True
        runner = self.runners.RouteRunner(self.context, self.model.RouteSession())
        runner.start()
        self.assertIsNone(runner.update_current_frame())
        self.assertEqual(runner.run_until_stopped(), "stopped")
        self.provider_factory.assert_not_called()
        self.controller.post_screencap.assert_not_called()

    def test_transient_missing_pose_resamples_and_releases_forward(self):
        self.provider.locate.side_effect = [
            self.pose(),
            self.pose(point=None, found=False),
            self.pose(point=(0, -10)),
        ]
        navigator = self.navigator()
        self.assertTrue(navigator.move_to((0, -10)))
        self.assertEqual(navigator.reason, "arrived")
        self.assertEqual(self.provider.locate.call_count, 3)
        self.controller.post_key_down.assert_called_once_with(87)
        self.controller.post_key_up.assert_called_once_with(87)
        self.assertFalse(navigator.w_down)

    def test_persistent_missing_samples_have_finite_reasoned_budget(self):
        for reason in ("frame_missing", "position_missing", "angle_missing"):
            with self.subTest(reason=reason):
                navigator = self.navigator()
                if reason == "frame_missing":

                    def missing_frame():
                        navigator.reason = "frame_missing"
                        return None

                    navigator.update = Mock(side_effect=missing_frame)
                else:
                    self.provider.locate.return_value = self.pose(
                        found=reason != "position_missing", heading=None
                    )
                before = self.clock.now
                self.assertFalse(navigator.move_to((0, -10)))
                self.assertEqual(navigator.reason, reason)
                self.assertLess(self.clock.now - before, 0.2)
                self.assertFalse(navigator.w_down)
        self.controller.post_key_down.assert_not_called()

    def test_alternating_missing_sources_share_one_sampling_budget(self):
        navigator = self.navigator()
        results = iter(
            [
                None,
                (self.pose(found=False), self.pose()),
                (self.pose(), self.pose(heading=None)),
            ]
            * 10
        )

        def missing():
            navigator.reason = "frame_missing"
            result = next(results)
            if result is not None:
                location, angle = result
                angle = types.SimpleNamespace(found=False, angle=None)
                return location, angle
            return None

        navigator.update = Mock(side_effect=missing)
        before = self.clock.now
        self.assertFalse(navigator.move_to((0, -10)))
        self.assertLess(self.clock.now - before, 0.2)
        self.assertIn(
            navigator.reason, {"frame_missing", "position_missing", "angle_missing"}
        )

    def test_total_target_timeout_cannot_restart_forever(self):
        self.provider.locate.return_value = self.pose()
        navigator = self.navigator()
        self.assertFalse(navigator.move_to((0, -10)))
        self.assertEqual(navigator.reason, "timeout")
        self.assertLess(self.clock.now, 101.0)
        self.assertFalse(navigator.w_down)
        self.controller.post_key_up.assert_called()

    def test_failed_controller_jobs_release_and_terminate(self):
        for operation in ("post_key_down", "post_relative_move", "post_screencap"):
            with self.subTest(operation=operation):
                self.controller.reset_mock()
                self.controller.post_key_down.return_value = Job()
                self.controller.post_relative_move.return_value = Job()
                self.provider.locate.return_value = self.pose()
                self.provider.uses_visual_positioning.return_value = (
                    operation == "post_screencap"
                )
                failed_job = Job(succeeded=False, result=object())
                getattr(self.controller, operation).return_value = failed_job
                navigator = self.navigator()
                self.assertFalse(navigator.move_to((10, 0)))
                self.assertEqual(navigator.reason, "controller_failed")
                getattr(self.controller, operation).assert_called_once()
                self.assertFalse(navigator.w_down)
                if operation == "post_screencap":
                    self.assertEqual(failed_job.get_calls, 0)
                else:
                    self.controller.post_key_up.assert_called_once_with(87)

    def test_successful_empty_screencap_retries_without_moving(self):
        self.provider.uses_visual_positioning.return_value = True
        self.controller.post_screencap.return_value = Job(result=None)
        navigator = self.navigator(frame_interval=0.05)
        self.assertFalse(navigator.move_to((10, 0)))
        self.assertEqual(navigator.reason, "frame_missing")
        self.assertGreater(self.controller.post_screencap.call_count, 1)
        self.controller.post_key_down.assert_not_called()

    def test_exception_after_pressing_forward_releases_key(self):
        self.provider.locate.return_value = self.pose()
        self.controller.post_relative_move.side_effect = RuntimeError(
            "controller disconnected"
        )
        navigator = self.navigator()
        self.assertFalse(navigator.move_to((10, 0)))
        self.assertEqual(navigator.reason, "controller_failed")
        self.controller.post_key_up.assert_called_once_with(87)
        self.assertFalse(navigator.w_down)

    def test_online_runner_releases_failed_key_up_before_idle(self):
        route, runner = self.runner([(0, -10)])
        navigator = self.navigator()
        runner.navigator = navigator
        self.provider.locate.side_effect = [self.pose(), self.pose((0, -10))]
        self.controller.post_key_up.side_effect = [Job(succeeded=False), Job()]
        held_after_failure = []

        def stop_after_failed_route():
            if route.payload()["status"] == "failed":
                held_after_failure.append(navigator.w_down)
                self.context.tasker.stopping = True

        self.assertEqual(
            runner.run_until_stopped(on_tick=stop_after_failed_route), "stopped"
        )
        self.assertEqual(held_after_failure, [False])
        self.assertEqual(self.controller.post_key_up.call_count, 2)
        self.controller.post_key_down.assert_called_once_with(87)

    def test_online_runner_exits_when_release_keeps_failing(self):
        route, runner = self.runner([(0, 0)])
        navigator = self.navigator()
        runner.navigator = navigator
        self.provider.locate.return_value = self.pose()
        self.assertTrue(navigator.press_forward())
        self.controller.post_key_up.return_value = Job(succeeded=False)
        ticks = []

        def guard_unexpected_idle_loop():
            ticks.append(None)
            if len(ticks) > 1:
                self.context.tasker.stopping = True

        self.assertEqual(
            runner.run_until_stopped(on_tick=guard_unexpected_idle_loop), "failed"
        )
        self.assertFalse(self.context.tasker.stopping)
        self.assertEqual(len(ticks), 1)
        self.assertEqual(route.payload()["status"], "failed")
        self.assertEqual(route.payload()["reason"], "controller_failed")
        self.assertFalse(route.payload()["active"])
        self.assertEqual(self.controller.post_key_up.call_count, 3)
        self.controller.post_key_down.assert_called_once_with(87)

    def test_release_failure_cannot_fail_replacement_route(self):
        route, runner = self.runner([(10, 10)])
        runner.navigator.release.return_value = False

        def replace_route(_):
            if runner.navigator.move_to.call_count == 1:
                route.reset([(20, 20)], True, None)
            return True

        def guard_unexpected_arrival():
            if route.payload()["status"] == "arrived":
                self.context.tasker.stopping = True

        runner.navigator.move_to.side_effect = replace_route
        self.assertEqual(
            runner.run_until_stopped(on_tick=guard_unexpected_arrival), "failed"
        )
        self.assertEqual(route.payload()["status"], "running")
        self.assertTrue(route.payload()["active"])
        self.assertEqual(route.payload()["currentIndex"], 0)
        runner.navigator.move_to.assert_called_once()

    def test_failed_key_up_cannot_report_arrival(self):
        self.provider.locate.return_value = self.pose()
        navigator = self.navigator()
        self.assertTrue(navigator.press_forward())
        self.controller.post_key_up.return_value = Job(succeeded=False)
        self.assertFalse(navigator.move_to((0, 0)))
        self.assertEqual(navigator.reason, "controller_failed")
        self.assertTrue(navigator.w_down)

    def test_cancelled_frame_never_drives_input(self):
        for boundary in ("screencap", "location", "prediction", "on_frame"):
            with self.subTest(boundary=boundary):
                cancelled = [False]
                self.provider.uses_visual_positioning.return_value = True
                self.provider.locate.side_effect = None
                self.provider.locate.return_value = self.pose()
                self.predictor.predict.side_effect = None
                self.predictor.predict.return_value = types.SimpleNamespace(
                    found=True, angle=0.0
                )
                self.controller.post_screencap.return_value = Job(result=object())

                def cancel():
                    cancelled[0] = True

                callback = None
                if boundary == "screencap":
                    self.controller.post_screencap.return_value.on_wait = cancel
                elif boundary == "location":
                    self.provider.locate.side_effect = (
                        lambda *args: cancel() or self.pose()
                    )
                elif boundary == "prediction":
                    self.predictor.predict.side_effect = (
                        lambda *args: cancel()
                        or types.SimpleNamespace(found=True, angle=0.0)
                    )
                else:
                    callback = lambda *args: cancel()
                navigator = self.navigator(
                    should_cancel=lambda: cancelled[0], on_frame=callback
                )
                self.assertFalse(navigator.move_to((10, 0)))
                self.assertEqual(navigator.reason, "cancelled")
                self.controller.post_key_down.assert_not_called()
                self.controller.post_relative_move.assert_not_called()

    def test_cancel_during_key_down_cannot_post_mouse_move(self):
        self.provider.locate.return_value = self.pose()
        self.controller.post_key_down.return_value.on_wait = lambda: setattr(
            self.context.tasker, "stopping", True
        )
        navigator = self.navigator()
        self.assertFalse(navigator.move_to((10, 0)))
        self.assertEqual(navigator.reason, "cancelled")
        self.controller.post_relative_move.assert_not_called()
        self.controller.post_key_up.assert_called_once_with(87)

    def test_both_sleep_helpers_watch_tasker_and_route_cancellation(self):
        for kind in ("tasker", "route"):
            for helper in ("sleep_remaining", "sleep_interruptible"):
                with self.subTest(kind=kind, helper=helper):
                    self.context.tasker.stopping = False
                    cancelled = [False]
                    navigator = self.navigator(
                        should_cancel=lambda: cancelled[0], debug=True
                    )
                    navigator.press_forward()

                    def cancel():
                        if kind == "tasker":
                            self.context.tasker.stopping = True
                        else:
                            cancelled[0] = True

                    self.clock.on_sleep = cancel
                    argument = (
                        self.clock.perf_counter()
                        if helper == "sleep_remaining"
                        else 1.0
                    )
                    self.assertFalse(getattr(navigator, helper)(argument))
                    self.assertFalse(navigator.w_down)
                    self.clock.on_sleep = None

    def test_invalid_navigation_numbers_reject_before_hardware(self):
        for constructor in (
            self.navigation.WaypointNavigator,
            self.runners.RouteRunner,
        ):
            for key in (
                "max_duration",
                "sample_timeout",
                "frame_interval",
                "tolerance",
            ):
                for value in (float("nan"), float("inf"), -1.0):
                    with self.subTest(
                        constructor=constructor.__name__, key=key, value=value
                    ):
                        args = (
                            (self.context,)
                            if constructor is self.navigation.WaypointNavigator
                            else (self.context, self.model.RouteSession())
                        )
                        with self.assertRaises(ValueError):
                            constructor(*args, **{key: value})
        self.provider_factory.assert_not_called()

    def test_local_route_stopping_prevents_sampling_or_success(self):
        local = self.local.LocalRouteNavigation.__new__(self.local.LocalRouteNavigation)
        local.context = self.context
        local._closed = False
        local._route_json = [{"pixelX": 10, "pixelY": 10}]
        local._route_json_path = None
        local.route = self.model.RouteSession()
        local.runner = Mock()
        local.runner.source_size.return_value = SIZE
        local.runner.update_current_frame.return_value = None
        self.context.tasker.stopping = True
        self.assertFalse(local.run_route())
        local.runner.start.assert_not_called()
        self.context.tasker.stopping = False
        local.runner.start.side_effect = lambda: setattr(
            self.context.tasker, "stopping", True
        )
        self.assertFalse(local.run_route())
        local.runner.update_current_frame.assert_not_called()
        self.context.tasker.stopping = False
        local.runner.start.side_effect = None
        local.runner.run_until_stopped.side_effect = (
            lambda **kwargs: setattr(self.context.tasker, "stopping", True) or "arrived"
        )
        self.assertFalse(local.run_route())

    def test_route_versions_validate_each_selected_level(self):
        point = {"pixelX": 10, "pixelY": 20}
        valid_route = {
            "name": "selected",
            "version": 1,
            "segments": [{"version": 1, "waypoints": [point]}],
        }
        for version in (True, False, "1", 1.0, 2, 0, None):
            invalid_documents = [
                {"version": version, "waypoints": [point]},
                {"version": version, "routes": [valid_route]},
                {"version": version, "segments": [{"waypoints": [point]}]},
                {"routes": [{**valid_route, "version": version}]},
                {
                    "routes": [
                        {
                            **valid_route,
                            "segments": [{"version": version, "waypoints": [point]}],
                        }
                    ]
                },
                {"route": {"version": version, "waypoints": [point]}},
            ]
            for document in invalid_documents:
                with self.subTest(version=version, document=document):
                    with self.assertRaisesRegex(ValueError, "version"):
                        self.model.parse_route_segment_from_json_data(
                            document, "", 1, SIZE, SIZE
                        )
        document = {
            "version": 1,
            "routes": [valid_route, {"name": "unused", "version": 999}],
        }
        self.assertEqual(
            self.model.parse_route_segment_from_json_data(
                document, "selected", 1, SIZE, SIZE
            ),
            [(10, 20)],
        )
        with self.assertRaisesRegex(ValueError, "version"):
            self.model.parse_route_segment_from_json_data(
                document, "unused", 1, SIZE, SIZE
            )

    def test_legacy_formats_coordinates_and_segment_numbering_still_work(self):
        point = {"pixelX": 10, "pixelY": 20}
        documents = [
            [point],
            {"waypoints": [point]},
            {"points": [point]},
            {"path": [point]},
            {"route": {"waypoints": [point]}},
            {"version": 1, "waypoints": [point]},
        ]
        for document in documents:
            self.assertEqual(self.local.parse_route_waypoints(document), [(10, 20)])
        segments = {
            "version": 1,
            "segments": [{"points": [point]}, {"path": [{"pixelX": 30, "pixelY": 40}]}],
        }
        for index, expected in ((0, [(10, 20)]), (1, [(10, 20)]), (2, [(30, 40)])):
            self.assertEqual(
                self.local.parse_route_waypoints(segments, segment_index=index),
                expected,
            )
        self.assertEqual(
            self.model.parse_waypoint({"x": 1, "y": 2, "z": 3}, SIZE, SIZE), (9, 10)
        )
        self.raw_to_map.assert_called_once_with(1.0, 2.0, 3.0)
        self.assertEqual(
            self.model.parse_waypoint({"lat": 0, "lng": 0}, SIZE, SIZE), (5632, 5632)
        )
        self.assertEqual(
            self.model.parse_waypoint({"target_x": 10, "target_y": 20}, SIZE, SIZE),
            (10, 20),
        )

    def test_repository_version_one_routes_parse(self):
        documents = list((ROOT / "assets/resource/routes").glob("*.json"))
        self.assertTrue(documents)
        for path in documents:
            with self.subTest(route=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["version"], 1)
                self.assertTrue(self.local.parse_route_waypoints(data))


if __name__ == "__main__":
    unittest.main()
