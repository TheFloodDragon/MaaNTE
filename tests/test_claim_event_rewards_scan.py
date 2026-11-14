"""合成文字帧与 OCR mocks 的侧栏完整性回归；不连接控制器，不代表游戏实测。"""

import copy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load():
    modules = {
        name: ModuleType(name)
        for name in (
            "maa",
            "maa.agent",
            "maa.agent.agent_server",
            "maa.context",
            "maa.custom_action",
            "utils",
            "utils.logger",
        )
    }
    for module in modules.values():
        module.__path__ = []

    class CustomAction:
        RunArg = object

        class RunResult:
            def __init__(self, success):
                self.success = success

    registered = {}

    def register(name):
        def decorator(cls):
            registered[name] = cls
            return cls

        return decorator

    modules["maa.custom_action"].CustomAction = CustomAction
    modules["maa.context"].Context = object
    modules["maa.agent.agent_server"].AgentServer = SimpleNamespace(
        custom_action=register
    )
    modules["utils.logger"].logger = Mock()
    spec = importlib.util.spec_from_file_location(
        "_claim_event_rewards_scan_unit",
        ROOT / "agent/custom/action/claim_event_rewards_scan.py",
    )
    module = importlib.util.module_from_spec(spec)
    modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module, registered


scan, REGISTERED = _load()
ARG = SimpleNamespace(node_name=scan.SCAN_NODE, custom_action_param="")
# 当前真实 report.json 的七项 OCR 原文；末项 0.770037，不把它改写为可靠名单。
LIVE_NAMES = (
    "环期赠礼",
    "「开市预演」",
    "集市分福",
    "盲盒「所行皆坦途",
    "像素溢出",
    "争锋赏宴·攻坚",
    "金坦荡",
)


def _row(text, *, y=180, score=0.99, box=None):
    return SimpleNamespace(
        text=text, score=score, box=box or [100, y, len(text) * 8, 16]
    )


def _detail(rows=(), *, hit=None):
    rows = list(rows)
    filtered = [row for row in rows if 0.8 <= row.score <= 1]
    return SimpleNamespace(
        hit=bool(filtered) if hit is None else hit,
        all_results=rows,
        filtered_results=filtered,
    )


def _page(names=("集市分福", "像素溢出"), *, scores=None):
    scores = scores or {}
    rows = [
        _row(text, y=178 + index * 64, score=scores.get(text, 0.99))
        for index, text in enumerate(names)
    ]
    frame = np.zeros((720, 1280, 3), np.uint8)
    # 小型伪字形只用于图像差异回归；不是 OCR 识别输入验收。
    for row in rows:
        x, y, _, _ = row.box
        for index, char in enumerate(row.text):
            left = x + index * 8
            frame[y : y + 13, left : left + 2] = 230
            offset = ord(char) % 9
            frame[y + offset : y + offset + 3, left : left + 6] = 230
    return {
        "image": frame,
        "InEventsMenu": _detail([_row("活动", box=[74, 26, 47, 26])]),
        scan.HEADER_NODE: _detail([_row("限时", box=[68, 119, 44, 27])]),
        scan.TARGET_NODE: _detail(
            [row for row in rows if row.text == scan.TARGET_TEXT]
        ),
        scan.NAMES_NODE: _detail(rows),
    }


def _task(node):
    return SimpleNamespace(
        entry=node,
        status=SimpleNamespace(succeeded=True),
        nodes=[
            SimpleNamespace(
                name=node, completed=True, action=SimpleNamespace(success=True)
            )
        ],
    )


class _Controller:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.current = None
        self.cached_image = None
        self.captures = 0
        self.failed_captures = set()
        self.wait_hook = lambda: None

    def post_screencap(self):
        self.captures += 1
        succeeded = self.captures not in self.failed_captures
        job = SimpleNamespace(succeeded=succeeded)

        def wait():
            if succeeded:
                self.current = next(self.pages)
                self.cached_image = self.current["image"]
            self.wait_hook()
            return job

        job.wait = wait
        return job


class _Context:
    def __init__(self, pages):
        self.tasker = SimpleNamespace(stopping=False, controller=_Controller(pages))
        self.next = {}
        self.override_success = True
        self.override_next = Mock(side_effect=self._override)
        self.run_task = Mock(side_effect=_task)
        self.run_recognition = Mock(side_effect=self._recognition)

    def _override(self, node, next_nodes):
        if self.override_success:
            self.next[node] = list(next_nodes)
        return self.override_success

    def _recognition(self, node, frame):
        if frame is not self.tasker.controller.cached_image:
            raise AssertionError("recognition must use the fresh cached frame")
        return self.tasker.controller.current[node]


