"""统一进入游戏的离线回归：虚拟时钟、独立导入及假截图，不操作窗口或 CDP。"""

import ast
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "EnterGameAccountLogin"
READY = "CloudWebWorldReady"
ENTER = "CloudWebWorldClickEnter"
LOGIN_ENTER = "CloudWebWorldLoginEnter"
LOADING = "CloudWebWorldLoading"
ANNOUNCEMENT = "CloudWebWorldAnnouncement"
MONTHLY = "CloudWebWorldMonthlyPass"
BLANK = "CloudWebWorldClickBlank"
CLOSE = "CloudWebWorldCloseButton"
LANGUAGES = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")


def read_json(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    return json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE))


def utility_stubs():
    """仅在 exec_module 期间替换依赖，兼容旧云测试替换过的 utils 包。"""
    utils = ModuleType("utils")
    utils.__path__ = []
    logger = ModuleType("utils.logger")
    logger.logger = Mock()
    focus = ModuleType("utils.maafocus")
    focus.PrintT = Mock()
    env = ModuleType("utils.pienv")
    env.controller_name = Mock(return_value="Win32")
    return {
        "utils": utils,
        "utils.logger": logger,
        "utils.maafocus": focus,
        "utils.pienv": env,
    }


def execute_isolated(name, relative, dependencies):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    # dataclasses 根据 __module__ 查询 sys.modules，完成加载后立即恢复原映射。
    with patch.dict(sys.modules, {**dependencies, name: module}):
        spec.loader.exec_module(module)
    return module


def load_common():
    return execute_isolated(
        "_test_enter_game_common", "agent/game_entry.py", utility_stubs()
    )


def load_wrapper(common):
    dependencies = utility_stubs()
    maa = ModuleType("maa")
    maa.__path__ = []
    agent = ModuleType("maa.agent")
    agent.__path__ = []
    server_module = ModuleType("maa.agent.agent_server")
    server_module.AgentServer = SimpleNamespace(custom_action=None)
    action_module = ModuleType("maa.custom_action")

    class FakeCustomAction:
        RunResult = SimpleNamespace

    action_module.CustomAction = FakeCustomAction
    dependencies.update(
        {
            "maa": maa,
            "maa.agent": agent,
            "maa.agent.agent_server": server_module,
            "maa.custom_action": action_module,
            "game_entry": common,
        }
    )
    registrations = {}

    def register(name):
        def decorate(cls):
            registrations[name] = cls
            return cls

        return decorate

    with patch.object(server_module.AgentServer, "custom_action", side_effect=register):
        module = execute_isolated(
            "_test_enter_game_wrapper",
            "agent/custom/action/enter_game.py",
            dependencies,
        )
    return module, registrations


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now = round(self.now + seconds, 9)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(max(seconds, 0.000000001))
        if self.on_sleep is not None:
            self.on_sleep()


class Frame:
    """只模拟识别器所需的数组形状，每次 capture 创建独立帧。"""

    ndim = 3

    def __init__(
        self,
        states=(),
        *,
        shape=(720, 1280, 3),
        box=(600, 600, 80, 40),
        count=1,
        delay=0,
    ):
        self.states = {states} if isinstance(states, str) else set(states)
        self.shape = shape
        self.box = box
        self.count = count
        self.delay = delay


class FrameStream:
    def __init__(self, clock, states):
        self.clock = clock
        self.states = list(states)
        self.frames = []
        self.on_capture = None

    def __call__(self):
        item = self.states[min(len(self.frames), len(self.states) - 1)]
        if isinstance(item, Frame):
            frame = Frame(
                item.states,
                shape=item.shape,
                box=item.box,
                count=item.count,
                delay=item.delay,
            )
        elif item is None:
            frame = None
        else:
            frame = Frame(item)
        self.frames.append(frame)
        if frame is not None:
            self.clock.advance(frame.delay)
        if self.on_capture is not None:
            self.on_capture(len(self.frames), frame)
        return frame


class FakeContext:
    def __init__(self, clock, nodes=None):
        self.clock = clock
        self.nodes = {} if nodes is None else nodes
        self.tasker = SimpleNamespace(stopping=False, controller=Mock())
        self.tasker.post_task = Mock(
            side_effect=AssertionError("不能调用真实 post_task")
        )
        self.run_task = Mock(side_effect=AssertionError("不能启动子任务"))
        self.override_next = Mock(return_value=True)
        self.recognitions = []
        self.on_recognition = None

    def get_node_data(self, node):
        return self.nodes.get(node)

    def run_recognition(self, node, frame):
        self.recognitions.append((node, frame))
        if self.on_recognition is not None:
            override = self.on_recognition(node, frame)
            if override is not NotImplemented:
                return override
        return SimpleNamespace(
            hit=node in frame.states,
            box=frame.box,
            filtered_results=[SimpleNamespace(box=frame.box)] * frame.count,
        )


