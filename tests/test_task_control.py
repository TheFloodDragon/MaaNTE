import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_isolated(name, path, modules):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


LOGGER = Mock()
LOGGER_MODULE = ModuleType("utils.logger")
LOGGER_MODULE.logger = LOGGER
CONTROL = load_isolated(
    "_task_control_tests",
    "agent/utils/task_control.py",
    {"utils.logger": LOGGER_MODULE},
)
CUSTOM = ModuleType("maa.custom_action")
CUSTOM.CustomAction = type(
    "CustomAction", (), {"RunArg": SimpleNamespace, "RunResult": SimpleNamespace}
)
SERVER = ModuleType("maa.agent.agent_server")
SERVER.AgentServer = SimpleNamespace(custom_action=lambda _: lambda cls: cls)
CONTEXT = ModuleType("maa.context")
CONTEXT.Context = object
REALTIME = load_isolated(
    "_realtime_control_tests",
    "agent/custom/action/realtime_task.py",
    {
        "utils.logger": LOGGER_MODULE,
        "utils.task_control": CONTROL,
        "maa.custom_action": CUSTOM,
        "maa.context": CONTEXT,
        "maa.agent.agent_server": SERVER,
    },
)


def task_detail(success):
    return SimpleNamespace(status=SimpleNamespace(succeeded=success))


def context_for(result):
    return SimpleNamespace(
        tasker=SimpleNamespace(stopping=False),
        run_task=Mock(return_value=result),
        get_node_data=Mock(return_value={"enabled": True}),
    )


class TaskControlTests(unittest.TestCase):
    def test_failed_nonempty_detail_is_failure(self):
        context = context_for(task_detail(False))
        self.assertFalse(CONTROL.run_task_checked(context, "RequiredStep"))
        self.assertEqual(context.run_task.call_count, 1)

    def test_missing_detail_is_failure(self):
        self.assertFalse(CONTROL.run_task_checked(context_for(None), "RequiredStep"))

    def test_successful_detail_propagates(self):
        self.assertTrue(
            CONTROL.run_task_checked(context_for(task_detail(True)), "RequiredStep")
        )

    def test_failed_action_detail_is_failure(self):
        self.assertFalse(CONTROL.action_succeeded(SimpleNamespace(success=False)))
        self.assertFalse(CONTROL.action_succeeded(None))
        self.assertTrue(CONTROL.action_succeeded(SimpleNamespace(success=True)))

    def test_cancel_before_subtask_does_not_run_it(self):
        context = context_for(task_detail(True))
        context.tasker.stopping = True
        with self.assertRaises(CONTROL.TaskCancelledError):
            CONTROL.require_task(context, "RequiredStep")
        context.run_task.assert_not_called()

    def test_cancel_during_subtask_cannot_report_success(self):
        context = context_for(None)

        def stop(*_):
            context.tasker.stopping = True
            return task_detail(True)

        context.run_task.side_effect = stop
        with self.assertRaises(CONTROL.TaskCancelledError):
            CONTROL.require_task(context, "RequiredStep")

    def test_wait_cancels_within_one_poll(self):
        context = context_for(None)
        elapsed = [0.0]

        def sleep(seconds):
            elapsed[0] += seconds
            context.tasker.stopping = True

        with patch.object(CONTROL.time, "monotonic", side_effect=lambda: elapsed[0]):
            with patch.object(CONTROL.time, "sleep", side_effect=sleep):
                self.assertFalse(CONTROL.interruptible_wait(context, 60))
        self.assertLessEqual(elapsed[0], 0.05)

    def test_wait_finishes_without_oversleep(self):
        elapsed = [0.0]
        with patch.object(CONTROL.time, "monotonic", side_effect=lambda: elapsed[0]):
            with patch.object(
                CONTROL.time,
                "sleep",
                side_effect=lambda seconds: elapsed.__setitem__(
                    0, elapsed[0] + seconds
                ),
            ):
                self.assertTrue(CONTROL.wait_until_cancelled(0.12, lambda: False))
        self.assertAlmostEqual(elapsed[0], 0.12)

    def test_invalid_wait_cannot_spin_forever(self):
        for duration in (-1, float("nan"), float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                CONTROL.wait_until_cancelled(duration, lambda: False)
        with self.assertRaises(ValueError):
            CONTROL.wait_until_cancelled(1, lambda: False, interval=0)


class RealtimeTaskTests(unittest.TestCase):
    def setUp(self):
        self.action = REALTIME.RealTimeTaskAction()
        self.argv = SimpleNamespace(custom_action_param=json.dumps({"nodes": ["Loot"]}))

    def test_each_action_returns_after_one_cycle(self):
        context = context_for(task_detail(True))
        for expected in (1, 2):
            self.assertTrue(self.action.run(context, self.argv).success)
            self.assertEqual(context.run_task.call_count, expected)

    def test_no_match_has_idle_fallback(self):
        context = context_for(task_detail(True))
        self.assertTrue(self.action.run(context, self.argv).success)
        override = context.run_task.call_args.args[1]
        self.assertEqual(
            override[REALTIME.HOLDER_NODE_NAME]["next"], ["Loot", "RealTimeSleep"]
        )

    def test_failed_cycle_is_not_retried_or_masked(self):
        for result in (None, task_detail(False)):
            with self.subTest(result=result):
                context = context_for(result)
                self.assertFalse(self.action.run(context, self.argv).success)
                self.assertEqual(context.run_task.call_count, 1)

    def test_cancelled_action_does_not_start_a_cycle(self):
        context = context_for(task_detail(True))
        context.tasker.stopping = True
        self.assertFalse(self.action.run(context, self.argv).success)
        context.run_task.assert_not_called()

    def test_invalid_configuration_does_not_dispatch(self):
        invalid = [
            None,
            "not json",
            {},
            {"nodes": []},
            {"nodes": [""]},
            {"nodes": ["RealTimeTaskMain"]},
            {"nodes": ["[JumpBack]Loot"]},
        ]
        for params in invalid:
            with self.subTest(params=params):
                context = context_for(task_detail(True))
                argv = SimpleNamespace(custom_action_param=params)
                self.assertFalse(self.action.run(context, argv).success)
                context.run_task.assert_not_called()

    def test_missing_node_does_not_dispatch(self):
        context = context_for(task_detail(True))
        context.get_node_data.return_value = None
        self.assertFalse(self.action.run(context, self.argv).success)
        context.run_task.assert_not_called()

    def test_disabled_idle_does_not_spin(self):
        context = context_for(task_detail(True))
        context.get_node_data.return_value = {"enabled": False}
        self.assertFalse(self.action.run(context, self.argv).success)
        context.run_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