class ClaimEventRewardsScanTests(unittest.TestCase):
    def run_scan(self, context, param="", action=None):
        return (
            (action or scan.ClaimEventRewardsScan())
            .run(
                context,
                SimpleNamespace(node_name=scan.SCAN_NODE, custom_action_param=param),
            )
            .success
        )

    def assert_not_absent(self, context):
        self.assertNotIn(
            call(scan.SCAN_NODE, [scan.NO_EVENT_NODE]),
            context.override_next.call_args_list,
        )

    def test_registers_exact_action_name(self):
        self.assertIs(
            REGISTERED["claim_event_rewards_scan"], scan.ClaimEventRewardsScan
        )

    def test_current_target_among_all_seven_live_ocr_names_needs_no_swipe(self):
        context = _Context([_page(LIVE_NAMES, scores={"金坦荡": 0.770037})])
        self.assertTrue(self.run_scan(context))
        context.run_task.assert_not_called()
        self.assertEqual(context.tasker.controller.captures, 1)
        self.assertEqual(context.next[scan.SCAN_NODE], [scan.EVENT_TAB_NODE])

    def test_target_found_while_locating_top(self):
        context = _Context([_page(), _page(LIVE_NAMES)])
        self.assertTrue(self.run_scan(context))
        self.assertEqual(context.run_task.call_args_list, [call(scan.UP_NODE)])
        self.assert_not_absent(context)

    def test_target_found_during_downward_scan(self):
        top = _page()
        context = _Context([top, copy.deepcopy(top), _page(LIVE_NAMES)])
        self.assertTrue(self.run_scan(context))
        self.assertEqual(
            context.run_task.call_args_list, [call(scan.UP_NODE), call(scan.DOWN_NODE)]
        )
        self.assert_not_absent(context)

    def test_one_page_absence_needs_fresh_top_and_bottom_boundary_frames(self):
        page = _page()
        context = _Context([page, copy.deepcopy(page), copy.deepcopy(page)])
        self.assertTrue(self.run_scan(context, "{}"))
        self.assertEqual(context.tasker.controller.captures, 3)
        self.assertEqual(
            context.run_task.call_args_list, [call(scan.UP_NODE), call(scan.DOWN_NODE)]
        )
        self.assertEqual(context.next[scan.SCAN_NODE], [scan.NO_EVENT_NODE])

    def test_multiple_pages_absent_only_after_top_to_bottom_overlap_scan(self):
        top = _page(["其他甲", "其他乙", "其他丙"])
        middle = _page(["其他乙", "其他丙", "其他丁"])
        bottom = _page(["其他丙", "其他丁", "其他戊"])
        context = _Context(
            [middle, top, copy.deepcopy(top), middle, bottom, copy.deepcopy(bottom)]
        )
        self.assertTrue(self.run_scan(context))
        self.assertEqual(
            context.run_task.call_args_list,
            [call(scan.UP_NODE)] * 2 + [call(scan.DOWN_NODE)] * 3,
        )
        self.assertEqual(context.next[scan.SCAN_NODE], [scan.NO_EVENT_NODE])

    def test_same_ocr_but_changed_text_image_is_not_a_boundary_or_retry(self):
        original = _page()
        changed = copy.deepcopy(original)
        changed["image"][178:194, 110:116] = 255
        context = _Context([original, changed])
        self.assertFalse(self.run_scan(context))
        self.assertEqual(context.run_task.call_count, 1)
        self.assert_not_absent(context)

    def test_red_new_border_background_and_right_panel_animations_are_ignored(self):
        original = _page()
        animated = copy.deepcopy(original)
        image = animated["image"]
        image[157:617, 188:201] = [90, 50, 240]  # 右侧边框
        image[159:174, 30:70] = [0, 255, 255]  # 左侧 NEW
        image[200:225, 95:112] = [0, 0, 255]  # 红点：没有覆盖伪字形
        image[200:240, 130:180] = [45, 45, 45]  # 暗高亮背景
        image[100:650, 205:1250] = 255  # 页面动画、倒计时
        context = _Context([original, animated, copy.deepcopy(animated)])
        self.assertTrue(self.run_scan(context))
        self.assertEqual(context.next[scan.SCAN_NODE], [scan.NO_EVENT_NODE])

    def test_global_swipe_limit_is_not_reset_after_top_boundary(self):
        top_names = ["其他%02d" % index for index in range(3)]
        top = _page(top_names)
        pages = [top, copy.deepcopy(top)]
        pages += [
            _page(["其他%02d" % index for index in range(start, start + 3)])
            for start in range(1, scan.MAX_SWIPES)
        ]
        context = _Context(pages)
        self.assertFalse(self.run_scan(context))
        self.assertEqual(context.run_task.call_count, scan.MAX_SWIPES)
        self.assertEqual(context.tasker.controller.captures, scan.MAX_SWIPES + 1)
        self.assert_not_absent(context)

    def test_top_not_proven_at_page_limit_fails(self):
        pages = [
            _page(["其他%02d" % index for index in range(start, start + 3)])
            for start in range(scan.MAX_SWIPES, -1, -1)
        ]
        context = _Context(pages)
        self.assertFalse(self.run_scan(context))
        self.assertEqual(
            context.run_task.call_args_list, [call(scan.UP_NODE)] * scan.MAX_SWIPES
        )
        self.assert_not_absent(context)

    def test_repeated_viewport_cycle_fails(self):
        first = _page(["其他甲", "其他乙"])
        pages = [
            first,
            _page(["其他丙", "其他甲"]),
            _page(["其他乙", "其他丙"]),
            copy.deepcopy(first),
        ]
        context = _Context(pages)
        self.assertFalse(self.run_scan(context))
        self.assertEqual(context.run_task.call_count, 3)
        self.assert_not_absent(context)

    def test_no_overlap_or_wrong_direction_cannot_prove_coverage(self):
        for names in (["其他丙", "其他丁"], ["像素溢出", "其他甲"]):
            with self.subTest(names=names):
                context = _Context([_page(), _page(names)])
                self.assertFalse(self.run_scan(context))
                self.assert_not_absent(context)

    def test_action_failures_or_extra_chain_nodes_fail_closed(self):
        cases = [None]
        failed_status = _task(scan.UP_NODE)
        failed_status.status.succeeded = False
        cases.append(failed_status)
        for field, value in (
            ("completed", False),
            ("name", "WrongNode"),
            ("action", None),
        ):
            detail = _task(scan.UP_NODE)
            setattr(detail.nodes[0], field, value)
            cases.append(detail)
        failed_action = _task(scan.UP_NODE)
        failed_action.nodes[0].action.success = False
        cases.append(failed_action)
        empty = _task(scan.UP_NODE)
        empty.nodes = []
        cases.append(empty)
        extra = _task(scan.UP_NODE)
        extra.nodes += _task(scan.UP_NODE).nodes
        cases.append(extra)
        for detail in cases:
            with self.subTest(detail=detail):
                context = _Context([_page()])
                context.run_task.side_effect = None
                context.run_task.return_value = detail
                self.assertFalse(self.run_scan(context))
                self.assertEqual(context.tasker.controller.captures, 1)
                self.assert_not_absent(context)

    def test_stopping_before_capture_and_during_swipe_prevents_success(self):
        context = _Context([_page(LIVE_NAMES)])
        context.tasker.stopping = True
        self.assertFalse(self.run_scan(context))
        self.assertEqual(context.tasker.controller.captures, 0)
        self.assert_not_absent(context)
        context = _Context([_page(), _page(LIVE_NAMES)])

        def stop(node):
            context.tasker.stopping = True
            return _task(node)

        context.run_task.side_effect = stop
        self.assertFalse(self.run_scan(context))
        self.assertEqual(context.tasker.controller.captures, 1)
        self.assert_not_absent(context)

    def test_stop_during_fresh_capture_prevents_recognition(self):
        context = _Context([_page(LIVE_NAMES)])
        context.tasker.controller.wait_hook = lambda: setattr(
            context.tasker, "stopping", True
        )
        self.assertFalse(self.run_scan(context))
        context.run_recognition.assert_not_called()

    def test_wrong_shape_channels_dtype_or_missing_frame_fail(self):
        frames = [
            None,
            np.zeros((1080, 1920, 3), np.uint8),
            np.zeros((720, 1280, 4), np.uint8),
            np.zeros((720, 1280, 3), float),
        ]
        for image in frames:
            with self.subTest(shape=getattr(image, "shape", None)):
                page = _page(LIVE_NAMES)
                page["image"] = image
                context = _Context([page])
                self.assertFalse(self.run_scan(context))
                context.run_recognition.assert_not_called()
                context.run_task.assert_not_called()

    def test_failed_fresh_capture_does_not_reuse_cached_frame(self):
        page = _page()
        context = _Context([page])
        context.tasker.controller.failed_captures.add(2)
        self.assertFalse(self.run_scan(context))
        self.assertEqual(context.run_recognition.call_count, 4)
        self.assert_not_absent(context)

    def test_unknown_menu_header_target_and_names_fail_without_swipe(self):
        cases = [
            ("InEventsMenu", _detail(hit=False)),
            (scan.HEADER_NODE, _detail([_row("常驻", box=[68, 119, 44, 27])])),
            (scan.TARGET_NODE, None),
            (scan.NAMES_NODE, None),
            (scan.NAMES_NODE, _detail(hit=False)),
        ]
        for node, detail in cases:
            with self.subTest(node=node):
                page = _page()
                page[node] = detail
                context = _Context([page])
                self.assertFalse(self.run_scan(context))
                context.run_task.assert_not_called()
                self.assert_not_absent(context)

    def test_low_confidence_and_similar_target_raw_rows_are_not_hidden_by_filter(self):
        cases = [
            ("环期", 0.99),
            ("赠礼", 0.99),
            ("环期赠札", 0.99),
            ("杯期赠礼", 0.99),
            ("环期赠礼", 0.79),
            ("其他低可信", 0.5),
            ("其他甲", float("nan")),
            ("其他甲", float("inf")),
        ]
        for text, score in cases:
            with self.subTest(text=text, score=score):
                context = _Context([_page(["集市分福", text], scores={text: score})])
                self.assertFalse(self.run_scan(context))
                context.run_task.assert_not_called()
                self.assert_not_absent(context)

    def test_clipped_duplicate_names_and_blank_text_image_fail(self):
        clipped = _page()
        clipped[scan.NAMES_NODE].all_results[0].box = [75, 180, 48, 16]
        blank = _page()
        blank["image"][:] = 0
        for page in (clipped, _page(["其他甲", "其他甲"]), blank):
            with self.subTest(page=page[scan.NAMES_NODE].all_results):
                context = _Context([page])
                self.assertFalse(self.run_scan(context))
                self.assert_not_absent(context)

    def test_changed_ocr_without_image_change_fails(self):
        original = _page(["其他甲", "其他乙"])
        changed = _page(["其他丙", "其他甲"])
        changed["image"] = original["image"].copy()
        context = _Context([original, changed])
        self.assertFalse(self.run_scan(context))
        self.assert_not_absent(context)

    def test_empty_parameters_only_and_default_next_reset_even_on_bad_param(self):
        for param in (
            '{"max_pages": 999999}',
            "[]",
            "null",
            "false",
            "1",
            "broken",
            [],
            {},
            0,
            False,
        ):
            with self.subTest(param=param):
                context = _Context([])
                context.next[scan.SCAN_NODE] = [scan.NO_EVENT_NODE]
                self.assertFalse(self.run_scan(context, param))
                self.assertEqual(context.next[scan.SCAN_NODE], [scan.EVENT_TAB_NODE])
                self.assertEqual(context.tasker.controller.captures, 0)
                context.run_task.assert_not_called()

    def test_override_and_recognition_exceptions_fail_closed(self):
        context = _Context([_page()])
        context.override_success = False
        self.assertFalse(self.run_scan(context))
        self.assertEqual(context.tasker.controller.captures, 0)
        context = _Context([_page()])
        context.run_recognition.side_effect = RuntimeError("OCR unavailable")
        self.assertFalse(self.run_scan(context))
        context.run_task.assert_not_called()
        self.assert_not_absent(context)

    def test_final_absence_override_failure_is_not_reported_success(self):
        page = _page()
        context = _Context([page, copy.deepcopy(page), copy.deepcopy(page)])
        context.override_next.side_effect = [True, False]
        self.assertFalse(self.run_scan(context))
        self.assertEqual(
            context.override_next.call_args_list,
            [
                call(scan.SCAN_NODE, [scan.EVENT_TAB_NODE]),
                call(scan.SCAN_NODE, [scan.NO_EVENT_NODE]),
            ],
        )

    def test_same_action_and_context_do_not_keep_absent_result_across_runs(self):
        page = _page()
        context = _Context([page, copy.deepcopy(page), copy.deepcopy(page)])
        action = scan.ClaimEventRewardsScan()
        self.assertTrue(self.run_scan(context, action=action))
        self.assertEqual(context.next[scan.SCAN_NODE], [scan.NO_EVENT_NODE])
        context.tasker.controller.pages = iter([_page(LIVE_NAMES)])
        self.assertTrue(self.run_scan(context, action=action))
        self.assertEqual(context.next[scan.SCAN_NODE], [scan.EVENT_TAB_NODE])
        self.assertEqual(
            context.override_next.call_args_list[-1],
            call(scan.SCAN_NODE, [scan.EVENT_TAB_NODE]),
        )
        self.assertEqual(context.run_task.call_count, 2)

    def test_registers_fail_action_and_returns_false(self):
        self.assertIn("claim_event_rewards_fail", REGISTERED)
        action = REGISTERED["claim_event_rewards_fail"]()
        self.assertIs(action.run(SimpleNamespace(), SimpleNamespace()).success, False)


if __name__ == "__main__":
    unittest.main()