class CommonCase(unittest.TestCase):
    def setUp(self):
        self.common = load_common()
        self.clock = Clock()
        self.common.time = self.clock
        self.common.PrintT = Mock()
        nodes = read_json("assets/resource/base/pipeline/CloudGame/CloudGameWeb.json")
        nodes.update(read_json("assets/resource/base/pipeline/EnterGame.json"))
        self.context = FakeContext(self.clock, nodes)
        self.click = Mock(return_value=True)
        self.key = Mock(return_value=True)
        self.capture = None

    def progress(self, login=60, world=10, transition=3):
        return self.common.WorldProgress(login, world, transition)

    def run_entry(self, states, progress=None, before_step=None):
        self.capture = FrameStream(self.clock, states)
        return self.common.run_world_entry(
            self.context,
            self.capture,
            self.click,
            self.key,
            progress=progress,
            before_step=before_step,
        )

    def assert_error(self, key, callback, reason=None):
        with self.assertRaises(self.common.EntryError) as caught:
            callback()
        self.assertEqual(caught.exception.key, key)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def printed_keys(self):
        return [call.args[1] for call in self.common.PrintT.call_args_list]

    def assert_no_input(self):
        self.click.assert_not_called()
        self.key.assert_not_called()
        self.context.tasker.post_task.assert_not_called()
        self.context.run_task.assert_not_called()


class TimeoutConfigTests(CommonCase):
    def test_public_default_is_ten_minutes(self):
        self.assertEqual(self.common.LOGIN_TIMEOUT_NODE, "EnterGameLoginTimeoutConfig")
        self.assertEqual(self.common.login_timeout_seconds(self.context), 600)

    def test_accepts_only_integral_one_to_120_minutes(self):
        for value in (1, 10, 120, 1.0, 120.0, "1", "10", "120"):
            with self.subTest(value=value):
                self.context.nodes = {
                    self.common.LOGIN_TIMEOUT_NODE: {"attach": {"minutes": value}}
                }
                self.assertEqual(
                    self.common.login_timeout_seconds(self.context), float(value) * 60
                )

    def test_invalid_values_do_not_fall_back_to_legacy(self):
        values = (
            0,
            -1,
            121,
            1.5,
            True,
            False,
            None,
            "",
            "0",
            "121",
            "1.0",
            " 10",
            "+10",
            "１",
            "١",
            float("nan"),
            float("inf"),
            [],
            {},
        )
        for value in values:
            with self.subTest(value=value):
                self.context.nodes = {
                    self.common.LOGIN_TIMEOUT_NODE: {"attach": {"minutes": value}},
                    "CloudWebLoginTimeoutConfig": {"attach": {"minutes": 10}},
                }
                self.assert_error(
                    "enter_game.invalid_config",
                    lambda: self.common.login_timeout_seconds(self.context),
                    "invalid_login_timeout",
                )

    def test_malformed_present_config_is_not_missing(self):
        for node in ({}, {"attach": {}}, {"attach": None}, [], "10"):
            with self.subTest(node=node):
                self.context.nodes = {self.common.LOGIN_TIMEOUT_NODE: node}
                self.assert_error(
                    "enter_game.invalid_config",
                    lambda: self.common.login_timeout_seconds(self.context),
                )

    def test_missing_public_config_uses_validated_legacy(self):
        self.context.nodes = {
            "CloudWebLoginTimeoutConfig": {"attach": {"minutes": "2"}}
        }
        self.assertEqual(self.common.login_timeout_seconds(self.context), 120)
        self.context.nodes["CloudWebLoginTimeoutConfig"]["attach"]["minutes"] = 121
        self.assert_error(
            "enter_game.invalid_config",
            lambda: self.common.login_timeout_seconds(self.context),
        )

    def test_missing_both_configs_uses_default_or_caller_fallback(self):
        self.context.nodes = {}
        self.assertEqual(self.common.login_timeout_seconds(self.context), 600)
        self.assertEqual(
            self.common.login_timeout_seconds(self.context, fallback_seconds=37), 37
        )

    def test_public_config_takes_precedence_over_legacy(self):
        self.context.nodes["CloudWebLoginTimeoutConfig"] = {"attach": {"minutes": 120}}
        self.assertEqual(self.common.login_timeout_seconds(self.context), 600)


