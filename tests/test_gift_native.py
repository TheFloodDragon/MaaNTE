"""真实 Maa Resource/Tasker 运行赠礼图；视觉替身仅验证流程，非真实 OCR 验收。"""

import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

import maa
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.define import LoggingLevelEnum
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

from test_gift_actions import (
    A,
    B,
    C,
    C1,
    C2,
    ACTIONS,
    PIPELINE,
    ROOT,
    GiftWorld,
    MarkerVision,
    actions,
    make_config,
    patched_actions,
)


class VirtualGiftController(CustomController):
    def __init__(self, world):
        self.world = world
        super().__init__()

    def connect(self):
        return True

    def request_uuid(self):
        return "gift-offline-native-tests"

    def get_features(self):
        return 0

    def screencap(self):
        return self.world.screencap()

    def click(self, x, y):
        return self.world.click(x, y)

    def swipe(self, x1, y1, x2, y2, duration):
        return self.world.swipe(x1, y1, x2, y2, duration)


class SyntheticBondRecognition(CustomRecognition):
    def __init__(self, world):
        self.vision = MarkerVision(world)
        super().__init__()

    def analyze(self, context, argv):
        if self.vision.snapshot(argv.image).view.page in {
            "characters",
            "bond",
            "gifts",
        }:
            return self.AnalyzeResult(box=[10, 10, 20, 20], detail={})
        return None


class VirtualEnterBond(CustomAction):
    def __init__(self, world):
        self.world = world
        super().__init__()

    def run(self, context, argv):
        return self.RunResult(success=self.world.enter_bond())


class ObservedAction(CustomAction):
    """只记录真实动作入口/结果，并提供可控停止边界；返回值完全由生产动作决定。"""

    def __init__(self, cls, trace, results, hook):
        self.action = cls()
        self.trace = trace
        self.results = results
        self.hook = hook
        super().__init__()

    def run(self, context, argv):
        self.trace.append(argv.node_name)
        if self.hook is not None:
            self.hook(argv.node_name)
        result = self.action.run(context, argv)
        self.results.append((argv.node_name, result.success))
        return result


class NativeGiftFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 延后切库，其他测试导入时仍可隔离加载 AgentServer 装饰器。
        # 全局日志固定，只在测试类启动时设置，不放入待删除的临时资源包。
        Library.open(
            Path(
                os.environ.get("MAAFW_BINARY_PATH", Path(maa.__file__).parent / "bin")
            ),
            agent_server=False,
        )
        Tasker.set_log_dir(ROOT / ".narrafork")
        Tasker.set_stdout_level(LoggingLevelEnum.Off)
        Tasker.set_save_draw(False)
        Tasker.set_save_on_error(False)
        Tasker.set_debug_mode(False)

    def run_flow(self, world=None, config=None, *, before_node=None, stop_at=None):
        world = world or GiftWorld()
        data = copy.deepcopy(PIPELINE)
        data.update(make_config() if config is None else config)
        # 保留 Gift.json 的真实图和错误分支，只提供公开场景接口的合成实现。
        data["InBondMenu"] = {
            "recognition": {
                "type": "Custom",
                "param": {"custom_recognition": "test_bond_scene"},
            },
            "rate_limit": 0,
            "pre_delay": 0,
            "post_delay": 0,
        }
        data["SceneAnyEnterBondMenu"] = {
            "recognition": {"type": "DirectHit"},
            "action": {
                "type": "Custom",
                "param": {"custom_action": "test_enter_bond"},
            },
            "rate_limit": 0,
            "pre_delay": 0,
            "post_delay": 0,
        }
        reached = threading.Event()
        release = threading.Event()
        stop_wait_timed_out = []
        trace, results = [], []

        def hook(node):
            if before_node is not None:
                before_node(node, world)
            if node == stop_at:
                reached.set()
                if not release.wait(5):
                    stop_wait_timed_out.append(node)

        # 小资源包仅保存 JSON，不复制 OCR 模型/目录图像，也不写截图。
        with tempfile.TemporaryDirectory(prefix="maante-gift-native-") as temporary:
            bundle = Path(temporary)
            pipeline_dir = bundle / "pipeline"
            pipeline_dir.mkdir()
            (pipeline_dir / "gift.json").write_text(json.dumps(data), encoding="utf-8")
            controller = VirtualGiftController(world)
            resource = Resource()
            tasker = None
            job = None
            callbacks = {
                name: ObservedAction(cls, trace, results, hook)
                for name, cls in ACTIONS.items()
            }
            callbacks["test_enter_bond"] = VirtualEnterBond(world)
            recognizer = SyntheticBondRecognition(world)
            try:
                for name, callback in callbacks.items():
                    self.assertTrue(resource.register_custom_action(name, callback))
                self.assertTrue(
                    resource.register_custom_recognition("test_bond_scene", recognizer)
                )
                self.assertTrue(resource.post_bundle(bundle).wait().succeeded)
                self.assertTrue(controller.post_connection().wait().succeeded)
                tasker = Tasker()
                self.assertTrue(tasker.bind(resource, controller))
                with patched_actions(world) as messages:
                    try:
                        job = tasker.post_task("GiftEntry")
                        if stop_at is not None:
                            self.assertTrue(
                                reached.wait(5), "未到达停止边界: " + str(trace)
                            )
                            # 不在动作回调线程等待 StopJob，避免 Tasker/Controller 死锁。
                            stop_job = tasker.post_stop()
                            release.set()
                            job.wait()
                            stop_job.wait()
                        else:
                            job.wait()
                        detail = job.get()
                        runtime = actions._runtime
                        result = SimpleNamespace(
                            succeeded=job.succeeded,
                            done=job.done,
                            nodes=(
                                [node.name for node in detail.nodes] if detail else []
                            ),
                            trace=list(trace),
                            results=list(results),
                            states=tuple(runtime.session.states),
                            pending=runtime.session.pending,
                            attempts=runtime.session.attempts,
                            messages=list(messages),
                            world=world,
                        )
                    finally:
                        release.set()
                        # 包含已停止任务也必须等待真正完成，才允许恢复 Python 替身。
                        tasker.post_stop().wait()
                        if job is not None:
                            job.wait()
                    self.assertTrue(job.done)
                    self.assertFalse(tasker.running)
                    self.assertEqual(stop_wait_timed_out, [])
                return result
            finally:
                release.set()
                if tasker is not None:
                    tasker.post_stop().wait()
                    if job is not None:
                        job.wait()
                    tasker.clear_sinks()
                    tasker.clear_context_sinks()
                # 原生引用依次释放，不留 Tasker/控制器线程跨 suite。
                del job
                del tasker
                del resource
                del controller

    def summary(self, result):
        return [
            message[1:] for message in result.messages if message[0] == "gift.summary"
        ][-1]

    def test_normal_three_two_or_zero_remaining_use_real_graph(self):
        for remaining in (3, 2, 0):
            with self.subTest(remaining=remaining):
                result = self.run_flow(GiftWorld(remaining=remaining))
                self.assertTrue(result.succeeded)
                self.assertTrue(result.done)
                self.assertEqual(result.world.sends, [(C1, A)] * remaining)
                self.assertEqual(result.states[0].confirmed, remaining)
                self.assertIn("GiftSummary", result.nodes)
                self.assertFalse(any(not ok for _, ok in result.results))
                self.assertEqual(self.summary(result)[0], remaining)

    def test_public_scene_navigation_returns_to_real_bond_ready_guard(self):
        result = self.run_flow(GiftWorld(page="world"), make_config([(C1, (A,), 1)]))
        self.assertTrue(result.succeeded)
        self.assertEqual(result.world.navigation, 1)
        self.assertEqual(result.world.sends, [(C1, A)])
        self.assertIn("GiftBondReady", result.nodes)

    def test_wrong_identity_uses_on_error_then_continues_next_character(self):
        world = GiftWorld()
        world.character_redirect[C1] = C2
        result = self.run_flow(world, make_config([(C1, (A,), 1), (C2, (B,), 1)]))
        self.assertTrue(result.succeeded)
        self.assertIn(("GiftWaitCharacter", False), result.results)
        self.assertIn("GiftWarnCurrent", result.nodes)
        self.assertIn("GiftRecoverCurrent", result.nodes)
        self.assertEqual(world.sends, [(C2, B)])
        self.assertEqual([state.confirmed for state in result.states], [0, 1])
        self.assertEqual(self.summary(result), (1, 0, 0, 1, 0))

    def test_receipt_timeout_uses_real_on_error_and_never_resubmits(self):
        world = GiftWorld()
        world.receipt = "none"
        result = self.run_flow(world)
        self.assertTrue(result.succeeded)
        self.assertIn(("GiftWaitReceipt", False), result.results)
        self.assertIn("GiftReceiptFailed", result.nodes)
        self.assertIn("GiftWarnCurrent", result.nodes)
        self.assertEqual(world.sends, [(C1, A)])
        self.assertEqual(result.states[0].confirmed, 0)
        self.assertEqual(result.states[0].status, "uncertain")
        self.assertIsNone(result.pending)
        self.assertLess(world.captures, 200)
        self.assertEqual(self.summary(result), (0, 0, 0, 0, 1))

    def test_send_input_failure_uses_receipt_error_path_without_replay(self):
        world = GiftWorld()
        world.fail_inputs.add("send")
        result = self.run_flow(world)
        self.assertTrue(result.succeeded)
        self.assertIn(("GiftSendOne", False), result.results)
        self.assertIn("GiftReceiptFailed", result.nodes)
        self.assertEqual(world.sends, [(C1, A)])
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.states[0].status, "uncertain")
        self.assertIsNone(result.pending)

    def test_global_limit_before_send_blocks_all_remaining_characters(self):
        def before(node, world):
            if node == "GiftSendOne":
                world.view_overrides["global_limit"] = True

        result = self.run_flow(
            config=make_config([(C1, (A,), 3), (C2, (B,), 3)]), before_node=before
        )
        self.assertTrue(result.succeeded)
        self.assertIn(("GiftSendOne", False), result.results)
        self.assertIn("GiftReceiptFailed", result.nodes)
        self.assertEqual(result.world.sends, [])
        self.assertEqual([state.confirmed for state in result.states], [0, 0])
        self.assertEqual(self.summary(result), (0, 0, 0, 2, 0))
        self.assertEqual(
            sum(kind == "characters" for _, kind, _ in result.world.inputs), 1
        )

    def test_unknown_popup_before_send_stops_recovery_input_and_warns_others(self):
        def before(node, world):
            if node == "GiftSendOne":
                world.page = "unknown"

        result = self.run_flow(
            config=make_config([(C1, (A,), 3), (C2, (B,), 3)]), before_node=before
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(result.world.sends, [])
        self.assertIn("GiftRecoverCurrent", result.nodes)
        self.assertEqual(self.summary(result), (0, 0, 0, 2, 0))
        self.assertEqual(
            sum(kind == "characters" for _, kind, _ in result.world.inputs), 1
        )

    def test_multiframe_missing_preferred_gift_falls_back_through_scroll_nodes(self):
        world = GiftWorld(gift_pages=((B,), (C,)))
        result = self.run_flow(world)
        self.assertTrue(result.succeeded)
        self.assertEqual(world.sends, [(C1, B)] * 3)
        self.assertIn("GiftScrollList", result.nodes)
        self.assertIn("GiftWaitScroll", result.nodes)
        self.assertFalse(any(not ok for _, ok in result.results))

    def test_stop_before_send_or_during_receipt_finishes_native_task_and_threads(self):
        for boundary, expected in (("GiftSendOne", []), ("GiftWaitReceipt", [(C1, A)])):
            world = GiftWorld()
            world.receipt = "none"
            with self.subTest(boundary=boundary):
                result = self.run_flow(world, stop_at=boundary)
                self.assertTrue(result.done)
                # Maa 将主动停止记为任务正常结束，安全性由未输入/未确认断言判断。
                self.assertEqual(world.sends, expected)
                self.assertEqual(result.states[0].confirmed, 0)
                self.assertIsNone(result.pending)
                self.assertIn((boundary, False), result.results)


if __name__ == "__main__":
    unittest.main()
