"""Regression tests for nested-task failures without action package registration."""

import importlib.util
import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from maa.agent.agent_server import AgentServer
from maa.define import MaaStatusEnum, Rect, Status, TaskDetail

_AGENT = Path(__file__).resolve().parents[1] / "agent"


def _load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, _AGENT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _package(name, relative_path):
    module = ModuleType(name)
    module.__path__ = [str(_AGENT / relative_path)]
    return module


def _task_detail(status=MaaStatusEnum.succeeded):
    return TaskDetail(1, "nested", [], Status(status))


def _context():
    controller = Mock()
    controller.post_screencap.return_value.wait.return_value.get.return_value = object()
    controller.post_screencap.return_value.wait.return_value.succeeded = True
    return SimpleNamespace(
        tasker=SimpleNamespace(stopping=False, controller=controller),
        run_task=Mock(return_value=_task_detail()),
        run_recognition=Mock(return_value=None),
        run_recognition_direct=Mock(return_value=None),
        get_node_data=Mock(return_value=None),
        set_anchor=Mock(),
    )


class _ActionTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.original_records = {"original": [2, -1]}
        self.records = dict(self.original_records)
        self.logger = Mock()
        logger_module = ModuleType("utils.logger")
        logger_module.logger = self.logger
        focus_module = ModuleType("utils.maafocus")
        self.print_t = focus_module.PrintT = Mock()
        manager_name = "custom.action.Common.CharacterAbility_CityAbility"
        manager_module = ModuleType(manager_name)
        self.clear_all = manager_module.clear_all = Mock(side_effect=self.records.clear)
        self.save = manager_module.set_character_abilities = Mock(
            side_effect=lambda name, levels: self.records.__setitem__(
                name, list(levels)
            )
        )
        modules = {
            "utils": _package("utils", "utils"),
            "utils.logger": logger_module,
            "utils.maafocus": focus_module,
            "custom": _package("custom", "custom"),
            "custom.action": _package("custom.action", "custom/action"),
            "custom.action.Common": _package(
                "custom.action.Common", "custom/action/Common"
            ),
            manager_name: manager_module,
        }
        stack.enter_context(patch.dict(sys.modules, modules))
        stack.enter_context(
            patch.object(
                AgentServer, "custom_action", side_effect=lambda name: lambda cls: cls
            )
        )
        self.control = _load_module("utils.task_control", "utils/task_control.py")
        self.city = _load_module(
            "_failure_test_city", "custom/action/SyncCharacterAbilityCityAbility.py"
        )
        self.furniture = _load_module(
            "_failure_test_furniture", "custom/action/Furniture/furniture_claim.py"
        )
        self.context = _context()
        self.argv = SimpleNamespace(
            custom_action_param=json.dumps({"fresh_record": True})
        )
        self.city_action = self.city.SyncCharacterAbilityCityAbilityMainAction()
        self.furniture_action = self.furniture.FurnitureClaim()
        self.original_ocr_name = self.city._ocr_name_with_retry
        self.original_tm_name = self.city._tm_get_name
        self.name_ocr = stack.enter_context(
            patch.object(self.city, "_ocr_name_with_retry", return_value="new")
        )
        self.tm_name = stack.enter_context(
            patch.object(self.city, "_tm_get_name", return_value=None)
        )
        self.skill_ocr = stack.enter_context(
            patch.object(self.city, "_ocr_skills", return_value=[4, 1])
        )
        self.wait = stack.enter_context(
            patch.object(self.city, "interruptible_wait", return_value=True)
        )
        stack.enter_context(patch.object(self.city, "_MAX_NO_CHANGE", 1))
        stack.enter_context(patch.object(self.city, "_MAX_ITERATIONS", 6))

    def assert_original_records(self):
        self.assertEqual(self.records, self.original_records)
        self.clear_all.assert_not_called()
        self.save.assert_not_called()
        self.assertNotIn(
            "SyncCityAbility: 遍历完成，共 %d 轮",
            [call.args[0] for call in self.logger.info.call_args_list],
        )

    def city_navigation(self):
        city = self.city
        entries = [
            city._NODE_OPEN_INFO,
            city._NODE_OPEN_ABILITY,
            city._NODE_OPEN_INFO,
            city._NODE_NEXT,
            city._NODE_OPEN_ABILITY,
            city._NODE_OPEN_INFO,
        ]
        for click_node in city._CLICK_NODES:
            entries.extend([click_node, city._NODE_OPEN_ABILITY, city._NODE_OPEN_INFO])
        return entries

    def prepare_furniture(self, box=None, claim_result=None):
        if box is None:
            box = [100, 200, 50, 60]
        if claim_result is None:
            claim_result = SimpleNamespace(hit=True)
        self.context.run_recognition.side_effect = [
            SimpleNamespace(hit=True, box=box),
            claim_result,
            None,
            None,
            None,
        ]