class WorldFlowTests(CommonCase):
    def test_account_enter_without_exit_loading_announcement_monthly_and_world(self):
        progress = self.progress()
        self.assertTrue(
            self.run_entry(
                [
                    ACCOUNT,
                    ACCOUNT,
                    ENTER,
                    ENTER,
                    LOADING,
                    ANNOUNCEMENT,
                    ANNOUNCEMENT,
                    MONTHLY,
                    MONTHLY,
                    READY,
                    READY,
                ],
                progress,
            )
        )
        self.assertEqual(self.click.call_count, 2)
        self.key.assert_called_once_with("Escape")
        self.assertEqual(self.printed_keys().count("enter_game.login_required"), 1)
        for key in (
            "enter_game.login_enter",
            "enter_game.announcement",
            "enter_game.monthly_pass",
            "enter_game.world_ready",
        ):
            self.assertIn(key, self.printed_keys())
        self.assertIsNone(progress.login_started)
        # 无退出图标仍命中最后的完整进入文字识别节点，而非 LoginEnter。
        self.assertTrue(
            any(
                node == ENTER and ENTER in frame.states
                for node, frame in self.context.recognitions
            )
        )

    def test_login_enter_with_exit_icon_is_supported(self):
        self.assertTrue(
            self.run_entry([LOGIN_ENTER, LOGIN_ENTER, READY, READY], self.progress())
        )
        self.click.assert_called_once_with(640.0, 620.0)
        self.key.assert_not_called()

    def test_already_in_world_requires_two_new_frames_without_input(self):
        self.assertTrue(self.run_entry([READY, READY], self.progress()))
        self.assertEqual(len(self.capture.frames), 2)
        self.assertIsNot(self.capture.frames[0], self.capture.frames[1])
        self.assert_no_input()

    def test_unknown_between_ready_frames_resets_stability(self):
        self.assertTrue(self.run_entry([READY, (), READY, READY], self.progress()))
        self.assertEqual(len(self.capture.frames), 4)
        self.assert_no_input()

    def test_missing_video_between_ready_frames_resets_stability(self):
        self.assertTrue(self.run_entry([READY, None, READY, READY], self.progress()))
        self.assertEqual(len(self.capture.frames), 4)
        self.assert_no_input()

    def test_close_button_is_clicked_once_then_waits_for_positive_change(self):
        self.assertTrue(
            self.run_entry(
                [CLOSE, CLOSE, CLOSE, CLOSE, LOADING, READY, READY], self.progress()
            )
        )
        self.click.assert_called_once_with(640.0, 620.0)
        self.key.assert_not_called()

    def test_unchanged_close_button_times_out_without_retry(self):
        progress = self.progress()
        self.assert_error(
            "enter_game.transition_failed",
            lambda: self.run_entry([CLOSE], progress),
            "unchanged_action_state",
        )
        self.click.assert_called_once()
        self.assertAlmostEqual(self.clock.now, 3)
        self.assertNotIn("enter_game.world_ready", self.printed_keys())

    def test_unknown_does_not_clear_pending_action_or_allow_duplicate_click(self):
        progress = self.progress()
        self.assert_error(
            "enter_game.transition_failed",
            lambda: self.run_entry([CLOSE, CLOSE, (), CLOSE], progress),
        )
        self.click.assert_called_once()
        self.assertEqual(progress.pending_node, CLOSE)

    def test_unknown_forever_preserves_pending_transition_deadline(self):
        progress = self.progress()
        self.assert_error(
            "enter_game.transition_failed",
            lambda: self.run_entry([CLOSE, CLOSE, ()], progress),
        )
        self.click.assert_called_once()
        self.assertAlmostEqual(self.clock.now, 3)

    def test_blank_popup_sends_escape_only_once(self):
        self.assertTrue(
            self.run_entry(
                [BLANK, BLANK, BLANK, LOADING, READY, READY], self.progress()
            )
        )
        self.key.assert_called_once_with("Escape")
        self.click.assert_not_called()

    def test_new_account_form_before_input_prevents_stale_click(self):
        self.assertTrue(
            self.run_entry(
                [ENTER, ACCOUNT, ACCOUNT, LOADING, READY, READY], self.progress()
            )
        )
        self.assertIn("enter_game.login_required", self.printed_keys())
        self.assert_no_input()

    def test_same_target_is_recognized_again_on_fresh_frame(self):
        self.assertTrue(self.run_entry([ENTER, LOADING, READY, READY], self.progress()))
        self.assert_no_input()
        self.assertTrue(
            any(
                node == LOADING and LOADING in frame.states
                for node, frame in self.context.recognitions
            )
        )

    def test_input_uses_fresh_target_position_not_original_box(self):
        self.assertTrue(
            self.run_entry(
                [
                    Frame(ENTER, box=(10, 10, 20, 20)),
                    Frame(ENTER, box=(600, 600, 80, 40)),
                    READY,
                    READY,
                ],
                self.progress(),
            )
        )
        self.click.assert_called_once_with(640.0, 620.0)

    def test_account_priority_blocks_ready_and_old_enter_target(self):
        progress = self.progress(login=1)
        self.assert_error(
            "enter_game.login_timeout",
            lambda: self.run_entry([(ACCOUNT, READY, ENTER)], progress),
        )
        self.assert_no_input()

    def test_loading_priority_prevents_no_icon_enter_fallback_click(self):
        self.assertTrue(
            self.run_entry([(LOADING, ENTER), READY, READY], self.progress())
        )
        self.assert_no_input()

    def test_full_enter_fallback_is_last_after_popups(self):
        self.assertEqual(self.common.WORLD_NODES[-1], ENTER)
        self.assertLess(
            self.common.WORLD_NODES.index(ANNOUNCEMENT),
            self.common.WORLD_NODES.index(ENTER),
        )
        self.assertTrue(
            self.run_entry(
                [(ANNOUNCEMENT, ENTER), (ANNOUNCEMENT, ENTER), READY, READY],
                self.progress(),
            )
        )
        self.click.assert_called_once()
        self.assertIn("enter_game.announcement", self.printed_keys())
        self.assertNotIn("enter_game.login_enter", self.printed_keys())

    def test_bgra_frames_are_accepted(self):
        self.assertTrue(
            self.run_entry([Frame(READY, shape=(720, 1280, 4))], self.progress())
        )
        self.assert_no_input()

    def test_announcement_in_front_of_world_is_processed_before_ready(self):
        self.assertTrue(
            self.run_entry(
                [(ANNOUNCEMENT, READY), (ANNOUNCEMENT, READY), READY, READY],
                self.progress(),
            )
        )
        self.click.assert_called_once()
        self.assertIn("enter_game.announcement", self.printed_keys())
        self.assertEqual(len(self.capture.frames), 4)

    def test_monthly_pass_in_front_of_world_is_processed_before_ready(self):
        self.assertTrue(
            self.run_entry(
                [(MONTHLY, READY), (MONTHLY, READY), READY, READY], self.progress()
            )
        )
        self.key.assert_called_once_with("Escape")
        self.assertIn("enter_game.monthly_pass", self.printed_keys())
        self.assertEqual(len(self.capture.frames), 4)

    def test_ordinary_modal_in_front_of_world_is_not_success(self):
        for modal in (BLANK, CLOSE):
            with self.subTest(modal=modal):
                self.click.reset_mock()
                self.key.reset_mock()
                self.assertTrue(
                    self.run_entry(
                        [(modal, READY), (modal, READY), READY, READY], self.progress()
                    )
                )
                self.assertEqual(self.click.call_count + self.key.call_count, 1)
                self.assertEqual(len(self.capture.frames), 4)

    def test_enter_alias_switch_does_not_allow_a_second_click(self):
        self.assert_error(
            "enter_game.transition_failed",
            lambda: self.run_entry([LOGIN_ENTER, LOGIN_ENTER, ENTER], self.progress()),
        )
        self.click.assert_called_once()

    def test_new_popup_before_input_prevents_clicking_old_enter_button(self):
        self.assertTrue(
            self.run_entry(
                [
                    ENTER,
                    (ANNOUNCEMENT, ENTER),
                    ANNOUNCEMENT,
                    ANNOUNCEMENT,
                    READY,
                    READY,
                ],
                self.progress(),
            )
        )
        self.click.assert_called_once()
        self.assertIn("enter_game.announcement", self.printed_keys())
        self.assertNotIn("enter_game.login_enter", self.printed_keys())


