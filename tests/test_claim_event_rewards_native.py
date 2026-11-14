"""真实 MaaFW + OCR/模板截图回归；所有控制器均为纯内存，无窗口或真实输入。

运行：python -B -m unittest discover -s tests -p test_claim_event_rewards_native.py -v
仅 DAY2 的三张脱敏实机图；FSM 隔离最终返世界，不代表世界导航或七天实机验收。
原生日志和简明报告仅保存在 .narrafork/claim-event-rewards/native/。
"""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import threading
import traceback
import unittest

SDK_ERROR = None
try:
    import cv2
    import numpy as np
    import maa
    from maa.controller import CustomController
    from maa.custom_action import CustomAction
    from maa.define import LoggingLevelEnum
    from maa.library import Library
    from maa.resource import Resource
    from maa.tasker import Tasker
except (ImportError, OSError, FileNotFoundError) as exc:
    SDK_ERROR = "%s: %s" % (type(exc).__name__, exc)
    CustomController = CustomAction = object

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / "assets/resource/base"
PIPELINE = RESOURCE / "pipeline/ClaimEventRewards.json"
FIXTURES = ROOT / "tests/fixtures/claim_event_rewards"
CACHE = ROOT / ".narrafork/claim-event-rewards/native"
PREFIX = "ClaimEventRewards"
PROBE_NODE = PREFIX + "NativeProbe"
PROBE_ACTION = "claim_event_rewards_native_probe"
DAY2_BUTTON = (827, 410, 116, 36)
DAY7_BUTTON = (1067, 574, 177, 57)
POPUP_CLOSE = (550, 616, 184, 42)


def node(suffix):
    return PREFIX + suffix


def box_list(box):
    if box is None:
        return None
    if isinstance(box, (tuple, list)):
        return [int(value) for value in box]
    return [int(getattr(box, key)) for key in ("x", "y", "w", "h")]


def summarize(detail):
    if detail is None:
        raise RuntimeError("原生 run_recognition 没有返回 RecognitionDetail")
    results = []
    for result in detail.filtered_results:
        item = {"box": box_list(getattr(result, "box", None))}
        for key in ("text", "score", "count"):
            value = getattr(result, key, None)
            if value is not None:
                item[key] = value
        results.append(item)
    return {"hit": bool(detail.hit), "box": box_list(detail.box), "filtered": results}


def state_expectations(day2_available=False):
    expected = {
        node("PageIdentity"): True,
        node("PageReady"): True,
        node("RewardPopup"): False,
        node("CheckedAllDays"): not day2_available,
        node("Day2YellowButton"): day2_available,
        node("Day7YellowButton"): False,
    }
    for day in range(1, 8):
        base = "Day%d" % day
        expected[node(base + "Available")] = day == 2 and day2_available
        expected[node(base + "AlreadyClaimed")] = day == 1 or (
            day == 2 and not day2_available
        )
        expected[node(base + "Locked")] = day >= 3
        expected[node(base + "Resolved")] = day != 2 or not day2_available
    return expected


def negative_expectations():
    expected = {
        node(suffix): False
        for suffix in (
            "PageIdentity",
            "PageReady",
            "RewardPopup",
            "CheckedAllDays",
            "NoReward",
            "Complete",
        )
    }
    for day in range(1, 8):
        for suffix in ("Available", "AlreadyClaimed", "Locked"):
            expected[node("Day%d%s" % (day, suffix))] = False
    return expected


class ImageController(CustomController):
    """默认拒绝所有输入；可选 FSM 只把合法 DAY2/关闭输入转为内存图像变化。"""

    def __init__(self, image, frames=None, initial_stage="available"):
        self.image = image
        self.frames = frames
        self.stage = initial_stage if frames else "static"
        self.inputs = []
        self.rejected_inputs = []
        self.lifecycle_events = []
        super().__init__()

    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "claim-event-rewards-native-image-only"

    def get_features(self):
        return 0

    def screencap(self):
        image = self.frames[self.stage] if self.frames else self.image
        return image.copy()

    def reject_input(self, *args):
        self.rejected_inputs.append([str(value) for value in args])
        return False

    start_app = stop_app = swipe = touch_down = touch_move = reject_input
    touch_up = input_text = key_down = key_up = scroll = reject_input
    relative_move = reject_input

    def inactive(self):
        # MaaFW 启停时调用的生命周期通知，不是输入；这里只记录内存中的 no-op。
        self.lifecycle_events.append("inactive")
        return True

    def shell(self, *args):
        self.reject_input(*args)
        return None

    @staticmethod
    def inside(x, y, box):
        left, top, width, height = box
        return left <= x < left + width and top <= y < top + height

    def click(self, x, y):
        if self.frames and self.stage == "available" and self.inside(x, y, DAY2_BUTTON):
            self.inputs.append({"kind": "click", "stage": self.stage, "xy": [x, y]})
            self.stage = "popup"
            return True
        if self.frames and self.stage == "popup" and self.inside(x, y, POPUP_CLOSE):
            self.inputs.append({"kind": "click", "stage": self.stage, "xy": [x, y]})
            self.stage = "claimed"
            return True
        return self.reject_input("click", x, y)

    def click_key(self, keycode):
        if self.frames and self.stage == "popup" and keycode == 27:
            self.inputs.append({"kind": "key", "stage": self.stage, "key": keycode})
            self.stage = "claimed"
            return True
        return self.reject_input("click_key", keycode)