class CityAbilityFailureTests(_ActionTest):
    def test_each_failed_navigation_aborts_and_preserves_fresh_records(self):
        entries = self.city_navigation()
        for failure_index in range(len(entries)):
            for result in (None, _task_detail(MaaStatusEnum.failed)):
                with self.subTest(
                    entry=entries[failure_index], index=failure_index, result=result
                ):
                    context = _context()

                    def run_task(entry, override):
                        if context.run_task.call_count - 1 == failure_index:
                            return result
                        return _task_detail()

                    context.run_task.side_effect = run_task
                    self.assertFalse(self.city_action.run(context, self.argv).success)
                    self.assertEqual(
                        [call.args[0] for call in context.run_task.call_args_list],
                        entries[: failure_index + 1],
                    )
                    self.assert_original_records()

    def test_stop_during_each_navigation_aborts_and_preserves_fresh_records(self):
        entries = self.city_navigation()
        for stop_index in range(len(entries)):
            with self.subTest(entry=entries[stop_index], index=stop_index):
                context = _context()

                def run_task(entry, override):
                    if context.run_task.call_count - 1 == stop_index:
                        context.tasker.stopping = True
                    return _task_detail()

                context.run_task.side_effect = run_task
                self.assertFalse(self.city_action.run(context, self.argv).success)
                self.assertEqual(
                    [call.args[0] for call in context.run_task.call_args_list],
                    entries[: stop_index + 1],
                )
                self.assert_original_records()

    def test_stop_before_start_does_not_change_anchor_or_navigate(self):
        self.context.tasker.stopping = True
        self.assertFalse(self.city_action.run(self.context, self.argv).success)
        self.context.set_anchor.assert_not_called()
        self.context.run_task.assert_not_called()
        self.assert_original_records()

    def test_iteration_limit_does_not_commit_partial_records(self):
        for maximum in (0, 1):
            with self.subTest(maximum=maximum):
                with patch.object(self.city, "_MAX_ITERATIONS", maximum):
                    self.assertFalse(
                        self.city_action.run(self.context, self.argv).success
                    )
                self.assert_original_records()

    def test_name_recognition_miss_continues_scan_without_committing(self):
        self.name_ocr.return_value = None
        self.assertFalse(self.city_action.run(self.context, self.argv).success)
        entries = [call.args[0] for call in self.context.run_task.call_args_list]
        for click_node in self.city._CLICK_NODES:
            self.assertIn(click_node, entries)
        self.skill_ocr.assert_not_called()
        self.assert_original_records()

    def test_tail_recognition_miss_preserves_previous_records(self):
        self.name_ocr.side_effect = ["new", "new", None, "new", "new", "new"]
        self.assertFalse(self.city_action.run(self.context, self.argv).success)
        self.assert_original_records()

    def test_required_skill_miss_does_not_commit_partial_fresh_records(self):
        for missing_index in (0, 5):
            with self.subTest(missing_index=missing_index):
                self.context = _context()
                levels = [[4, 1] for _ in range(6)]
                levels[missing_index] = [-1, 1]
                self.skill_ocr.side_effect = levels
                self.assertFalse(self.city_action.run(self.context, self.argv).success)
                self.assert_original_records()

    def test_required_skill_miss_is_skipped_in_incremental_mode(self):
        self.skill_ocr.return_value = [-1, 1]
        self.argv.custom_action_param = "{}"
        self.assertFalse(self.city_action.run(self.context, self.argv).success)
        self.assert_original_records()

    def test_absent_optional_skill_can_be_committed(self):
        self.skill_ocr.return_value = [4, -1]
        self.assertTrue(self.city_action.run(self.context, self.argv).success)
        self.assertEqual(self.records, {"new": [4, -1]})
        self.clear_all.assert_called_once_with()

    def test_navigation_failure_is_distinct_from_recognition_miss(self):
        self.name_ocr.return_value = None
        self.assertIsNone(self.city._get_character_name(self.context))
        self.context.run_task.return_value = _task_detail(MaaStatusEnum.failed)
        self.tm_name.reset_mock()
        with self.assertRaises(self.control.TaskFailedError):
            self.city._get_character_name(self.context)
        self.tm_name.assert_not_called()
        self.skill_ocr.assert_not_called()

    def test_cancelled_ocr_wait_does_not_retry_or_navigate(self):
        def cancel(context, seconds):
            context.tasker.stopping = True
            return False

        self.wait.side_effect = cancel
        with self.assertRaises(self.control.TaskCancelledError):
            self.original_ocr_name(self.context)
        self.assertEqual(self.context.run_recognition.call_count, 1)
        self.context.run_task.assert_not_called()
        self.wait.assert_called_once_with(self.context, 0.2)
        self.assert_original_records()

    def test_cancelled_tail_wait_preserves_previous_records(self):
        def cancel(context, seconds):
            context.tasker.stopping = True
            return False

        self.wait.side_effect = cancel
        self.assertFalse(self.city_action.run(self.context, self.argv).success)
        self.assertEqual(
            self.context.run_task.call_args.args[0], self.city._CLICK_NODES[0]
        )
        self.wait.assert_called_once_with(self.context, 0.3)
        self.assert_original_records()

    def test_stop_during_ocr_does_not_navigate_to_ability_page(self):
        def recognize(*args, **kwargs):
            self.context.tasker.stopping = True
            return None

        self.context.run_recognition.side_effect = recognize
        with patch.object(self.city, "_ocr_name_with_retry", self.original_ocr_name):
            self.assertFalse(self.city_action.run(self.context, self.argv).success)
        self.assertEqual(self.context.run_recognition.call_count, 1)
        self.assertEqual(
            [call.args[0] for call in self.context.run_task.call_args_list],
            [self.city._NODE_OPEN_INFO],
        )
        self.wait.assert_not_called()
        self.assert_original_records()

    def test_stop_during_template_matching_does_not_match_again(self):
        def recognize(*args, **kwargs):
            self.context.tasker.stopping = True
            return None

        self.context.get_node_data.return_value = {
            "recognition": {"param": {"template": ["first.png", "second.png"]}}
        }
        self.context.run_recognition_direct.side_effect = recognize
        with patch.object(self.city, "_tm_get_name", self.original_tm_name):
            self.assertFalse(self.city_action.run(self.context, self.argv).success)
        self.assertEqual(self.context.run_recognition_direct.call_count, 1)
        self.skill_ocr.assert_not_called()
        self.assert_original_records()

    def test_stop_during_skill_ocr_does_not_save_incremental_record(self):
        def recognize(context):
            context.tasker.stopping = True
            return [4, 1]

        self.skill_ocr.side_effect = recognize
        self.argv.custom_action_param = "{}"
        self.assertFalse(self.city_action.run(self.context, self.argv).success)
        self.assert_original_records()

    def test_complete_scan_replaces_fresh_records_once(self):
        self.assertTrue(self.city_action.run(self.context, self.argv).success)
        self.assertEqual(self.records, {"new": [4, 1]})
        self.clear_all.assert_called_once_with()
        self.save.assert_called_once_with("new", [4, 1])
        self.assertEqual(self.wait.call_count, len(self.city._CLICK_NODES))
        self.assertEqual(
            [call.args[0] for call in self.context.run_task.call_args_list],
            self.city_navigation(),
        )

    def test_complete_incremental_scan_preserves_unseen_records(self):
        self.argv.custom_action_param = "{}"
        self.assertTrue(self.city_action.run(self.context, self.argv).success)
        self.assertEqual(self.records, {**self.original_records, "new": [4, 1]})
        self.clear_all.assert_not_called()

    def test_failed_or_empty_screenshot_cannot_be_recognized(self):
        self.city._ocr_name_with_retry = self.original_ocr_name
        for succeeded, image in ((False, object()), (True, None)):
            with self.subTest(succeeded=succeeded):
                self.context = _context()
                job = (
                    self.context.tasker.controller.post_screencap.return_value.wait.return_value
                )
                job.succeeded = succeeded
                job.get.return_value = image
                self.assertFalse(self.city_action.run(self.context, self.argv).success)
                self.context.run_recognition.assert_not_called()
                self.assert_original_records()