class DeadlineTests(CommonCase):
    def test_account_timeout_is_distinct_and_never_submits_credentials(self):
        progress = self.progress(login=60, world=2)
        error = self.assert_error(
            "enter_game.login_timeout",
            lambda: self.run_entry([ACCOUNT], progress),
            "login_deadline",
        )
        self.assertEqual(error.values, (1,))
        self.assertAlmostEqual(self.clock.now, 60)
        self.assert_no_input()

    def test_unknown_screen_reaches_world_timeout(self):
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry([()], self.progress(world=2)),
            "world_deadline",
        )
        self.assertAlmostEqual(self.clock.now, 2)
        self.assert_no_input()

    def test_no_video_reaches_world_timeout(self):
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry([None], self.progress(world=2)),
        )
        self.assert_no_input()

    def test_loading_timeout_has_no_click_or_escape(self):
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry([LOADING], self.progress(world=2)),
        )
        self.assert_no_input()

    def test_account_wait_is_excluded_from_world_budget(self):
        progress = self.progress(login=60, world=2)
        self.assertTrue(
            self.run_entry([ACCOUNT] * 8 + [LOADING, READY, READY], progress)
        )
        self.assertGreater(self.clock.now, 2)
        self.assertAlmostEqual(progress.world_deadline, 5.2)
        self.assertIsNone(progress.login_started)
        self.assert_no_input()

    def test_repeated_account_state_does_not_reset_deadline_or_prompt(self):
        progress = self.progress(login=3, world=20)
        progress.observe(self.context, ACCOUNT)
        deadline = progress.login_deadline
        self.clock.advance(1)
        progress.observe(self.context, ACCOUNT)
        self.clock.advance(1)
        progress.observe(self.context, LOADING)
        self.clock.advance(0.5)
        progress.observe(self.context, ACCOUNT)
        self.assertEqual(progress.login_deadline, deadline)
        self.assertEqual(self.printed_keys().count("enter_game.login_required"), 1)
        self.clock.advance(0.5)
        self.assert_error(
            "enter_game.login_timeout", lambda: progress.check(self.context)
        )

    def test_account_reentry_uses_original_deadline_during_engine_loop(self):
        progress = self.progress(login=2, world=10)
        self.assert_error(
            "enter_game.login_timeout",
            lambda: self.run_entry([ACCOUNT, LOADING, ACCOUNT], progress),
        )
        self.assertAlmostEqual(self.clock.now, 2)
        self.assertEqual(self.printed_keys().count("enter_game.login_required"), 1)
        self.assert_no_input()

    def test_account_unknown_keeps_account_budget_until_positive_login_state(self):
        progress = self.progress(login=2, world=1)
        self.assert_error(
            "enter_game.login_timeout", lambda: self.run_entry([ACCOUNT, ()], progress)
        )
        self.assertEqual(progress.world_deadline, 1)
        self.assert_no_input()

    def test_slow_ocr_cannot_click_after_world_deadline(self):
        def slow(node, frame):
            if node == ENTER:
                self.clock.advance(3)
            return NotImplemented

        self.context.on_recognition = slow
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry([ENTER], self.progress(world=2)),
        )
        self.assert_no_input()

    def test_slow_ready_ocr_cannot_accept_success(self):
        ready_calls = []

        def slow(node, frame):
            if node == READY:
                ready_calls.append(node)
                if len(ready_calls) == 2:
                    self.clock.advance(3)
            return NotImplemented

        self.context.on_recognition = slow
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry([READY], self.progress(world=2)),
        )
        self.assertNotIn("enter_game.world_ready", self.printed_keys())
        self.assert_no_input()

    def test_slow_fresh_target_ocr_cannot_click(self):
        enter_calls = []

        def slow(node, frame):
            if node == ENTER:
                enter_calls.append(node)
                if len(enter_calls) == 2:
                    self.clock.advance(3)
            return NotImplemented

        self.context.on_recognition = slow
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry([ENTER], self.progress(world=2)),
        )
        self.assertEqual(len(self.capture.frames), 2)
        self.assert_no_input()

    def test_slow_initial_capture_cannot_trigger_input(self):
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry([Frame(ENTER, delay=3)], self.progress(world=2)),
        )
        self.assertEqual(self.context.recognitions, [])
        self.assert_no_input()

    def test_slow_fresh_capture_cannot_trigger_input(self):
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry(
                [ENTER, Frame(ENTER, delay=3)], self.progress(world=2)
            ),
        )
        self.assert_no_input()

    def test_slow_second_ready_capture_cannot_accept_success(self):
        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry(
                [READY, Frame(READY, delay=3)], self.progress(world=2)
            ),
        )
        self.assertNotIn("enter_game.world_ready", self.printed_keys())
        self.assert_no_input()

    def test_slow_account_ocr_cannot_accept_login_success_past_login_deadline(self):
        progress = self.progress(login=2)

        def slow(node, frame):
            if node == READY and READY in frame.states:
                self.clock.advance(3)
            return NotImplemented

        self.context.on_recognition = slow
        self.assert_error(
            "enter_game.login_timeout",
            lambda: self.run_entry([ACCOUNT, READY], progress),
        )
        self.assertNotIn("enter_game.world_ready", self.printed_keys())
        self.assert_no_input()

    def test_before_step_receives_active_deadline_and_cannot_bypass_it(self):
        deadlines = []
        progress = self.progress(world=2)

        def before(deadline):
            deadlines.append(deadline)
            self.clock.advance(3)

        self.assert_error(
            "enter_game.world_timeout",
            lambda: self.run_entry([READY], progress, before),
        )
        self.assertEqual(deadlines, [2])
        self.assertEqual(len(self.capture.frames), 0)
        self.assert_no_input()

    def test_before_step_receives_account_and_pending_deadlines(self):
        seen = []
        progress = self.progress(login=60, world=10, transition=3)
        self.assertTrue(
            self.run_entry(
                [ACCOUNT, ENTER, ENTER, LOADING, READY, READY], progress, seen.append
            )
        )
        self.assertIn(10, seen)
        self.assertIn(60, seen)
        self.assertTrue(any(3 <= deadline < 4 for deadline in seen))


