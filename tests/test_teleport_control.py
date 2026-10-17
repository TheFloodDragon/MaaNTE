"""Isolated regression tests for map teleport task control (unittest only)."""

from contextlib import ExitStack
import importlib.util
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
ACTION = AGENT / "custom" / "action"
PREFIX = "_teleport_control_test_action"


def package(name, path=None):
    module = ModuleType(name)
    module.__path__ = [] if path is None else [str(path)]
    return module


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def job(success=True):
    result = SimpleNamespace(succeeded=success)
    return SimpleNamespace(wait=Mock(return_value=result))


class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


class TeleportControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scope = ExitStack()
        logger = ModuleType("utils.logger")
        logger.logger = logging.getLogger("teleport_control_tests")
        logger.logger.addHandler(logging.NullHandler())
        logger.get_logger = lambda name: logger.logger
        cls.focus = ModuleType("utils.maafocus")
        cls.focus.Print = Mock()
        agent_server = ModuleType("maa.agent.agent_server")
        agent_server.AgentServer = SimpleNamespace(
            custom_action=lambda name: lambda action: action
        )
        custom_action = ModuleType("maa.custom_action")
        custom_action.CustomAction = type(
            "CustomAction",
            (),
            {"RunArg": SimpleNamespace, "RunResult": SimpleNamespace},
        )
        context = ModuleType("maa.context")
        context.Context = SimpleNamespace
        pipeline = ModuleType("maa.pipeline")
        pipeline.JOCR = lambda **params: SimpleNamespace(**params)
        pipeline.JRecognitionType = SimpleNamespace(OCR="OCR")
        modules = {
            "utils": package("utils", AGENT / "utils"),
            "utils.logger": logger,
            "utils.maafocus": cls.focus,
            "maa": package("maa"),
            "maa.agent": package("maa.agent"),
            "maa.agent.agent_server": agent_server,
            "maa.context": context,
            "maa.custom_action": custom_action,
            "maa.pipeline": pipeline,
            PREFIX: package(PREFIX, ACTION),
            PREFIX + ".Common": package(PREFIX + ".Common", ACTION / "Common"),
            PREFIX
            + ".MapTeleport": package(PREFIX + ".MapTeleport", ACTION / "MapTeleport"),
        }
        cls.scope.enter_context(patch.dict(sys.modules, modules))
        cls.control = load_module("utils.task_control", AGENT / "utils/task_control.py")
        cls.module = load_module(
            PREFIX + ".MapTeleport.teleport_to_point",
            ACTION / "MapTeleport/teleport_to_point.py",
        )

    @classmethod
    def tearDownClass(cls):
        cls.scope.close()

    def setUp(self):
        self.module = type(self).module
        self.clock = Clock()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(self.control.time, "monotonic", self.clock.monotonic)
        )
        self.stack.enter_context(
            patch.object(self.control.time, "sleep", self.clock.sleep)
        )
        self.controller = Mock()
        self.frame = np.zeros((24, 24, 3), dtype=np.uint8)
        self.controller.cached_image = self.frame
        for name in (
            "post_screencap",
            "post_touch_move",
            "post_touch_down",
            "post_touch_up",
            "post_swipe",
        ):
            getattr(self.controller, name).return_value = job()
        self.context = SimpleNamespace(
            tasker=SimpleNamespace(stopping=False, controller=self.controller),
            run_action=Mock(return_value=SimpleNamespace(success=True)),
            run_task=Mock(
                return_value=SimpleNamespace(status=SimpleNamespace(succeeded=True))
            ),
            run_recognition=Mock(return_value=SimpleNamespace(hit=True)),
            run_recognition_direct=Mock(return_value=SimpleNamespace(hit=True)),
        )
        self.focus.Print.reset_mock()

    def mock_flow(self):
        point = self.module.TeleportPoint(
            "point",
            "测试传送点",
            (0.0, 0.0),
            "world",
            "测试地区",
            1,
            "测试选项",
            "test.png",
            "",
        )
        values = {
            "load_teleport_point": point,
            "_load_template": self.frame,
            "_ensure_in_world": True,
            "_press_key_action": True,
            "_drag_zoom_control": True,
            "_click_map_index": True,
            "_wait_ocr_text": object(),
            "_switch_to_area": True,
            "_click_main_selection": True,
            "_click_teleport_icon": True,
            "_wait_screen_still": self.frame,
            "_click_rect_action": True,
            "_click_teleport_button": True,
            "_wait_teleport_loading": True,
        }
        return {
            name: self.stack.enter_context(
                patch.object(self.module, name, return_value=value)
            )
            for name, value in values.items()
        }

    def test_module_can_be_loaded_without_maa(self):
        name = PREFIX + ".MapTeleport.without_maa"
        with patch.dict(sys.modules, {"maa.agent.agent_server": None}):
            module = load_module(name, ACTION / "MapTeleport/teleport_to_point.py")
        self.assertIsNone(module.AgentServer)
        self.assertIsNone(module.CustomAction)

    def test_action_detail_failure_is_not_success(self):
        for detail in (None, SimpleNamespace(success=False)):
            self.context.run_action.return_value = detail
            self.assertFalse(self.module._press_key_action(self.context, 77))
            self.assertFalse(self.module._click_rect_action(self.context, [1, 2, 3, 4]))
        self.context.run_action.return_value = SimpleNamespace(success=True)
        self.assertTrue(self.module._press_key_action(self.context, 77))
        self.assertTrue(self.module._click_rect_action(self.context, [1, 2, 3, 4]))

    def test_cancelled_context_posts_no_new_input(self):
        self.context.tasker.stopping = True
        self.assertFalse(self.module._press_key_action(self.context, 77))
        self.assertFalse(self.module._click_rect_action(self.context, [1, 2, 3, 4]))
        self.assertFalse(self.module._click_point(self.context, 1, 2))
        self.assertFalse(
            self.module._swipe_up_in_roi(self.context, [1, 2, 3, 4], duration=200)
        )
        self.assertFalse(
            self.module._click_teleport_icon(
                self.context, self.frame, "a", 1, template_threshold=0.8
            )
        )
        self.context.run_action.assert_not_called()
        self.assertEqual(self.controller.mock_calls, [])

    def test_cancellation_during_action_does_not_report_success(self):
        def cancel(*args, **kwargs):
            self.context.tasker.stopping = True
            return SimpleNamespace(success=True)

        self.context.run_action.side_effect = cancel
        self.assertFalse(self.module._press_key_action(self.context, 77))

    def test_failed_enter_world_task_stops_before_recognition(self):
        for detail in (None, SimpleNamespace(status=SimpleNamespace(succeeded=False))):
            self.context.run_task.return_value = detail
            self.assertFalse(self.module._ensure_in_world(self.context))
        self.context.run_recognition.assert_not_called()
        self.controller.post_screencap.assert_not_called()

    def test_failed_loading_task_stops_before_recognition(self):
        self.context.run_task.return_value.status.succeeded = False
        self.assertFalse(self.module._wait_teleport_loading(self.context))
        self.context.run_recognition.assert_not_called()
        self.controller.post_screencap.assert_not_called()

    def test_loading_success_requires_fresh_in_world_confirmation(self):
        self.context.run_recognition.return_value.hit = False
        self.assertFalse(self.module._wait_teleport_loading(self.context))
        self.controller.post_screencap.assert_called_once()
        name, frame = self.context.run_recognition.call_args.args
        self.assertEqual(name, "InWorld")
        self.assertIs(frame, self.frame)
        self.context.run_recognition.return_value.hit = True
        self.assertTrue(self.module._wait_teleport_loading(self.context))

    def test_cancel_during_loading_suppresses_screenshot_and_recognition(self):
        def cancel(*args, **kwargs):
            self.context.tasker.stopping = True
            return SimpleNamespace(status=SimpleNamespace(succeeded=True))

        self.context.run_task.side_effect = cancel
        self.assertFalse(self.module._wait_teleport_loading(self.context))
        self.controller.post_screencap.assert_not_called()
        self.context.run_recognition.assert_not_called()

    def test_failed_screencap_never_reads_cached_image(self):
        self.controller.post_screencap.return_value = job(False)
        with patch.object(
            type(self.controller),
            "cached_image",
            new_callable=PropertyMock,
            create=True,
        ) as cached:
            cached.return_value = self.frame
            self.assertIsNone(self.module._screencap(self.context))
            cached.assert_not_called()
        self.assertFalse(self.module._wait_teleport_loading(self.context))
        self.context.run_recognition.assert_not_called()

    def test_screencap_exception_does_not_use_old_frame(self):
        self.controller.post_screencap.side_effect = RuntimeError("capture failed")
        self.assertIsNone(self.module._screencap(self.context))

    def test_screen_wait_cancelled_during_pause_does_not_capture_again(self):
        self.clock.on_sleep = lambda: setattr(self.context.tasker, "stopping", True)
        self.assertIsNone(
            self.module._wait_screen_still(self.context, interval=20, max_wait=100)
        )
        self.controller.post_screencap.assert_called_once()
        self.assertLessEqual(self.clock.now, 0.05)

    def test_screen_wait_accepts_only_consecutive_stable_fresh_frames(self):
        self.assertIs(self.module._wait_screen_still(self.context), self.frame)
        self.assertEqual(self.controller.post_screencap.call_count, 3)

    def test_unstable_screen_timeout_returns_none(self):
        white = np.full_like(self.frame, 255)
        frames = iter([self.frame, white] * 10)
        with patch.object(
            self.module, "_screencap", side_effect=lambda context: next(frames)
        ):
            self.assertIsNone(
                self.module._wait_screen_still(self.context, max_wait=0.6, interval=0.1)
            )
        self.assertLessEqual(self.clock.now, 0.6)

    def test_failed_frame_during_still_wait_ends_immediately(self):
        with patch.object(
            self.module, "_screencap", side_effect=[self.frame, None]
        ) as capture:
            self.assertIsNone(self.module._wait_screen_still(self.context))
        self.assertEqual(capture.call_count, 2)

    def test_click_releases_touch_on_cancellation(self):
        self.clock.on_sleep = lambda: setattr(self.context.tasker, "stopping", True)
        self.assertFalse(self.module._click_point(self.context, 10, 20, delay=20))
        self.controller.post_touch_down.assert_called_once_with(10, 20)
        self.controller.post_touch_up.assert_called_once()
        self.assertLessEqual(self.clock.now, 0.05)

    def test_click_releases_touch_when_down_wait_raises(self):
        self.controller.post_touch_down.return_value.wait.side_effect = RuntimeError(
            "down failed"
        )
        self.assertFalse(self.module._click_point(self.context, 10, 20))
        self.controller.post_touch_up.assert_called_once()

    def test_click_failed_move_prevents_touch_down(self):
        self.controller.post_touch_move.return_value = job(False)
        self.assertFalse(self.module._click_point(self.context, 10, 20))
        self.controller.post_touch_down.assert_not_called()

    def test_failed_release_fails_click(self):
        self.controller.post_touch_up.return_value = job(False)
        self.assertFalse(self.module._click_point(self.context, 10, 20))

    def test_swipe_checks_job_and_releases_on_failure_or_exception(self):
        self.controller.post_swipe.return_value = job(False)
        self.assertFalse(
            self.module._swipe_up_in_roi(self.context, [1, 2, 3, 4], duration=200)
        )
        self.controller.post_swipe.return_value.wait.side_effect = RuntimeError(
            "swipe failed"
        )
        self.assertFalse(
            self.module._swipe_up_in_roi(self.context, [1, 2, 3, 4], duration=200)
        )
        self.assertEqual(self.controller.post_touch_up.call_count, 2)

    def test_swipe_cancellation_still_releases(self):
        def cancel():
            self.context.tasker.stopping = True
            return SimpleNamespace(succeeded=True)

        self.controller.post_swipe.return_value.wait.side_effect = cancel
        self.assertFalse(
            self.module._swipe_up_in_roi(self.context, [1, 2, 3, 4], duration=200)
        )
        self.controller.post_touch_up.assert_called_once()

    def test_invalid_delays_and_limits_fail_before_ui(self):
        for options in (
            {"action_delay": -0.1},
            {"action_delay": float("nan")},
            {"action_delay": float("inf")},
            {"max_area_switches": 0},
            {"ocr_threshold": float("nan")},
            {"template_threshold": 1.1},
        ):
            with self.subTest(options=options):
                self.assertFalse(
                    self.module.run_map_teleport_flow(self.context, "point", **options)
                )
        self.context.run_task.assert_not_called()
        self.context.run_action.assert_not_called()
        self.assertEqual(self.controller.mock_calls, [])
        for delay in (-1, float("nan"), float("inf")):
            self.assertFalse(self.module._click_point(self.context, 1, 2, delay=delay))
            self.assertFalse(
                self.module._swipe_up_in_roi(self.context, [1, 2, 3, 4], duration=delay)
            )
        self.assertEqual(self.controller.mock_calls, [])

    def test_repeated_non_target_list_node_terminates(self):
        with patch.object(
            self.module,
            "_identify_new_list_nodes",
            return_value=(1200, 200, 10, 10, 0.9),
        ) as identify:
            with patch.object(self.module, "_check_closest_icon", return_value=False):
                self.assertFalse(
                    self.module._click_teleport_icon(
                        self.context, self.frame, "a", 1, template_threshold=0.8
                    )
                )
        self.assertEqual(identify.call_count, 2)
        self.controller.post_touch_down.assert_not_called()

    def test_failed_list_drag_prevents_second_drag(self):
        with patch.object(self.module, "_identify_new_list_nodes", return_value=None):
            with patch.object(
                self.module, "_drag_list_upward", return_value=False
            ) as drag:
                self.assertFalse(
                    self.module._click_teleport_icon(
                        self.context, self.frame, "a", 1, template_threshold=0.8
                    )
                )
        drag.assert_called_once()

    def test_cancel_during_icon_check_never_clicks(self):
        def cancel(*args, **kwargs):
            self.context.tasker.stopping = True
            return True

        with patch.object(
            self.module,
            "_identify_new_list_nodes",
            return_value=(1200, 200, 10, 10, 0.9),
        ):
            with patch.object(self.module, "_check_closest_icon", side_effect=cancel):
                self.assertFalse(
                    self.module._click_teleport_icon(
                        self.context, self.frame, "a", 1, template_threshold=0.8
                    )
                )
        self.controller.post_touch_down.assert_not_called()

    def test_missing_stable_list_frame_does_not_trigger_drag(self):
        select_icon = self.module._click_teleport_icon
        mocks = self.mock_flow()
        mocks["_click_teleport_icon"].side_effect = select_icon
        mocks["_wait_screen_still"].return_value = None
        with patch.object(self.module, "_drag_list_upward") as drag:
            self.assertFalse(self.module.run_map_teleport_flow(self.context, "point"))
        drag.assert_not_called()
        self.controller.post_touch_down.assert_not_called()
        mocks["_press_key_action"].assert_called_once_with(
            self.context, self.module.KEY_M
        )
        mocks["_click_rect_action"].assert_not_called()

    def test_cancel_at_map_open_delay_stops_before_zoom(self):
        mocks = self.mock_flow()
        self.clock.on_sleep = lambda: setattr(self.context.tasker, "stopping", True)
        self.assertFalse(self.module.run_map_teleport_flow(self.context, "point"))
        mocks["_press_key_action"].assert_called_once_with(
            self.context, self.module.KEY_M
        )
        mocks["_drag_zoom_control"].assert_not_called()
        self.assertFalse(
            any("已完成" in call.args[1] for call in self.focus.Print.call_args_list)
        )

    def test_unstable_final_screen_prevents_escape_and_confirm(self):
        mocks = self.mock_flow()
        mocks["_wait_screen_still"].return_value = None
        self.assertFalse(self.module.run_map_teleport_flow(self.context, "point"))
        mocks["_press_key_action"].assert_called_once_with(
            self.context, self.module.KEY_M
        )
        mocks["_click_rect_action"].assert_not_called()
        mocks["_wait_teleport_loading"].assert_not_called()

    def test_loading_failure_never_announces_completion(self):
        mocks = self.mock_flow()
        mocks["_wait_teleport_loading"].return_value = False
        self.assertFalse(self.module.run_map_teleport_flow(self.context, "point"))
        self.assertFalse(
            any("已完成" in call.args[1] for call in self.focus.Print.call_args_list)
        )

    def test_cancel_at_final_confirm_delay_prevents_click(self):
        mocks = self.mock_flow()

        def cancel_after_escape():
            if self.clock.now >= 1.6:
                self.context.tasker.stopping = True

        self.clock.on_sleep = cancel_after_escape
        self.assertFalse(self.module.run_map_teleport_flow(self.context, "point"))
        self.assertEqual(mocks["_press_key_action"].call_count, 2)
        mocks["_click_rect_action"].assert_not_called()
        mocks["_wait_teleport_loading"].assert_not_called()
        self.assertFalse(
            any("已完成" in call.args[1] for call in self.focus.Print.call_args_list)
        )

    def test_cancel_after_loading_never_announces_completion(self):
        mocks = self.mock_flow()

        def cancel(*args, **kwargs):
            self.context.tasker.stopping = True
            return True

        mocks["_wait_teleport_loading"].side_effect = cancel
        self.assertFalse(self.module.run_map_teleport_flow(self.context, "point"))
        self.assertFalse(
            any("已完成" in call.args[1] for call in self.focus.Print.call_args_list)
        )

    def test_ocr_poll_cancellation_does_not_run_ocr(self):
        with patch.object(self.module, "_wait_screen_still") as still:

            def cancel(*args, **kwargs):
                self.context.tasker.stopping = True
                return self.frame

            still.side_effect = cancel
            self.assertIsNone(
                self.module._wait_ocr_text(
                    self.context, "地图索引", [1, 2, 3, 4], threshold=0.5
                )
            )
        self.context.run_recognition_direct.assert_not_called()

    def test_successful_flow_announces_completion_through_focus(self):
        self.mock_flow()
        self.assertTrue(self.module.run_map_teleport_flow(self.context, "point"))
        self.assertIn("地图传送流程已完成", self.focus.Print.call_args.args[1])

    def test_action_invalid_parameters_return_failure_with_focus_message(self):
        result = self.module.MapTeleportToPointAction().run(
            self.context, SimpleNamespace(custom_action_param="bad json")
        )
        self.assertFalse(result.success)
        self.focus.Print.assert_called_once()


if __name__ == "__main__":
    unittest.main()