class FurnitureFailureTests(_ActionTest):
    def test_unsuccessful_task_details_do_not_report_claimed_or_continue(self):
        for detail in (
            None,
            _task_detail(MaaStatusEnum.invalid),
            _task_detail(MaaStatusEnum.pending),
            _task_detail(MaaStatusEnum.running),
            _task_detail(MaaStatusEnum.failed),
        ):
            with self.subTest(detail=detail):
                self.context = _context()
                self.prepare_furniture()
                self.context.run_task.return_value = detail
                self.assertFalse(self.furniture_action.run(self.context, None).success)
                self.assertEqual(self.context.run_recognition.call_count, 2)
                self.context.tasker.controller.post_screencap.assert_called_once_with()
                self.print_t.assert_not_called()

    def test_box_lists_tuples_and_rects_are_supported(self):
        for box in ([100, 200, 50, 60], (100, 200, 50, 60), Rect(100, 200, 50, 60)):
            with self.subTest(box=box):
                self.context = _context()
                self.print_t.reset_mock()
                self.prepare_furniture(box)
                self.assertTrue(self.furniture_action.run(self.context, None).success)
                override = {"FurnitureClaim": {"roi": [100, 200, 50, 60]}}
                self.context.run_task.assert_called_once_with(
                    "FurnitureClaim", override
                )
                self.assertEqual(
                    self.context.run_recognition.call_args_list[1].kwargs[
                        "pipeline_override"
                    ],
                    override,
                )
                self.print_t.assert_called_once_with(
                    self.context, "furniture.claimed.hamster_ball"
                )

    def test_missing_furniture_or_unhit_box_is_not_claimed(self):
        for detail in (
            None,
            SimpleNamespace(hit=False, box=[100, 200, 50, 60]),
            SimpleNamespace(hit=True, box=None),
            SimpleNamespace(hit=True, box=[]),
        ):
            with self.subTest(detail=detail):
                self.context = _context()
                self.context.run_recognition.return_value = detail
                self.assertTrue(self.furniture_action.run(self.context, None).success)
                self.assertEqual(self.context.run_recognition.call_count, 4)
                self.context.run_task.assert_not_called()
                self.print_t.assert_not_called()

    def test_none_or_missed_claim_recognition_is_skipped(self):
        for detail in (None, SimpleNamespace(hit=False)):
            with self.subTest(detail=detail):
                self.context = _context()
                self.prepare_furniture()
                self.context.run_recognition.side_effect = [
                    SimpleNamespace(hit=True, box=[100, 200, 50, 60]),
                    detail,
                    None,
                    None,
                    None,
                ]
                self.assertTrue(self.furniture_action.run(self.context, None).success)
                self.context.run_task.assert_not_called()
                self.print_t.assert_not_called()

    def test_stop_before_start_does_not_capture_or_claim(self):
        self.context.tasker.stopping = True
        self.assertFalse(self.furniture_action.run(self.context, None).success)
        self.context.tasker.controller.post_screencap.assert_not_called()
        self.context.run_recognition.assert_not_called()
        self.context.run_task.assert_not_called()
        self.print_t.assert_not_called()

    def test_stop_during_capture_does_not_recognize_or_claim(self):
        def capture():
            self.context.tasker.stopping = True
            return object()

        job = (
            self.context.tasker.controller.post_screencap.return_value.wait.return_value
        )
        job.get.side_effect = capture
        self.assertFalse(self.furniture_action.run(self.context, None).success)
        self.context.run_recognition.assert_not_called()
        self.context.run_task.assert_not_called()
        self.print_t.assert_not_called()

    def test_stop_during_either_recognition_does_not_claim(self):
        for stop_at in (1, 2):
            with self.subTest(stop_at=stop_at):
                self.context = _context()

                def recognize(*args, **kwargs):
                    if self.context.run_recognition.call_count == stop_at:
                        self.context.tasker.stopping = True
                    return SimpleNamespace(hit=True, box=[100, 200, 50, 60])

                self.context.run_recognition.side_effect = recognize
                self.assertFalse(self.furniture_action.run(self.context, None).success)
                self.assertEqual(self.context.run_recognition.call_count, stop_at)
                self.context.run_task.assert_not_called()
                self.print_t.assert_not_called()

    def test_stop_during_successful_claim_does_not_report_claimed(self):
        self.prepare_furniture()

        def claim(*args, **kwargs):
            self.context.tasker.stopping = True
            return _task_detail()

        self.context.run_task.side_effect = claim
        self.assertFalse(self.furniture_action.run(self.context, None).success)
        self.assertEqual(self.context.run_recognition.call_count, 2)
        self.context.tasker.controller.post_screencap.assert_called_once_with()
        self.print_t.assert_not_called()

    def test_failed_or_empty_screenshot_cannot_report_success(self):
        for succeeded, image in ((False, object()), (True, None)):
            with self.subTest(succeeded=succeeded):
                self.context = _context()
                job = (
                    self.context.tasker.controller.post_screencap.return_value.wait.return_value
                )
                job.succeeded = succeeded
                job.get.return_value = image
                self.assertFalse(self.furniture_action.run(self.context, None).success)
                self.context.run_recognition.assert_not_called()
                self.context.run_task.assert_not_called()
                self.print_t.assert_not_called()


if __name__ == "__main__":
    unittest.main()