class SafetyTests(CommonCase):
    def test_stopping_before_first_capture_returns_stopped(self):
        self.context.tasker.stopping = True
        self.assert_error(
            "enter_game.stopped", lambda: self.run_entry([READY], self.progress())
        )
        self.assertEqual(self.capture.frames, [])
        self.assert_no_input()

    def test_stopping_during_wait_is_checked_within_a_tenth_second(self):
        self.clock.on_sleep = lambda: setattr(self.context.tasker, "stopping", True)
        self.assert_error(
            "enter_game.stopped", lambda: self.run_entry([ACCOUNT], self.progress())
        )
        self.assertLessEqual(self.clock.now, 0.1)
        self.assert_no_input()

    def test_stopping_during_capture_does_not_recognize_or_input(self):
        stream = FrameStream(self.clock, [ENTER])
        stream.on_capture = lambda count, frame: setattr(
            self.context.tasker, "stopping", True
        )
        self.assert_error(
            "enter_game.stopped",
            lambda: self.common.run_world_entry(
                self.context, stream, self.click, self.key, progress=self.progress()
            ),
        )
        self.assertEqual(self.context.recognitions, [])
        self.assert_no_input()

    def test_stopping_during_ocr_rejects_late_input(self):
        def stop(node, frame):
            if node == ENTER:
                self.context.tasker.stopping = True
            return NotImplemented

        self.context.on_recognition = stop
        self.assert_error(
            "enter_game.stopped", lambda: self.run_entry([ENTER], self.progress())
        )
        self.assert_no_input()

    def test_stopping_during_second_ready_ocr_rejects_success(self):
        ready_calls = []

        def stop(node, frame):
            if node == READY:
                ready_calls.append(node)
                if len(ready_calls) == 2:
                    self.context.tasker.stopping = True
            return NotImplemented

        self.context.on_recognition = stop
        self.assert_error(
            "enter_game.stopped", lambda: self.run_entry([READY], self.progress())
        )
        self.assertNotIn("enter_game.world_ready", self.printed_keys())

    def test_invalid_frame_shapes_fail_before_recognition(self):
        for shape in (
            (1080, 1920, 3),
            (720, 1280, 1),
            (720, 1280, 2),
            (720, 1280, 5),
            (1280, 720, 3),
        ):
            with self.subTest(shape=shape):
                self.assert_error(
                    "enter_game.invalid_frame",
                    lambda: self.run_entry(
                        [Frame(READY, shape=shape)], self.progress()
                    ),
                )
        self.assertEqual(self.context.recognitions, [])
        self.assert_no_input()

    def test_non_array_frame_is_rejected(self):
        self.assert_error(
            "enter_game.invalid_frame",
            lambda: self.common.run_world_entry(
                self.context,
                lambda: object(),
                self.click,
                self.key,
                progress=self.progress(),
            ),
        )
        self.assert_no_input()

    def test_recognition_unavailable_is_error_not_unknown(self):
        self.context.on_recognition = lambda node, frame: None
        self.assert_error(
            "enter_game.recognition_failed",
            lambda: self.run_entry([READY], self.progress()),
        )
        self.assert_no_input()

    def test_failed_click_is_not_retried(self):
        self.click.return_value = False
        self.assert_error(
            "enter_game.input_rejected",
            lambda: self.run_entry([ENTER], self.progress()),
            "input_failed",
        )
        self.click.assert_called_once()

    def test_failed_escape_is_not_retried(self):
        self.key.return_value = False
        self.assert_error(
            "enter_game.input_rejected",
            lambda: self.run_entry([MONTHLY], self.progress()),
            "input_failed",
        )
        self.key.assert_called_once_with("Escape")
        self.click.assert_not_called()

    def test_ambiguous_target_is_rejected_before_click(self):
        self.assert_error(
            "enter_game.input_rejected",
            lambda: self.run_entry([Frame(ENTER, count=2)], self.progress()),
            "ambiguous_target",
        )
        self.assert_no_input()

    def test_invalid_boxes_are_rejected_before_click(self):
        for box in (
            (-1, 0, 10, 10),
            (0, 0, 0, 10),
            (1270, 0, 20, 10),
            (0, 710, 10, 20),
            (0, 0, float("inf"), 10),
            (True, 0, 10, 10),
            (0, 0, 10),
        ):
            with self.subTest(box=box):
                self.assert_error(
                    "enter_game.input_rejected",
                    lambda: self.run_entry([Frame(ENTER, box=box)], self.progress()),
                )
        self.assert_no_input()

    def test_object_style_box_is_supported(self):
        box = SimpleNamespace(x=600, y=600, w=80, h=40)
        self.assertTrue(
            self.run_entry(
                [Frame(ENTER, box=box), Frame(ENTER, box=box), READY, READY],
                self.progress(),
            )
        )
        self.click.assert_called_once_with(640.0, 620.0)