class RecognitionProbe(CustomAction):
    """只调用现有节点的 run_recognition，绝不执行这些节点的 action。"""

    def __init__(self, controller, expected, diagnostics=None, read_anchors=False):
        super().__init__()
        self.controller = controller
        self.expected = expected
        self.diagnostics = diagnostics or {}
        self.read_anchors = read_anchors
        self.results = {}
        self.anchors = {}
        self.errors = []

    def run(self, context, argv):
        try:
            image = self.controller.screencap()
            for name in self.expected:
                if context.tasker.stopping:
                    raise RuntimeError("识别回放被停止")
                self.results[name] = summarize(context.run_recognition(name, image))
            for label, (name, override) in self.diagnostics.items():
                # clone 的局部 ROI 覆盖不污染生产节点，更不改变任何图片或 OCR 输出。
                cloned = context.clone()
                self.results[label] = summarize(
                    cloned.run_recognition(name, image, pipeline_override=override)
                )
            if self.read_anchors:
                for suffix in ("ClaimConfirmation", "Outcome"):
                    name = node(suffix)
                    self.anchors[name] = context.get_anchor(name)
            return self.RunResult(success=True)
        except Exception:
            self.errors.append(traceback.format_exc())
            return self.RunResult(success=False)


class ClaimEventRewardsFail(CustomAction):
    """生产错误节点所需的明确失败回调，不导入会切换库模式的 maa.agent。"""

    def run(self, context, argv):
        return self.RunResult(success=False)


class NativeClaimEventRewardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SDK_ERROR:
            raise unittest.SkipTest("MaaFW/cv2/numpy 不可用：" + SDK_ERROR)
        missing = [
            name
            for name in ("det.onnx", "rec.onnx", "keys.txt")
            if not (RESOURCE / "model/ocr" / name).is_file()
        ]
        if missing:
            raise unittest.SkipTest("缺少真实 OCR 模型：" + ", ".join(missing))
        try:
            Library.open(
                Path(
                    os.environ.get(
                        "MAAFW_BINARY_PATH", Path(maa.__file__).parent / "bin"
                    )
                ),
                agent_server=False,
            )
            if Library.is_agent_server():
                raise RuntimeError("已导入 AgentServer；请单独运行本原生回归文件")
            # 全局日志只在类启动时配置一次，避免在原生异步工作线程间反复换目录。
            CACHE.mkdir(parents=True, exist_ok=True)
            if not Tasker.set_log_dir(CACHE / "framework"):
                raise RuntimeError("原生日志目录配置失败")
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            Tasker.set_debug_mode(False)
        except (OSError, FileNotFoundError) as exc:
            raise unittest.SkipTest("MaaFW 原生动态库缺失：%s" % exc) from exc
        try:
            cls.sdk_version = importlib.metadata.version("maafw")
        except importlib.metadata.PackageNotFoundError:
            cls.sdk_version = "unknown"
        cls.frames = {}
        cls.fixture_hashes = {}
        for label, filename in (
            ("available", "available_day2.png"),
            ("popup", "reward_popup.png"),
            ("claimed", "claimed_day2.png"),
        ):
            data = (FIXTURES / filename).read_bytes()
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape != (720, 1280, 3):
                raise AssertionError("真实夹具必须为 1280x720 BGR：" + filename)
            cls.frames[label] = image
            cls.fixture_hashes[filename] = hashlib.sha256(data).hexdigest()

    def run_native(
        self,
        tag,
        image,
        expected,
        *,
        fsm=False,
        diagnostics=None,
        initial_stage="available"
    ):
        controller = ImageController(image, self.frames if fsm else None, initial_stage)
        probe = RecognitionProbe(controller, expected, diagnostics, read_anchors=fsm)
        resource = Resource()
        tasker = None
        timer = None
        timed_out = threading.Event()
        report = {
            "mode": "memory_fsm_no_world_navigation" if fsm else "recognition_only",
            "maafw_binding_version": self.sdk_version,
            "fixture_sha256": self.fixture_hashes,
            "expected": expected,
            "task_succeeded": False,
        }
        try:
            before = PIPELINE.read_bytes()
            report["pipeline_sha256"] = hashlib.sha256(before).hexdigest()
            self.assertTrue(
                resource.post_bundle(RESOURCE).wait().succeeded, "原生资源加载失败"
            )
            self.assertEqual(
                before,
                PIPELINE.read_bytes(),
                "生产 pipeline 正在改写；待写入完成后重跑",
            )
            for name in expected:
                self.assertIsNotNone(
                    resource.get_node_data(name),
                    "依赖生产节点尚未完成（不能跳过或伪称通过）：" + name,
                )
            self.assertTrue(resource.register_custom_action(PROBE_ACTION, probe))
            self.assertTrue(
                resource.register_custom_action(
                    "claim_event_rewards_fail", ClaimEventRewardsFail()
                )
            )
            override = {
                PROBE_NODE: {
                    "recognition": {"type": "DirectHit"},
                    "action": {
                        "type": "Custom",
                        "param": {"custom_action": PROBE_ACTION},
                    },
                    "next": [],
                    "on_error": [],
                    "rate_limit": 0,
                    "pre_delay": 0,
                    "post_delay": 0,
                }
            }
            if fsm:
                # 唯一生产流程覆盖：Complete 的最终世界导航改为测试末尾探针。
                # 保留七格、奖励弹窗、ClaimConfirmation/Outcome 锚点及所有识别动作。
                override[node("Complete")] = {"next": [PROBE_NODE]}
            self.assertTrue(resource.override_pipeline(override))
            self.assertTrue(controller.post_connection().wait().succeeded)
            self.assertTrue(controller.set_screenshot_use_raw_size(True))
            tasker = Tasker()
            self.assertTrue(tasker.bind(resource, controller))
            self.assertTrue(tasker.inited)
            entry = node("PageReady") if fsm else PROBE_NODE

            def interrupt():
                timed_out.set()
                tasker.post_stop()  # 不在回调线程等待 stop，避免死锁。

            timer = threading.Timer(180, interrupt)
            timer.daemon = True
            timer.start()
            job = tasker.post_task(entry).wait()
            timer.cancel()
            timer.join()
            self.assertFalse(timed_out.is_set(), "原生回放超时并已安全停止")
            # 必须在 post_stop 前保存状态和所有惰性节点详情；之后绝不再查询旧 job。
            report["task_succeeded"] = bool(job.succeeded)
            detail = job.get()
            report["nodes"] = (
                [
                    {
                        "name": item.name,
                        "completed": bool(item.completed),
                        "box": (
                            box_list(item.recognition.box) if item.recognition else None
                        ),
                    }
                    for item in detail.nodes
                    if item is not None
                ]
                if detail
                else []
            )
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if tasker is not None:
                tasker.post_stop().wait()
            report.update(
                {
                    "recognitions": probe.results,
                    "anchors": probe.anchors,
                    "callback_errors": probe.errors,
                    "inputs": controller.inputs,
                    "rejected_inputs": controller.rejected_inputs,
                    "final_stage": controller.stage,
                }
            )
            report["recognition_contract_succeeded"] = not probe.errors and all(
                name in probe.results and probe.results[name]["hit"] == value
                for name, value in expected.items()
            )
            (CACHE / (tag + ".json")).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        self.assertFalse(probe.errors, "原生识别回调异常；查看缓存报告")
        self.assertTrue(report["task_succeeded"], "原生任务失败：%s" % report["nodes"])
        self.assertEqual(controller.rejected_inputs, [], "生产流程尝试未授权的内存输入")
        if not fsm:
            self.assertEqual(controller.inputs, [], "只读回放不得执行节点 action")
        for name, value in expected.items():
            with self.subTest(frame=tag, node=name):
                self.assertEqual(probe.results[name]["hit"], value)
        return report

    def assert_box_inside(self, report, name, roi):
        box = report["recognitions"][name]["box"]
        self.assertIsNotNone(box, name)
        x, y, width, height = box
        left, top, roi_width, roi_height = roi
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertGreaterEqual(x, left)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(x + width, left + roi_width)
        self.assertLessEqual(y + height, top + roi_height)

    def test_available_day2_real_native_recognition(self):
        expected = state_expectations(day2_available=True)
        expected.update(
            {
                node("Day2AvailableText"): True,
                node("Day2ClaimedText"): False,
                node("Title"): True,
                node("SignInHint"): True,
            }
        )
        report = self.run_native("available_day2", self.frames["available"], expected)
        self.assert_box_inside(report, node("Day2AvailableText"), DAY2_BUTTON)
        self.assert_box_inside(report, node("Day2YellowButton"), DAY2_BUTTON)
        self.assert_box_inside(report, node("Title"), (1030, 90, 217, 78))

    def test_reward_popup_real_native_recognition(self):
        expected = {
            node("RewardPopup"): True,
            node("PageIdentity"): False,
            node("PageReady"): False,
            node("CheckedAllDays"): False,
            node("RewardPopupTitle"): True,
            node("RewardPopupHint"): True,
            node("RewardPopupIcon"): True,
        }
        expected.update({node("Day%dAvailable" % day): False for day in range(1, 8)})
        report = self.run_native("reward_popup", self.frames["popup"], expected)
        self.assert_box_inside(report, node("RewardPopupTitle"), (585, 289, 111, 45))
        self.assert_box_inside(report, node("RewardPopupHint"), POPUP_CLOSE)
        self.assert_box_inside(report, node("RewardPopupIcon"), (603, 356, 75, 74))

    def test_claimed_day2_real_native_recognition(self):
        expected = state_expectations()
        expected.update(
            {node("Day2ClaimedText"): True, node("Day2AvailableText"): False}
        )
        report = self.run_native("claimed_day2", self.frames["claimed"], expected)
        self.assert_box_inside(report, node("Day2ClaimedText"), DAY2_BUTTON)

    def test_black_frame_cannot_be_positive_success(self):
        expected = negative_expectations()
        expected.update({node("Day%dResolved" % day): False for day in range(1, 8)})
        self.run_native("black_frame", np.zeros((720, 1280, 3), np.uint8), expected)

    def test_wrong_page_with_real_available_button_cannot_succeed(self):
        # 合成负例只遮去真实活动页标题/签到提示，保留真实可领取按钮和七格布局。
        # 不把此负例伪称为另一张实机错页，也不合成任何正面领取文案。
        image = self.frames["available"].copy()
        image[90:168, 1030:1247] = 0
        image[202:247, 1110:1245] = 0
        expected = negative_expectations()
        expected.update(
            {
                node("Day2AvailableText"): True,
                node("Day2YellowButton"): True,
                node("CardLayout"): True,
            }
        )
        self.run_native("wrong_page_masked_identity", image, expected)

    def test_day7_permanent_yellow_card_is_not_available_button(self):
        main_card_label = "diagnostic_day7_main_card_yellow"
        yellow = node("Day7YellowButton")
        diagnostics = {
            main_card_label: (
                yellow,
                {yellow: {"recognition": {"param": {"roi": [1067, 277, 177, 293]}}}},
            )
        }
        for label in ("available", "claimed"):
            with self.subTest(frame=label):
                report = self.run_native(
                    "day7_yellow_guard_" + label,
                    self.frames[label],
                    {
                        yellow: False,
                        node("Day7Available"): False,
                        node("Day7Locked"): True,
                    },
                    diagnostics=diagnostics,
                )
                self.assertTrue(report["recognitions"][main_card_label]["hit"])
                self.assert_box_inside(report, main_card_label, (1067, 277, 177, 293))

    def test_native_day2_fsm_claim_once_close_once_confirm_gray(self):
        report = self.run_native(
            "fsm_day2_once", self.frames["available"], state_expectations(), fsm=True
        )
        names = [item["name"] for item in report["nodes"]]
        self.assertEqual(
            names,
            [
                node("PageReady"),
                node("Day1AlreadyClaimed"),
                node("Day2Available"),
                node("RewardPopup"),
                node("Day2ClaimConfirmed"),
                *[node("Day%dLocked" % day) for day in range(3, 8)],
                node("CheckedAllDays"),
                node("Complete"),
                PROBE_NODE,
            ],
        )
        self.assertTrue(all(item["completed"] for item in report["nodes"]))
        self.assertEqual(report["final_stage"], "claimed")
        self.assertEqual(
            [item["stage"] for item in report["inputs"]], ["available", "popup"]
        )
        self.assertEqual(report["anchors"][node("Outcome")], node("Complete"))
        self.assertIn(report["anchors"][node("ClaimConfirmation")], (None, ""))

    def test_native_already_claimed_fsm_has_no_inputs(self):
        # 同一张真实已领取图的独立子流程；不模拟新日期，不包含返世界导航。
        report = self.run_native(
            "fsm_already_claimed",
            self.frames["claimed"],
            state_expectations(),
            fsm=True,
            initial_stage="claimed",
        )
        names = [item["name"] for item in report["nodes"]]
        self.assertEqual(
            names,
            [
                node("PageReady"),
                node("Day1AlreadyClaimed"),
                node("Day2AlreadyClaimed"),
                *[node("Day%dLocked" % day) for day in range(3, 8)],
                node("CheckedAllDays"),
                node("NoReward"),
                node("Complete"),
                PROBE_NODE,
            ],
        )
        self.assertEqual(report["inputs"], [])
        self.assertEqual(report["anchors"][node("Outcome")], node("NoReward"))
        self.assertIn(report["anchors"][node("ClaimConfirmation")], (None, ""))


if __name__ == "__main__":
    unittest.main()
