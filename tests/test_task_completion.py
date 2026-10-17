"""Completion routes must stop one pipeline while preserving the task queue."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

_ROOT = Path(__file__).resolve().parents[1]
_ACTIONS = _ROOT / "agent" / "custom" / "action"


def _pipeline_nodes(relative_path):
    text = (_ROOT / "assets/resource/base/pipeline" / relative_path).read_text(
        encoding="utf-8"
    )
    return json.loads(re.sub(r"//.*", "", text))


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_actions():
    """Keep stubs and action registration out of other tests and real maa imports."""
    modules = {}
    for name in (
        "maa",
        "maa.agent",
        "maa.agent.agent_server",
        "maa.context",
        "maa.custom_action",
        "maa.pipeline",
        "utils",
        "utils.logger",
        "utils.maafocus",
        "cv2",
        "numpy",
        "_completion_actions",
        "_completion_actions.Common",
        "_completion_actions.Tetris",
        "_completion_actions.Tetris.feats",
        "_completion_actions.Tetris.feats.play",
    ):
        modules[name] = ModuleType(name)
        modules[name].__path__ = []

    class CustomAction:
        RunArg = object

        class RunResult:
            def __init__(self, success):
                self.success = success

    modules["maa.custom_action"].CustomAction = CustomAction
    modules["maa.context"].Context = object
    modules["maa.agent.agent_server"].AgentServer = SimpleNamespace(
        custom_action=lambda name: lambda cls: cls
    )
    modules["maa.pipeline"].JOCR = lambda **kwargs: SimpleNamespace(**kwargs)
    modules["maa.pipeline"].JRecognitionType = SimpleNamespace(OCR="OCR")
    modules["cv2"].COLOR_BGRA2BGR = 1
    modules["cv2"].cvtColor = lambda frame, _: frame
    messages = modules["utils.maafocus"].PrintT = Mock()
    modules["utils.logger"].logger = Mock()
    player = SimpleNamespace(play_round=Mock(return_value=True))
    player_factory = Mock(return_value=player)
    modules["_completion_actions.Tetris.feats.play"].TetrisGamePlayer = player_factory

    with patch.dict(sys.modules, modules):
        _load_module("utils.task_control", _ROOT / "agent/utils/task_control.py")
        _load_module("_completion_actions.Common.utils", _ACTIONS / "Common/utils.py")
        tetris = _load_module(
            "_completion_actions.auto_tetris", _ACTIONS / "auto_tetris.py"
        )
        lucky = _load_module(
            "_completion_actions.lucky_star_round", _ACTIONS / "lucky_star_round.py"
        )
    return tetris, lucky, player_factory, messages


class _Job:
    def __init__(self, succeeded=True):
        self.succeeded = succeeded

    def wait(self):
        return self


class _Context:
    def __init__(self):
        self.tasker = SimpleNamespace(
            stopping=False,
            post_stop=Mock(side_effect=AssertionError("Queue must keep running")),
            controller=SimpleNamespace(
                post_key_down=Mock(return_value=_Job()),
                post_key_up=Mock(return_value=_Job()),
                post_screencap=Mock(return_value=_Job()),
                cached_image=SimpleNamespace(size=1, shape=(1, 1, 3)),
            ),
        )
        self.nodes = {
            "TetrisHandleGameActive": {
                "action": {
                    "type": "Custom",
                    "param": {"custom_action_param": {"use_all_vitality": False}},
                },
                "next": ["TetrisExit"],
            },
            "TetrisCheckVitality": {"next": ["TetrisExit"]},
            "PukaLandLuckyStarRound": {"next": ["PukaLandLuckyStarExit"]},
            "PukaLandLuckyStarResultGate": {"next": ["PukaLandLuckyStarExit"]},
        }
        self.override_next = Mock(side_effect=self._override_next)
        self.override_pipeline = Mock(side_effect=self._override_pipeline)
        self.get_node_data = Mock(side_effect=lambda name: self.nodes.get(name))
        self.run_action = Mock(return_value=SimpleNamespace(success=True))
        self.run_recognition_direct = Mock(return_value=self.ocr("0"))

    @staticmethod
    def ocr(text, hit=True):
        rows = [SimpleNamespace(text=text)] if text is not None else []
        return SimpleNamespace(hit=hit, filtered_results=rows, all_results=rows)

    def _override_next(self, name, next_nodes):
        self.nodes.setdefault(name, {})["next"] = list(next_nodes)
        return True

    def _override_pipeline(self, overrides):
        for name, fields in overrides.items():
            self.nodes.setdefault(name, {}).update(copy.deepcopy(fields))
        return True


def _args(name, params=None, **kwargs):
    return SimpleNamespace(
        node_name=name,
        custom_action_param=json.dumps(params or {}),
        reco_detail=kwargs.get("reco_detail", SimpleNamespace(hit=True)),
        box=kwargs.get("box", [10, 20, 30, 40]),
    )


class TaskCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tetris, self.lucky, self.player, self.messages = _load_actions()
        self.context = _Context()
        self.controller = self.context.tasker.controller
        self.wait = patch.object(self.tetris, "interruptible_wait", return_value=True)
        self.wait.start()
        self.addCleanup(self.wait.stop)

    def run_tetris(self, **params):
        return self.tetris.AutoTetris().run(self.context, _args("GameAlias", params))

    def run_vitality(self, text="0"):
        self.context.run_recognition_direct.return_value = self.context.ocr(text)
        return self.tetris.TetrisCheckVitalityAction().run(
            self.context, _args("VitalityAlias")
        )

    def test_independent_loading_restores_existing_modules(self):
        before = dict(sys.modules)
        _load_actions()
        self.assertEqual(before, dict(sys.modules))

    def test_pipeline_completion_nodes_stop_the_current_task(self):
        for path, name in (
            ("Tetris/Tetris.json", "TetrisExit"),
            ("PukaLand/PukaLandLuckyStar.json", "PukaLandLuckyStarExit"),
        ):
            with self.subTest(node=name):
                node = _pipeline_nodes(path)[name]
                self.assertEqual(node["recognition"]["type"], "DirectHit")
                self.assertEqual(node["action"]["type"], "StopTask")
                self.assertEqual(node["next"], [])

    def test_tetris_failure_cleanup_preserves_failure(self):
        nodes = _pipeline_nodes("Tetris/Tetris.json")
        self.assertEqual(
            nodes["TetrisDismissResultThenExit"]["next"], ["TetrisExitWithError"]
        )
        failure = nodes["TetrisExitWithError"]
        self.assertEqual(failure["action"]["type"], "Custom")
        self.assertEqual(failure["action"]["param"]["custom_action"], "fail_task")
        self.assertEqual(failure["next"], [])
        self.assertFalse(failure.get("on_error"))

    def test_tetris_completes_exact_round_count_and_uses_calling_node(self):
        for _ in range(2):
            self.assertTrue(self.run_tetris(repeat_count=3).success)
            self.assertEqual(
                self.context.nodes["GameAlias"]["next"],
                ["TetrisDismissResult", "TetrisExit"],
            )
            self.controller.post_key_down.assert_not_called()
        self.assertTrue(self.run_tetris(repeat_count=3).success)
        self.assertEqual(self.tetris._round_count, 3)
        self.assertEqual(self.player.return_value.play_round.call_count, 3)
        self.assertEqual(self.context.nodes["GameAlias"]["next"], ["TetrisExit"])
        self.controller.post_key_down.assert_called_once_with(27)
        self.controller.post_key_up.assert_called_once_with(27)
        self.context.tasker.post_stop.assert_not_called()
        self.assertTrue(self.run_tetris(repeat_count=3).success)
        self.assertEqual(self.player.return_value.play_round.call_count, 3)

    def test_tetris_vitality_mode_preserves_continuation(self):
        self.assertTrue(self.run_tetris(use_all_vitality=True).success)
        self.assertEqual(
            self.context.nodes["GameAlias"]["next"],
            ["[JumpBack]TetrisCheckVitality", "TetrisExit"],
        )
        self.controller.post_key_down.assert_not_called()

    def test_tetris_routing_failure_prevents_playing(self):
        self.context.override_next.side_effect = None
        self.context.override_next.return_value = False
        self.assertFalse(self.run_tetris().success)
        self.player.assert_not_called()

    def test_tetris_completion_routing_failure_is_not_success(self):
        self.context.override_next.side_effect = [True, False]
        self.assertFalse(self.run_tetris().success)
        self.assertNotIn(
            "tetris.task_done", [call.args[1] for call in self.messages.call_args_list]
        )

    def test_tetris_failed_round_does_not_complete(self):
        self.player.return_value.play_round.return_value = False
        self.assertFalse(self.run_tetris().success)
        self.assertEqual(self.tetris._round_count, 0)
        self.controller.post_key_down.assert_not_called()
        self.messages.assert_any_call(self.context, "tetris.task_failed")

    def test_tetris_cancellation_before_playing(self):
        self.context.tasker.stopping = True
        self.assertFalse(self.run_tetris().success)
        self.player.assert_not_called()
        self.messages.assert_not_called()

    def test_tetris_cancellation_after_player_success_is_not_completion(self):
        def cancel(*_):
            self.context.tasker.stopping = True
            return True

        self.player.return_value.play_round.side_effect = cancel
        self.assertFalse(self.run_tetris().success)
        self.assertEqual(self.tetris._round_count, 0)
        self.controller.post_key_down.assert_not_called()

    def test_escape_is_released_on_cancellation(self):
        def cancel(*_):
            self.context.tasker.stopping = True
            return False

        self.tetris.interruptible_wait.side_effect = cancel
        self.assertFalse(self.run_tetris().success)
        self.controller.post_key_up.assert_called_once_with(27)
        self.assertNotIn(
            "tetris.task_done", [call.args[1] for call in self.messages.call_args_list]
        )

    def test_escape_is_released_when_key_down_raises(self):
        self.controller.post_key_down.side_effect = RuntimeError("controller failed")
        self.assertFalse(self.run_tetris().success)
        self.controller.post_key_up.assert_called_once_with(27)

    def test_escape_key_up_failure_prevents_success(self):
        self.controller.post_key_up.return_value = _Job(False)
        self.assertFalse(self.run_tetris().success)

    def test_tetris_reset_restores_routes_and_keeps_vitality_option(self):
        for v2 in (False, True):
            with self.subTest(v2=v2):
                params = {"custom_action_param": {"use_all_vitality": True}}
                self.context.nodes["TetrisHandleGameActive"] = (
                    {"action": {"type": "Custom", "param": params}} if v2 else params
                )
                self.tetris._single_shot_done = True
                self.tetris._round_count = 8
                result = self.tetris.TetrisResetContext().run(
                    self.context, _args("Reset", {"allow_speed_drop": True})
                )
                self.assertTrue(result.success)
                self.assertEqual(self.tetris._round_count, 0)
                self.assertFalse(self.tetris._single_shot_done)
                self.assertTrue(self.tetris._allow_speed_drop)
                self.assertEqual(
                    self.context.nodes["TetrisHandleGameActive"]["next"],
                    ["[JumpBack]TetrisCheckVitality", "TetrisExit"],
                )
                self.assertIn(
                    "[JumpBack]TetrisSceneDispatch",
                    self.context.nodes["TetrisCheckVitality"]["next"],
                )

    def test_reset_override_failures_are_reported(self):
        self.context.override_pipeline.side_effect = None
        self.context.override_pipeline.return_value = False
        for action in (self.tetris.TetrisResetContext(), self.lucky.LuckyStarReset()):
            with self.subTest(action=type(action).__name__):
                self.assertFalse(action.run(self.context, _args("Reset")).success)

    def test_zero_vitality_completes_current_pipeline_successfully(self):
        self.assertTrue(self.run_vitality("0").success)
        self.assertEqual(self.context.nodes["VitalityAlias"]["next"], ["TetrisExit"])
        self.messages.assert_called_once_with(self.context, "tetris.vitality_exhausted")
        self.controller.post_key_up.assert_called_once_with(27)
        self.context.tasker.post_stop.assert_not_called()

    def test_positive_vitality_restores_continuation(self):
        self.context.nodes["VitalityAlias"] = {"next": ["TetrisExit"]}
        self.assertTrue(self.run_vitality("20").success)
        self.assertIn(
            "[JumpBack]TetrisSceneDispatch", self.context.nodes["VitalityAlias"]["next"]
        )
        self.messages.assert_not_called()
        self.controller.post_key_up.assert_called_once_with(27)

    def test_missing_vitality_is_not_zero(self):
        for detail in (
            None,
            self.context.ocr(None),
            self.context.ocr("活力"),
            self.context.ocr("0", hit=False),
        ):
            with self.subTest(detail=detail):
                self.context.run_recognition_direct.return_value = detail
                result = self.tetris.TetrisCheckVitalityAction().run(
                    self.context, _args("VitalityAlias")
                )
                self.assertFalse(result.success)
        self.messages.assert_not_called()
        self.controller.post_key_down.assert_not_called()
        self.context.override_next.assert_not_called()

    def test_failed_screenshot_does_not_use_cached_vitality(self):
        self.controller.post_screencap.return_value = _Job(False)
        self.assertFalse(self.run_vitality().success)
        self.context.run_recognition_direct.assert_not_called()
        self.messages.assert_not_called()

    def test_vitality_cancellation_during_ocr_does_not_complete(self):
        def cancel(*_):
            self.context.tasker.stopping = True
            return self.context.ocr("0")

        self.context.run_recognition_direct.side_effect = cancel
        self.assertFalse(self.run_vitality().success)
        self.controller.post_key_down.assert_not_called()
        self.messages.assert_not_called()

    def test_lucky_zero_rounds_stops_without_starting_or_incrementing(self):
        result = self.lucky.LuckyStarRoundGate().run(
            self.context, _args("RoundAlias", {"repeat_count": 0})
        )
        self.assertTrue(result.success)
        self.assertEqual(self.lucky._round_count, 0)
        self.assertEqual(
            self.context.nodes["RoundAlias"]["next"], ["PukaLandLuckyStarExit"]
        )
        self.context.run_action.assert_not_called()
        self.context.tasker.post_stop.assert_not_called()

    def test_lucky_exact_round_count_clicks_retry_only_between_rounds(self):
        gate = self.lucky.LuckyStarRoundGate()
        retry = self.lucky.LuckyStarRetryOrFinish()
        self.assertTrue(
            gate.run(self.context, _args("RoundAlias", {"repeat_count": 2})).success
        )
        self.assertEqual(
            self.context.nodes["RoundAlias"]["next"], ["PukaLandLuckyStarStart"]
        )
        self.assertTrue(retry.run(self.context, _args("ResultAlias")).success)
        self.context.run_action.assert_called_once_with(
            "ResultAlias",
            box=[10, 20, 30, 40],
            pipeline_override={
                "ResultAlias": {"action": {"type": "Click", "param": {"target": True}}}
            },
        )
        self.assertTrue(
            gate.run(self.context, _args("RoundAlias", {"repeat_count": 2})).success
        )
        self.assertEqual(
            self.context.nodes["RoundAlias"]["next"], ["PukaLandLuckyStarCountdown"]
        )
        self.assertTrue(retry.run(self.context, _args("ResultAlias")).success)
        self.assertEqual(self.lucky._round_count, 2)
        self.assertEqual(self.context.run_action.call_count, 1)
        self.assertEqual(
            self.context.nodes["ResultAlias"]["next"], ["PukaLandLuckyStarExit"]
        )
        self.assertTrue(
            gate.run(self.context, _args("RoundAlias", {"repeat_count": 2})).success
        )
        self.assertEqual(self.lucky._round_count, 2)
        self.context.tasker.post_stop.assert_not_called()

    def test_lucky_failed_route_does_not_count_or_click(self):
        self.context.override_next.side_effect = None
        self.context.override_next.return_value = False
        self.assertFalse(
            self.lucky.LuckyStarRoundGate()
            .run(self.context, _args("RoundAlias"))
            .success
        )
        self.assertEqual(self.lucky._round_count, 0)
        self.assertFalse(
            self.lucky.LuckyStarRetryOrFinish()
            .run(self.context, _args("ResultAlias"))
            .success
        )
        self.context.run_action.assert_not_called()
        self.lucky._round_count = self.lucky._total_rounds
        self.assertFalse(
            self.lucky.LuckyStarRetryOrFinish()
            .run(self.context, _args("ResultAlias"))
            .success
        )
        self.context.run_action.assert_not_called()

    def test_lucky_failed_click_is_not_success(self):
        for result in (None, SimpleNamespace(success=False)):
            with self.subTest(result=result):
                self.context.run_action.return_value = result
                self.assertFalse(
                    self.lucky.LuckyStarRetryOrFinish()
                    .run(self.context, _args("ResultAlias"))
                    .success
                )

    def test_lucky_missing_button_does_not_click(self):
        for args in (
            _args("ResultAlias", reco_detail=None),
            _args("ResultAlias", box=None),
        ):
            self.assertFalse(
                self.lucky.LuckyStarRetryOrFinish().run(self.context, args).success
            )
        self.context.run_action.assert_not_called()

    def test_lucky_cancellation_prevents_actions(self):
        self.context.tasker.stopping = True
        for action in (
            self.lucky.LuckyStarReset(),
            self.lucky.LuckyStarRoundGate(),
            self.lucky.LuckyStarRetryOrFinish(),
        ):
            with self.subTest(action=type(action).__name__):
                self.assertFalse(action.run(self.context, _args("Alias")).success)
        self.context.override_next.assert_not_called()
        self.context.override_pipeline.assert_not_called()
        self.context.run_action.assert_not_called()

    def test_lucky_cancellation_during_click_is_not_success(self):
        def cancel(*_, **__):
            self.context.tasker.stopping = True
            return SimpleNamespace(success=True)

        self.context.run_action.side_effect = cancel
        self.assertFalse(
            self.lucky.LuckyStarRetryOrFinish()
            .run(self.context, _args("ResultAlias"))
            .success
        )

    def test_lucky_reset_removes_prior_completion_routes(self):
        self.lucky._round_count = 5
        self.assertTrue(
            self.lucky.LuckyStarReset().run(self.context, _args("Reset")).success
        )
        self.assertEqual(self.lucky._round_count, 0)
        self.assertEqual(
            self.context.nodes["PukaLandLuckyStarRound"]["next"],
            ["PukaLandLuckyStarStart", "PukaLandLuckyStarCountdown"],
        )
        self.assertEqual(
            self.context.nodes["PukaLandLuckyStarResultGate"]["next"],
            ["PukaLandLuckyStarRestarted"],
        )
        self.assertTrue(
            self.lucky.LuckyStarRoundGate()
            .run(self.context, _args("PukaLandLuckyStarRound"))
            .success
        )
        self.assertTrue(
            self.lucky.LuckyStarRetryOrFinish()
            .run(self.context, _args("PukaLandLuckyStarResultGate"))
            .success
        )
        self.context.run_action.assert_called_once()


class NativeStopTaskTests(unittest.TestCase):
    def test_stop_task_discards_jumpback_and_keeps_queue_and_overrides_isolated(self):
        if os.environ.get("MAANTE_NATIVE_COMPLETION_TEST") != "1":
            if importlib.util.find_spec("maa") is None:
                self.skipTest("Native MaaFramework is unavailable")
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "NativeStopTaskTests"],
                env={**os.environ, "MAANTE_NATIVE_COMPLETION_TEST": "1"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return

        try:
            import numpy as np
            from maa.controller import CustomController
            from maa.custom_action import CustomAction
            from maa.resource import Resource
            from maa.tasker import Tasker
        except (ImportError, OSError) as exc:
            self.skipTest("Native MaaFramework is unavailable: %s" % exc)

        class Controller(CustomController):
            def connect(self):
                return True

            def connected(self):
                return True

            def request_uuid(self):
                return "task-completion-test"

            def get_features(self):
                return 0

            def screencap(self):
                return np.zeros((720, 1280, 3), dtype=np.uint8)

            def key_down(self, keycode):
                return True

            def key_up(self, keycode):
                return True

            def click(self, x, y):
                clicks.append((x, y))
                return True

        class Callback(CustomAction):
            def __init__(self, callback):
                super().__init__()
                self.callback = callback

            def run(self, context, argv):
                return self.callback(context, argv)

        for task in ("tetris", "lucky_gate", "lucky_result", "lucky_retry"):
            with self.subTest(task=task):
                tetris, lucky, _, _ = _load_actions()
                events = []
                initial_routes = []
                clicks = []
                tetris._target_round = 1
                tetris._single_shot_done = True
                lucky._round_count = 1
                lucky._total_rounds = 2 if task == "lucky_retry" else 1
                completion = {
                    "tetris": tetris.AutoTetris,
                    "lucky_gate": lucky.LuckyStarRoundGate,
                    "lucky_result": lucky.LuckyStarRetryOrFinish,
                    "lucky_retry": lucky.LuckyStarRetryOrFinish,
                }[task]()
                path, exit_node = (
                    ("Tetris/Tetris.json", "TetrisExit")
                    if task == "tetris"
                    else ("PukaLand/PukaLandLuckyStar.json", "PukaLandLuckyStarExit")
                )

                def complete(context, argv):
                    events.append("complete")
                    initial_routes.append(context.get_node_data("Gate")["next"])
                    return completion.run(context, argv).success

                def probe(context, argv):
                    events.append("probe")
                    initial_routes.append(context.get_node_data("Gate")["next"])
                    return True

                def unexpected(context, argv):
                    events.append("unexpected")
                    return False

                def node(action_type="DoNothing", **fields):
                    return {
                        "recognition": {"type": "DirectHit"},
                        "action": {"type": action_type},
                        "pre_delay": 0,
                        "post_delay": 0,
                        "rate_limit": 0,
                        **fields,
                    }

                resource = Resource()
                controller = Controller()
                tasker = Tasker()
                callbacks = [Callback(complete), Callback(probe), Callback(unexpected)]
                for name, callback in zip(
                    ("complete", "probe", "unexpected"), callbacks
                ):
                    self.assertTrue(resource.register_custom_action(name, callback))
                pipeline = {
                    "Root": node(next=["[JumpBack]Gate"]),
                    "Gate": node(
                        "Custom",
                        action={
                            "type": "Custom",
                            "param": {
                                "custom_action": "complete",
                                "custom_action_param": {
                                    "repeat_count": 0 if task == "lucky_gate" else 1
                                },
                            },
                        },
                        next=["Unexpected"],
                    ),
                    exit_node: _pipeline_nodes(path)[exit_node],
                    "PukaLandLuckyStarRestarted": node("StopTask"),
                    "Probe": node(
                        action={"type": "Custom", "param": {"custom_action": "probe"}}
                    ),
                    "Unexpected": node(
                        action={
                            "type": "Custom",
                            "param": {"custom_action": "unexpected"},
                        }
                    ),
                }
                self.assertTrue(resource.override_pipeline(pipeline))
                original_next = resource.get_node_data("Gate")["next"]
                self.assertTrue(controller.post_connection().wait().succeeded)
                self.assertTrue(tasker.bind(resource, controller))
                jobs = [tasker.post_task(entry) for entry in ("Root", "Probe", "Root")]
                deadline = time.monotonic() + 5
                try:
                    while not all(job.done for job in jobs):
                        if time.monotonic() >= deadline:
                            self.fail(
                                "Completion pipeline did not stop within five seconds"
                            )
                        time.sleep(0.01)
                    self.assertTrue(all(job.succeeded for job in jobs))
                    self.assertEqual(events, ["complete", "probe", "complete"])
                    self.assertEqual(len(clicks), 2 if task == "lucky_retry" else 0)
                    self.assertEqual(initial_routes, [original_next] * 3)
                    self.assertEqual(
                        resource.get_node_data("Gate")["next"], original_next
                    )
                finally:
                    if tasker.running:
                        tasker.post_stop().wait()


if __name__ == "__main__":
    unittest.main()