class WrapperTests(CommonCase):
    def setUp(self):
        super().setUp()
        self.wrapper, self.registrations = load_wrapper(self.common)
        self.wrapper.PrintT = Mock()
        self.argv = SimpleNamespace(
            node_name="EnterGameEntrance", custom_action_param=""
        )

    def set_controller(self, name):
        self.wrapper.controller_name = Mock(return_value=name)

    def dispatch(self):
        return self.wrapper.EnterGameDispatch().run(self.context, self.argv)

    def world(self):
        return self.wrapper.EnterGameWorld().run(self.context, self.argv)

    def install_controller(self, states):
        stream = FrameStream(self.clock, states)
        controller = Mock()
        controller.cached_image = None

        def capture():
            controller.cached_image = stream()
            return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))

        controller.post_screencap.side_effect = capture
        for name in ("post_click", "post_click_key"):
            getattr(controller, name).return_value.wait.return_value = SimpleNamespace(
                succeeded=True
            )
        self.context.tasker.controller = controller
        return controller, stream

    def test_wrapper_registers_all_three_actions_without_custom_package(self):
        self.assertEqual(
            set(self.registrations),
            {"enter_game_dispatch", "enter_game_world", "enter_game_fail"},
        )
        self.assertEqual(
            self.registrations["enter_game_world"], self.wrapper.EnterGameWorld
        )

    def test_all_three_client_controllers_dispatch_to_common_world(self):
        for name in ("Win32", "Win32-Front", "Win32-Background"):
            with self.subTest(name=name):
                self.context.override_next.reset_mock()
                self.set_controller(name)
                self.assertTrue(self.dispatch().success)
                self.context.override_next.assert_called_once_with(
                    "EnterGameEntrance", ["EnterGameWorld"]
                )
        self.context.tasker.post_task.assert_not_called()

    def test_native_cloud_controller_dispatches_to_cloud_start(self):
        self.set_controller("CloudGame-Front")
        self.assertTrue(self.dispatch().success)
        self.context.override_next.assert_called_once_with(
            "EnterGameEntrance", ["CloudGameStartEntrance"]
        )
        self.context.tasker.post_task.assert_not_called()

    def test_unknown_or_web_controller_is_rejected_without_dispatch(self):
        for name in ("", "unknown", "Adb", "CloudGame-Web"):
            with self.subTest(name=name):
                self.set_controller(name)
                self.assertFalse(self.dispatch().success)
        self.context.override_next.assert_not_called()
        self.assertEqual(
            self.wrapper.PrintT.call_args.args[1], "enter_game.wrong_controller"
        )

    def test_dispatch_validates_public_timeout_before_overriding_next(self):
        self.set_controller("Win32")
        self.context.nodes[self.common.LOGIN_TIMEOUT_NODE] = {
            "attach": {"minutes": 121}
        }
        self.assertFalse(self.dispatch().success)
        self.context.override_next.assert_not_called()
        self.assertEqual(
            self.wrapper.PrintT.call_args.args[1], "enter_game.invalid_config"
        )

    def test_override_next_failure_is_reported(self):
        self.set_controller("Win32")
        self.context.override_next.return_value = False
        self.assertFalse(self.dispatch().success)
        self.assertEqual(
            self.wrapper.PrintT.call_args.args[1], "enter_game.invalid_config"
        )

    def test_each_client_adapter_reaches_world_with_real_engine_and_fake_jobs(self):
        for name in ("Win32", "Win32-Front", "Win32-Background"):
            with self.subTest(name=name):
                self.set_controller(name)
                controller, stream = self.install_controller([READY, READY])
                self.assertTrue(self.world().success)
                self.assertEqual(len(stream.frames), 2)
                controller.post_click.assert_not_called()
                controller.post_click_key.assert_not_called()
                controller.post_key_down.assert_not_called()
                controller.post_key_up.assert_not_called()

    def test_client_wrapper_runs_account_enter_loading_popups_world(self):
        self.set_controller("Win32-Background")
        controller, stream = self.install_controller(
            [
                ACCOUNT,
                ENTER,
                ENTER,
                LOADING,
                ANNOUNCEMENT,
                ANNOUNCEMENT,
                MONTHLY,
                MONTHLY,
                READY,
                READY,
            ]
        )
        self.assertTrue(self.world().success)
        self.assertEqual(controller.post_click.call_count, 2)
        controller.post_click.assert_called_with(640, 620)
        controller.post_click_key.assert_called_once_with(27)
        controller.post_key_down.assert_not_called()
        controller.post_key_up.assert_not_called()
        self.assertEqual(len(stream.frames), 10)
        self.context.tasker.post_task.assert_not_called()

    def test_cloud_or_unknown_controller_cannot_use_win32_world_adapter(self):
        controller, stream = self.install_controller([READY])
        for name in ("CloudGame-Front", "CloudGame-Web", "Adb", ""):
            with self.subTest(name=name):
                self.set_controller(name)
                self.assertFalse(self.world().success)
        controller.post_screencap.assert_not_called()
        self.assertEqual(stream.frames, [])

    def test_failed_screencap_job_is_reported_without_recognition_or_input(self):
        self.set_controller("Win32")
        for status in (None, SimpleNamespace(succeeded=False)):
            with self.subTest(status=status):
                controller = Mock(cached_image=Frame(READY))
                controller.post_screencap.return_value.wait.return_value = status
                self.context.tasker.controller = controller
                self.assertFalse(self.world().success)
                controller.post_click.assert_not_called()
                controller.post_click_key.assert_not_called()
                controller.post_key_down.assert_not_called()
        self.assertEqual(
            self.wrapper.PrintT.call_args.args[1], "enter_game.invalid_frame"
        )
        self.assertEqual(self.context.recognitions, [])

    def test_successful_screencap_with_missing_cached_image_is_failure(self):
        self.set_controller("Win32")
        controller = Mock(cached_image=None)
        controller.post_screencap.return_value.wait.return_value = SimpleNamespace(
            succeeded=True
        )
        self.context.tasker.controller = controller
        self.assertFalse(self.world().success)
        self.assertEqual(
            self.wrapper.PrintT.call_args.args[1], "enter_game.invalid_frame"
        )

    def test_failed_click_job_is_not_retried(self):
        self.set_controller("Win32")
        controller, stream = self.install_controller([ENTER])
        controller.post_click.return_value.wait.return_value = SimpleNamespace(
            succeeded=False
        )
        self.assertFalse(self.world().success)
        controller.post_click.assert_called_once()
        self.assertEqual(len(stream.frames), 2)
        self.assertEqual(
            self.wrapper.PrintT.call_args.args[1], "enter_game.input_rejected"
        )

    def test_failed_click_key_job_is_reported_without_manual_input_or_retry(self):
        self.set_controller("Win32")
        controller, _ = self.install_controller([MONTHLY])
        controller.post_click_key.return_value.wait.return_value = SimpleNamespace(
            succeeded=False
        )
        self.assertFalse(self.world().success)
        controller.post_click_key.assert_called_once_with(27)
        controller.post_key_down.assert_not_called()
        controller.post_key_up.assert_not_called()
        self.assertEqual(
            self.wrapper.PrintT.call_args.args[1], "enter_game.input_rejected"
        )

    def test_missing_click_key_status_is_reported_without_retry(self):
        self.set_controller("Win32")
        controller, _ = self.install_controller([MONTHLY])
        controller.post_click_key.return_value.wait.return_value = None
        self.assertFalse(self.world().success)
        controller.post_click_key.assert_called_once_with(27)
        controller.post_key_down.assert_not_called()
        controller.post_key_up.assert_not_called()
        self.assertEqual(
            self.wrapper.PrintT.call_args.args[1], "enter_game.input_rejected"
        )

    def test_controller_capture_exception_is_caught_without_success(self):
        self.set_controller("Win32")
        controller, _ = self.install_controller([READY])
        controller.post_screencap.side_effect = RuntimeError("capture failed")
        self.assertFalse(self.world().success)
        self.assertEqual(self.wrapper.PrintT.call_args.args[1], "enter_game.failed")
        controller.post_click.assert_not_called()

    def test_stopping_wrapper_does_not_capture_dispatch_or_report_failure(self):
        self.set_controller("Win32")
        controller, _ = self.install_controller([READY])
        self.context.tasker.stopping = True
        self.assertFalse(self.dispatch().success)
        self.assertFalse(self.world().success)
        controller.post_screencap.assert_not_called()
        self.context.override_next.assert_not_called()
        self.wrapper.PrintT.assert_not_called()

    def test_stop_during_screencap_wait_does_not_recognize_or_input(self):
        self.set_controller("Win32")
        controller, _ = self.install_controller([ENTER])

        def stop_capture():
            controller.cached_image = Frame(ENTER)

            def wait():
                self.context.tasker.stopping = True
                return SimpleNamespace(succeeded=True)

            return SimpleNamespace(wait=wait)

        controller.post_screencap.side_effect = stop_capture
        self.assertFalse(self.world().success)
        controller.post_click.assert_not_called()
        self.assertEqual(self.context.recognitions, [])
        self.wrapper.PrintT.assert_not_called()

    def test_explicit_fail_action_never_returns_success(self):
        self.assertFalse(
            self.wrapper.EnterGameFail().run(self.context, self.argv).success
        )


class StaticResourceTests(unittest.TestCase):
    def test_import_helpers_restore_modules_and_do_not_import_action_package(self):
        names = (
            "utils",
            "utils.logger",
            "utils.maafocus",
            "utils.pienv",
            "game_entry",
            "maa",
            "maa.agent",
            "maa.agent.agent_server",
            "maa.custom_action",
            "custom",
            "custom.action",
            "_test_enter_game_common",
            "_test_enter_game_wrapper",
        )
        sentinel = object()
        before = {name: sys.modules.get(name, sentinel) for name in names}
        original_path = list(sys.path)
        common = load_common()
        load_wrapper(common)
        for name in names:
            self.assertIs(sys.modules.get(name, sentinel), before[name], name)
        self.assertEqual(sys.path, original_path)

    def test_public_and_web_common_modules_do_not_import_agent_server(self):
        for relative in ("agent/game_entry.py", "agent/cloud_web_flow.py"):
            with self.subTest(module=relative):
                tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertFalse(
                            any("agent_server" in alias.name for alias in node.names)
                        )
                    elif isinstance(node, ast.ImportFrom):
                        self.assertNotIn("agent_server", node.module or "")
                        self.assertFalse(
                            any(alias.name == "AgentServer" for alias in node.names)
                        )

    def test_all_enter_game_agent_keys_exist_in_every_language(self):
        documents = {
            lang: read_json(f"assets/resource/locales/agent/{lang}.json")
            for lang in LANGUAGES
        }
        keys = {
            key
            for document in documents.values()
            for key in document
            if key.startswith("enter_game.")
        }
        # 检查生产源码里的固定 PrintT / EntryError key，及 attach.report 拼接的 key。
        for relative in ("agent/game_entry.py", "agent/custom/action/enter_game.py"):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            keys.update(
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("enter_game.")
                and node.value != "enter_game."
            )
        nodes = read_json("assets/resource/base/pipeline/CloudGame/CloudGameWeb.json")
        keys.update(
            "enter_game." + node["attach"]["report"]
            for node in nodes.values()
            if node.get("attach", {}).get("web_action") in ("click", "esc")
        )
        self.assertGreaterEqual(len(keys), 20)
        for lang, document in documents.items():
            for key in sorted(keys):
                with self.subTest(language=lang, key=key):
                    self.assertIsInstance(document.get(key), str)
                    self.assertTrue(document[key].strip())
                    self.assertEqual(
                        re.findall(r"%(?:\d+\$)?[dsf]", document[key]),
                        re.findall(r"%(?:\d+\$)?[dsf]", documents["zh_cn"][key]),
                    )

    def test_task_labels_and_option_descriptions_exist_in_five_languages(self):
        task = read_json("assets/resource/tasks/CloudGame.json")

        def label_keys(value):
            if isinstance(value, dict):
                return set().union(*(label_keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(label_keys(item) for item in value))
            return (
                {value[1:]}
                if isinstance(value, str) and value.startswith("$")
                else set()
            )

        keys = label_keys(task)
        for lang in LANGUAGES:
            document = read_json(f"assets/resource/locales/interface/{lang}.json")
            for key in keys:
                with self.subTest(language=lang, key=key):
                    self.assertIsInstance(document.get(key), str)
                    self.assertTrue(document[key].strip())

    def test_public_login_option_overrides_exactly_one_shared_node(self):
        task = read_json("assets/resource/tasks/CloudGame.json")
        option = task["option"]["CloudWebLoginTimeout"]
        self.assertEqual(
            option["pipeline_override"],
            {"EnterGameLoginTimeoutConfig": {"attach": {"minutes": "{minutes}"}}},
        )
        self.assertEqual(option["inputs"][0]["default"], "10")
        self.assertEqual(option["inputs"][0]["pipeline_type"], "int")
        verify = option["inputs"][0]["verify"]
        for minutes in range(0, 122):
            self.assertEqual(
                bool(re.fullmatch(verify, str(minutes))), 1 <= minutes <= 120, minutes
            )
        for invalid in ("-1", "+1", " 10", "1.5", "١", "１"):
            self.assertIsNone(re.fullmatch(verify, invalid))
        pipeline = read_json("assets/resource/base/pipeline/EnterGame.json")
        self.assertEqual(
            pipeline["EnterGameLoginTimeoutConfig"]["attach"], {"minutes": 10}
        )

    def test_unified_task_entry_supports_all_four_controllers(self):
        task = read_json("assets/resource/tasks/CloudGame.json")["task"][0]
        self.assertEqual(task["entry"], "EnterGameEntrance")
        self.assertEqual(
            set(task["controller"]),
            {"Win32", "Win32-Front", "Win32-Background", "CloudGame-Front"},
        )
        self.assertIn("CloudWebLoginTimeout", task["option"])
        self.assertIn(
            "resource/tasks/CloudGame.json",
            read_json("assets/interface.json")["import"],
        )

    def test_world_node_and_account_resources_exist_and_enter_fallback_is_full_text(
        self,
    ):
        common = load_common()
        nodes = read_json("assets/resource/base/pipeline/EnterGame.json")
        nodes.update(
            read_json("assets/resource/base/pipeline/CloudGame/CloudGameWeb.json")
        )
        for node in common.WORLD_NODES:
            self.assertIn(node, nodes)
        account = nodes[ACCOUNT]["recognition"]
        self.assertEqual(account["type"], "OCR")
        self.assertEqual(account["param"]["roi"], [250, 80, 780, 500])
        for text in ("账号密码登录", "安全验证", "请在手机上确认登录", "验证码"):
            self.assertIn(text, account["param"]["expected"])
        self.assertNotIn("同意", account["param"]["expected"])
        self.assertNotIn("协议", account["param"]["expected"])
        enter = nodes[ENTER]["recognition"]
        self.assertEqual(enter["type"], "OCR")
        self.assertIn("进入游戏", enter["param"]["expected"])
        self.assertNotIn("进入", enter["param"]["expected"])
        self.assertIn(ENTER, nodes["EnterGameScreen"]["recognition"]["param"]["any_of"])


if __name__ == "__main__":
    unittest.main()
