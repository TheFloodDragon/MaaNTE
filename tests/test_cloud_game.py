"""云启动逻辑回归：使用确定性的虚拟帧，不代替真实客户端识别验收。"""

import ast
import copy
import importlib.util
import json
import logging
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from maa.agent.agent_server import AgentServer

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = json.loads(
    (ROOT / "assets/resource/base/pipeline/CloudGame/CloudGame.json").read_text(
        encoding="utf-8"
    )
)
TASK = json.loads(
    (ROOT / "assets/resource/tasks/CloudGame.json").read_text(encoding="utf-8")
)


def load_cloud():
    # 不导入 action/__init__.py，避免加载与测试无关的音频、导航等依赖。
    modules = {
        name: ModuleType(name)
        for name in ("utils", "utils.logger", "utils.maafocus", "utils.pienv")
    }
    modules["utils.logger"].logger = logging.getLogger("cloud-game-test")
    modules["utils.maafocus"].PrintT = Mock()
    modules["utils.pienv"].controller_name = lambda: "CloudGame-Front"
    spec = importlib.util.spec_from_file_location(
        "_test_cloud_game", ROOT / "agent/custom/action/cloud_game.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(sys.modules, modules), patch.object(
        AgentServer, "custom_action", lambda name: lambda cls: cls
    ):
        spec.loader.exec_module(module)
    return module


cloud = load_cloud()


class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


def scene(*hits, texts=(), missing=False, fail_node=None, multiple=False):
    return SimpleNamespace(
        hits=set(hits),
        texts=texts,
        missing=missing,
        fail_node=fail_node,
        multiple=multiple,
    )


HOME = ("CloudGameHomeScreen", "CloudGameEnterText")
CONFIRM = ("CloudGameStartConfirmNotice", "CloudGameStartConfirmEnter")
DAILY = ("CloudGameDailyLoginTitle", "CloudGameDailyLoginCloseButton")
WORLD = ("InWorld",)


class FakeContext:
    def __init__(self, frames):
        self.frames = frames
        self.index = -1
        self.current = frames[0]
        self.nodes = copy.deepcopy(PIPELINE)
        self.nodes["CloudGameProfile"]["attach"]["calibrated"] = True
        self.clicks = []
        self.capture_count = 0
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.image[0, 0] = 1
        self.controller = SimpleNamespace(
            post_screencap=self.capture, post_click=self.click, cached_image=self.image
        )
        self.tasker = SimpleNamespace(stopping=False, controller=self.controller)
        self.override_ok = True

    def capture(self):
        self.capture_count += 1
        self.index = min(self.index + 1, len(self.frames) - 1)
        self.current = self.frames[self.index]
        self.controller.cached_image = self.image
        return SimpleNamespace(
            wait=lambda: SimpleNamespace(succeeded=not self.current.missing)
        )

    def click(self, x, y):
        self.clicks.append((x, y))
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))

    def match(self, node):
        if node in self.current.hits:
            return True
        rec = self.nodes.get(node, {}).get("recognition", {})
        if rec.get("type") == "And":
            return all(self.match(name) for name in rec["param"]["all_of"])
        return False

    def run_recognition(self, node, image):
        if node == self.current.fail_node:
            return None
        hit = self.match(node)
        results = (
            [SimpleNamespace(text=text) for text in self.current.texts]
            if node == "CloudGameQueueText"
            else []
        )
        if hit:
            results = [SimpleNamespace(text="", box=[100, 200, 80, 30])] * (
                2 if self.current.multiple else 1
            )
        return SimpleNamespace(
            hit=hit, box=[100, 200, 80, 30], filtered_results=results
        )

    def get_node_data(self, name):
        return self.nodes.get(name)

    def override_pipeline(self, data):
        for name, values in data.items():
            self.nodes[name].update(values)
        return self.override_ok

    def override_next(self, name, nodes):
        self.nodes[name]["next"] = nodes
        return self.override_ok


def arg(node, params=None, task_id=42, box=(100, 200, 80, 30)):
    return SimpleNamespace(
        node_name=node,
        task_detail=SimpleNamespace(task_id=task_id),
        custom_action_param=json.dumps(params or {}),
        box=list(box),
    )


class CloudLogicTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.timer_patch = patch.object(cloud, "time", self.clock)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.message_patch = patch.object(cloud, "PrintT", Mock())
        self.message_patch.start()
        self.addCleanup(self.message_patch.stop)
        # 启动确认弹窗是独立顶层窗口，取帧走 Win32 枚举。这里只替换取帧这一层，
        # 是否命中仍由 FakeContext 的场景决定，保证确认分支可离线验证。
        self.dialog_patch = patch.object(
            cloud,
            "_owned_dialog_frame",
            lambda: np.zeros((720, 1280, 3), dtype=np.uint8),
        )
        self.dialog_patch.start()
        self.addCleanup(self.dialog_patch.stop)
        self.confirm_context = None
        self.confirm_clicked = False
        self.find_confirm_patch = patch.object(
            cloud, "_find_owned_confirm", side_effect=self.fake_find_confirm
        )
        self.find_confirm_patch.start()
        self.addCleanup(self.find_confirm_patch.stop)
        self.click_confirm_patch = patch.object(
            cloud, "_click_owned_target", side_effect=self.fake_click_confirm
        )
        self.click_confirm_patch.start()
        self.addCleanup(self.click_confirm_patch.stop)
        self.confirm_state_patch = patch.object(
            cloud,
            "_confirm_target_state",
            side_effect=lambda target: "gone" if self.confirm_clicked else "present",
        )
        self.confirm_state_patch.start()
        self.addCleanup(self.confirm_state_patch.stop)
        cloud._session = None
        # 入口的本地窗口检查在离线测试中用已就绪的假窗口替代；cloud_start 单独测试。
        self.prepare_patch = patch.object(
            cloud, "_prepare_cloud_client", side_effect=self.fake_prepare
        )
        self.prepare_patch.start()
        self.addCleanup(self.prepare_patch.stop)
        self.owned_patch = patch.object(
            cloud, "_owned_dialog_candidates", return_value=[]
        )
        self.owned_patch.start()
        self.addCleanup(self.owned_patch.stop)

    def fake_prepare(self, context, state):
        state.main_hwnd = 1000
        cloud.PrintT(context, "cloud_game.client_ready")

    def fake_find_confirm(self, context, screen, target, expected_hwnd=None):
        if not (
            context.match("CloudGameStartConfirmNotice")
            and context.match("CloudGameStartConfirmEnter")
        ):
            return None
        result = cloud._ConfirmTarget(
            hwnd=1001,
            owner=1000,
            rect=(0, 0, 1280, 720),
            client_size=(1280, 720),
            box=(100, 200, 80, 30),
        )
        cloud._confirm_target = result
        if cloud._session is not None:
            cloud._session.confirm_target = result
        return result

    def fake_click_confirm(self, target):
        self.confirm_clicked = True
        if self.confirm_context is not None:
            self.confirm_context.click(target.box[0] + 40, target.box[1] + 15)
        return True

    def reset(self, frames, **params):
        context = FakeContext(frames)
        self.confirm_context = context
        self.confirm_clicked = False
        defaults = {
            "transition_timeout_seconds": 10,
            "login_timeout_seconds": 10,
            "timeout_minutes": 1,
        }
        defaults.update(params)
        context.nodes["CloudGameAutoQueueConfig"]["attach"]["auto_queue"] = (
            defaults.pop("auto_queue", True)
        )
        context.nodes["CloudGameQueueTimeoutConfig"]["attach"]["timeout_minutes"] = (
            defaults.pop("timeout_minutes")
        )
        result = cloud.CloudGameReset().run(
            context, arg("CloudGameStartEntrance", defaults)
        )
        self.assertTrue(result.success)
        return context

    def messages(self, key):
        return [
            call.args[2:] for call in cloud.PrintT.call_args_list if call.args[1] == key
        ]

    def test_uncalibrated_profile_blocks_input(self):
        context = FakeContext([scene(*HOME)])
        context.nodes["CloudGameProfile"]["attach"]["calibrated"] = False
        self.assertFalse(
            cloud.CloudGameReset().run(context, arg("CloudGameStartEntrance")).success
        )
        self.assertTrue(self.messages("cloud_game.calibration_required"))
        self.assertEqual(context.capture_count, 0)
        self.assertEqual(context.clicks, [])

    def test_wrong_controller_is_rejected(self):
        with patch.object(cloud, "controller_name", return_value="Win32-Front"):
            self.assertFalse(
                cloud.CloudGameReset()
                .run(FakeContext([scene()]), arg("CloudGameStartEntrance"))
                .success
            )

    def test_invalid_parameters_are_not_silently_defaulted(self):
        for value in (0, -1, True, "NaN", "Infinity", "garbage", 1441):
            with self.subTest(value=value):
                context = FakeContext([scene()])
                context.nodes["CloudGameQueueTimeoutConfig"]["attach"][
                    "timeout_minutes"
                ] = value
                self.assertFalse(
                    cloud.CloudGameReset()
                    .run(context, arg("CloudGameStartEntrance"))
                    .success
                )
        for value in ("{bad", "[]", "null", "true"):
            with self.subTest(value=value), self.assertRaises(cloud._CloudError):
                cloud._params(value)
        self.assertEqual(
            cloud._params('{"timeout_minutes": 15}')["timeout_minutes"], 15
        )
        self.assertEqual(cloud._params({"auto_queue": False}), {"auto_queue": False})

    def test_entrance_fails_closed_when_client_window_is_missing(self):
        self.prepare_patch.stop()
        with patch.object(cloud, "_enumerate_cloud_windows", return_value=[]):
            with patch.dict(
                sys.modules,
                {"cloud_start": ModuleType("cloud_start")},
            ) as modules:
                modules["cloud_start"].client_windows = lambda: []
                modules["cloud_start"].desktop_interactive = lambda: True
                modules["cloud_start"].prepare_client_window = Mock()
                modules["cloud_start"].window_responding = Mock()
                context = FakeContext([scene(*HOME)])
                self.assertFalse(
                    cloud.CloudGameReset()
                    .run(context, arg("CloudGameStartEntrance"))
                    .success
                )
        self.prepare_patch.start()
        self.assertTrue(self.messages("cloud_game.client_missing"))
        self.assertIsNone(cloud._session)
        self.assertEqual(context.clicks, [])

    def test_login_obstacles_wait_without_submitting_credentials(self):
        for node, key in (
            ("CloudGameLoginForm", "cloud_game.login_required"),
            ("CloudGameVerification", "cloud_game.verification_required"),
            ("CloudGameAuthorization", "cloud_game.authorization_required"),
            ("CloudGameLoginFailure", "cloud_game.login_retry_required"),
        ):
            with self.subTest(node=node):
                cloud.PrintT.reset_mock()
                context = self.reset(
                    [scene(node), scene(node), scene(*HOME), scene(*HOME)]
                )
                self.assertTrue(
                    cloud.CloudGameWaitLogin()
                    .run(context, arg("CloudGameLoginWait"))
                    .success
                )
                self.assertEqual(len(self.messages(key)), 1)
                self.assertEqual(context.clicks, [])

    def test_login_failure_does_not_extend_deadline(self):
        context = self.reset(
            [scene("CloudGameLoginFailure")], login_timeout_seconds=4
        )
        self.assertFalse(
            cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success
        )
        self.assertTrue(self.messages("cloud_game.login_timeout"))
        self.assertLessEqual(self.clock.now, 4.01)
        self.assertIsNone(cloud._session)

    def owned_window(self, hwnd=1001):
        return cloud._WindowInfo(
            hwnd=hwnd, rect=(0, 8, 1280, 728), client_size=(1280, 720),
            owner=1000, title="NTECloudGame",
            class_name="Qt51517QWindowToolSaveBits", pid=7,
        )

    def test_remembered_account_window_is_submitted_once_then_waits(self):
        # 实机：记住账号的登录窗口无输入框，只点一次“登录”；之后靠主窗口状态判断。
        self.owned_patch.stop()
        clicks = []
        overlay = self.owned_window()
        dialog = {"present": True}
        frames = [scene("CloudGameLoginScreen")] * 3 + [scene(*HOME)] * 2

        def candidates():
            return [overlay] if dialog["present"] else []

        def owned_hits(node):
            return dialog["present"] and node in (
                "CloudGameLoginOtherMethods", "CloudGameLoginSubmit"
            )

        def click(target):
            clicks.append(target.hwnd)
            dialog["present"] = False
            return True

        with patch.object(cloud, "_owned_dialog_candidates", side_effect=candidates), patch.object(
            cloud, "_capture_owned_window",
            return_value=np.zeros((720, 1280, 3), dtype=np.uint8),
        ), patch.object(cloud, "_click_owned_target", side_effect=click):
            context = self.reset(frames)
            original = context.run_recognition

            def run_recognition(node, image):
                if owned_hits(node):
                    return SimpleNamespace(
                        hit=True, box=[613, 433, 54, 28],
                        filtered_results=[SimpleNamespace(text="登录", box=[613, 433, 54, 28])],
                    )
                return original(node, image)

            context.run_recognition = run_recognition
            self.assertTrue(
                cloud.CloudGameWaitLogin()
                .run(context, arg("CloudGameLoginWait"))
                .success
            )
        self.owned_patch.start()
        self.assertEqual(clicks, [1001])
        self.assertEqual(len(self.messages("cloud_game.login_remembered")), 1)
        self.assertEqual(len(self.messages("cloud_game.login_detected")), 1)
        # 主窗口的“点击任意位置登录”入口不再被重复点击。
        self.assertEqual(context.clicks, [])

    def test_credential_form_in_owned_window_is_never_submitted(self):
        self.owned_patch.stop()
        overlay = self.owned_window()
        with patch.object(cloud, "_owned_dialog_candidates", return_value=[overlay]), patch.object(
            cloud, "_capture_owned_window",
            return_value=np.zeros((720, 1280, 3), dtype=np.uint8),
        ), patch.object(cloud, "_click_owned_target") as click:
            context = self.reset([scene(*HOME)], login_timeout_seconds=3)
            original = context.run_recognition
            context.run_recognition = lambda node, image: (
                SimpleNamespace(hit=True, box=[1, 1, 1, 1], filtered_results=[SimpleNamespace(text="x", box=[1, 1, 1, 1])])
                if node in ("CloudGameLoginForm", "CloudGameLoginOtherMethods", "CloudGameLoginSubmit")
                else original(node, image)
            )
            self.assertFalse(
                cloud.CloudGameWaitLogin()
                .run(context, arg("CloudGameLoginWait"))
                .success
            )
            click.assert_not_called()
        self.owned_patch.start()
        self.assertEqual(len(self.messages("cloud_game.login_required")), 1)
        self.assertTrue(self.messages("cloud_game.login_timeout"))

    def test_owned_capture_failure_keeps_waiting_instead_of_failing(self):
        self.owned_patch.stop()
        overlay = self.owned_window()
        with patch.object(cloud, "_owned_dialog_candidates", return_value=[overlay]), patch.object(
            cloud, "_capture_owned_window", return_value=None
        ):
            context = self.reset([scene(*HOME)], login_timeout_seconds=3)
            self.assertFalse(
                cloud.CloudGameWaitLogin()
                .run(context, arg("CloudGameLoginWait"))
                .success
            )
        self.owned_patch.start()
        self.assertFalse(self.messages("cloud_game.recognition_failed"))
        self.assertTrue(self.messages("cloud_game.login_timeout"))
        self.assertGreaterEqual(context.capture_count, 2)

    def test_unknown_overlay_blocks_home_as_login_evidence(self):
        self.owned_patch.stop()
        overlay = cloud._WindowInfo(
            hwnd=1001, rect=(0, 0, 1280, 720), client_size=(1280, 720),
            owner=1000, title="", class_name="Qt51517QWindowToolSaveBitsOwnDC",
        )
        with patch.object(
            cloud, "_owned_dialog_candidates", return_value=[overlay]
        ), patch.object(
            cloud,
            "_capture_owned_window",
            return_value=np.zeros((720, 1280, 3), dtype=np.uint8),
        ):
            context = self.reset([scene(*HOME)], login_timeout_seconds=3)
            self.assertFalse(
                cloud.CloudGameWaitLogin()
                .run(context, arg("CloudGameLoginWait"))
                .success
            )
        self.owned_patch.start()
        self.assertEqual(len(self.messages("cloud_game.login_required")), 1)
        self.assertEqual(context.clicks, [])

    def test_finish_and_failure_release_session(self):
        context = self.reset([scene(*HOME)])
        self.assertIsNotNone(cloud._session)
        self.assertTrue(
            cloud.CloudGameFinish().run(context, arg("CloudGameStartDone")).success
        )
        self.assertIsNone(cloud._session)
        self.assertIsNone(cloud._confirm_target)
        context = self.reset([scene(*HOME)])
        self.assertFalse(
            cloud.CloudGameFail().run(context, arg("CloudGameStartFailed")).success
        )
        self.assertIsNone(cloud._session)

    def test_state_does_not_leak_to_another_task(self):
        context = self.reset([scene(*HOME)])
        self.assertFalse(
            cloud.CloudGameWaitLogin()
            .run(context, arg("CloudGameLoginWait", task_id=43))
            .success
        )
        self.assertEqual(context.capture_count, 0)

    def test_reset_clears_previous_attempts(self):
        context = self.reset([scene(*HOME)])
        cloud._session.clicked.add("enter")
        self.assertTrue(
            cloud.CloudGameReset().run(context, arg("CloudGameStartEntrance")).success
        )
        self.assertFalse(cloud._session.clicked)

    def test_login_entry_is_clicked_once_and_prompts_once(self):
        context = self.reset(
            [scene("CloudGameLoginScreen"), scene("CloudGameLoginScreen"), scene(*HOME)]
        )
        self.assertTrue(
            cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success
        )
        self.assertEqual(context.clicks, [(140, 215)])
        self.assertEqual(len(self.messages("cloud_game.login_required")), 1)
        self.assertEqual(len(self.messages("cloud_game.login_detected")), 1)

    def test_daily_popup_after_login_returns_to_pipeline(self):
        context = self.reset([scene("CloudGameLoginScreen"), scene(*DAILY)])
        self.assertTrue(
            cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success
        )
        self.assertEqual(context.clicks, [(140, 215)])

    def test_login_wait_is_bounded(self):
        context = self.reset([scene("CloudGameLoginScreen")], login_timeout_seconds=3)
        self.assertFalse(
            cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success
        )
        self.assertTrue(self.messages("cloud_game.login_timeout"))
        self.assertLessEqual(self.clock.now, 3.01)

    def test_manual_option_preserves_recognizers_and_blocks_clicks(self):
        context = self.reset([scene(*HOME)], auto_queue=False)
        self.assertTrue(context.nodes["CloudGameManualEnterNotice"]["enabled"])
        self.assertNotIn("enabled", context.nodes["CloudGameEnterText"])
        self.assertTrue(
            cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success
        )
        self.assertFalse(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
            .success
        )
        self.assertFalse(
            cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait")).success
        )
        self.assertEqual(context.clicks, [])

    def test_enter_click_once_and_wait_for_delayed_queue(self):
        context = self.reset([scene(*HOME), scene(*HOME), scene(*CONFIRM)])
        self.assertTrue(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
            .success
        )
        self.assertEqual(context.clicks, [(140, 215)])
        self.assertFalse(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
            .success
        )
        self.assertEqual(len(context.clicks), 1)

    def test_start_confirmation_is_required_before_queueing(self):
        context = self.reset([scene(*HOME), scene(*HOME)], transition_timeout_seconds=3)
        # 首页停留不能当作开始成功；客户端 30 秒倒计时后会自行退出。
        self.assertFalse(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
            .success
        )
        self.assertEqual(len(context.clicks), 1)
        context = self.reset([scene(*CONFIRM), scene("CloudGameQueueScreen")])
        self.assertTrue(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameStartConfirm", {"kind": "confirm"}))
            .success
        )
        self.assertEqual(len(context.clicks), 1)

    def test_confirmation_dialog_is_not_clicked_in_manual_mode(self):
        context = self.reset([scene(*CONFIRM)], auto_queue=False)
        self.assertFalse(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameStartConfirm", {"kind": "confirm"}))
            .success
        )
        self.assertEqual(context.clicks, [])
        self.assertTrue(self.messages("cloud_game.manual_enter"))

    def test_queue_wait_yields_to_start_confirmation(self):
        context = self.reset([scene("CloudGameQueueScreen"), scene(*CONFIRM)])
        self.assertTrue(
            cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait")).success
        )
        self.assertEqual(context.clicks, [])

    def test_queue_kind_is_rejected_because_client_has_no_queue_choice(self):
        # 客户端二进制中不存在“队列”字样：没有队列选择页，也不能凭猜测点击任何队列按钮。
        context = self.reset([scene("CloudGameQueueScreen")])
        self.assertFalse(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameQueueWait", {"kind": "queue"}))
            .success
        )
        self.assertEqual(context.clicks, [])
        self.assertTrue(self.messages("cloud_game.invalid_config"))

    def test_unchanged_button_times_out_without_retry(self):
        context = self.reset([scene(*HOME)], transition_timeout_seconds=3)
        self.assertFalse(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
            .success
        )
        self.assertEqual(len(context.clicks), 1)

    def test_popup_waits_for_animation_and_two_positive_frames(self):
        context = self.reset([scene(*DAILY), scene(*DAILY), scene(*HOME), scene(*HOME)])
        self.assertTrue(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameCloseDailyLoginPopup", {"kind": "daily"}))
            .success
        )
        self.assertEqual(len(context.clicks), 1)
        self.assertEqual(len(self.messages("cloud_game.daily_login_closed")), 1)

    def test_popup_missing_frame_or_recognition_failure_is_not_success(self):
        for next_frame in (
            scene(missing=True),
            scene(*HOME, fail_node="CloudGameDailyLoginTitle"),
            scene(),
        ):
            with self.subTest(frame=next_frame):
                cloud.PrintT.reset_mock()
                context = self.reset(
                    [scene(*DAILY), next_frame], transition_timeout_seconds=2
                )
                self.assertFalse(
                    cloud.CloudGameClick()
                    .run(
                        context, arg("CloudGameCloseDailyLoginPopup", {"kind": "daily"})
                    )
                    .success
                )
                self.assertFalse(self.messages("cloud_game.daily_login_closed"))
                self.assertEqual(len(context.clicks), 1)

    def test_queue_keeps_separate_values_and_deduplicates(self):
        first = ("预计等待时间", "１０～２０分钟", "当前排队位置", "１２")
        later = ("预计等待时间", "5分钟", "当前排队位置", "3")
        context = self.reset(
            [
                scene("CloudGameQueueScreen", texts=first),
                scene("CloudGameQueueScreen", texts=first),
                scene("CloudGameQueueScreen", texts=later),
                scene("CloudGameQueueScreen", texts=later),
                scene("CloudGameLoading"),
                scene(*WORLD),
            ]
        )
        self.assertTrue(
            cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait")).success
        )
        statuses = self.messages("cloud_game.queue_status")
        self.assertEqual(len(statuses), 2)
        self.assertIn("12", statuses[0][0])
        self.assertIn("10~20分钟", statuses[0][0])
        self.assertIn("5分钟", statuses[1][0])
        self.assertEqual(len(self.messages("cloud_game.queue_started")), 1)
        self.assertEqual(context.clicks, [])

    def test_queue_yields_late_popup_or_game_login(self):
        for node in (
            "CloudGameDailyLoginTitle",
            "CloudGameGameLogin",
        ):
            with self.subTest(node=node):
                context = self.reset([scene("CloudGameQueueScreen"), scene(node)])
                self.assertTrue(
                    cloud.CloudGameQueueWait()
                    .run(context, arg("CloudGameQueueWait"))
                    .success
                )
                self.assertEqual(context.clicks, [])

    def test_queue_deadline_persists_across_popup_returns(self):
        context = self.reset([scene("CloudGameDailyLoginTitle")], timeout_minutes=0.1)
        self.assertTrue(
            cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait")).success
        )
        deadline = cloud._session.queue_deadline
        self.clock.now = deadline
        self.assertFalse(
            cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait")).success
        )
        # 截止时间不因回到 Pipeline 而延长；失败后会话状态随即释放。
        self.assertTrue(self.messages("cloud_game.queue_timeout"))
        self.assertIsNone(cloud._session)

    def test_queue_expiry_and_unknown_state_stop_without_input(self):
        for frame, key in (
            (scene("CloudGameQueueScreen"), "cloud_game.queue_timeout"),
            (scene(), "cloud_game.unknown_state"),
        ):
            context = self.reset(
                [frame], timeout_minutes=0.1, transition_timeout_seconds=2
            )
            self.assertFalse(
                cloud.CloudGameQueueWait()
                .run(context, arg("CloudGameQueueWait"))
                .success
            )
            self.assertTrue(self.messages(key))
            self.assertEqual(context.clicks, [])

    def test_missing_frame_and_wrong_frame_size_fail(self):
        context = self.reset([scene(missing=True)])
        self.assertFalse(
            cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait")).success
        )
        context = self.reset([scene(*HOME)])
        context.image = np.zeros((600, 800, 3), dtype=np.uint8)
        self.assertFalse(
            cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success
        )
        self.assertTrue(self.messages("cloud_game.invalid_frame"))

    def test_stop_during_poll_is_prompt(self):
        context = self.reset([scene("CloudGameQueueScreen")])
        self.clock.on_sleep = lambda: setattr(context.tasker, "stopping", True)
        self.assertFalse(
            cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait")).success
        )
        self.assertLessEqual(self.clock.now, 0.1)
        self.assertEqual(context.clicks, [])

    def test_ready_requires_two_frames_and_handles_late_popup(self):
        context = self.reset([scene(*WORLD), scene(*WORLD)])
        self.assertTrue(
            cloud.CloudGameConfirmReady().run(context, arg("CloudGameReady")).success
        )
        self.assertEqual(context.capture_count, 2)
        self.assertEqual(
            context.nodes["CloudGameReady"]["next"], ["CloudGameStartDone"]
        )
        context = self.reset([scene(*WORLD), scene(*DAILY, *WORLD)])
        self.assertTrue(
            cloud.CloudGameConfirmReady().run(context, arg("CloudGameReady")).success
        )
        self.assertEqual(context.nodes["CloudGameReady"]["next"], ["CloudGameDispatch"])

    def test_normalization_does_not_invent_numbers(self):
        self.assertEqual(cloud._queue_status(["", "???", "秘密账号"]), "")
        self.assertEqual(
            cloud._queue_status(["Queue position", "12"]), "Queue position | 12"
        )
        self.assertIn("待ち", cloud._queue_status(["待ち時間", "3分"]))
        self.assertIn("대기", cloud._queue_status(["대기 순서", "5"]))
        self.assertEqual(
            cloud._queue_status(["预计等待时间", "1:30"]), "预计等待时间 | 1:30"
        )


class ResourceContractTests(unittest.TestCase):
    def test_no_unconditional_login_loop(self):
        dispatch = PIPELINE["CloudGameDispatch"]["next"]
        self.assertNotIn("CloudGameLoginWait", dispatch)
        self.assertNotIn("CloudGameStartEntrance", dispatch)
        self.assertEqual(dispatch[-1], "CloudGameQueueWait")
        # 客户端没有队列选择页，任何点击队列的分支都不得回到分派表。
        for name in ("CloudGameQueueSelect", "CloudGameUnsafeQueue"):
            self.assertNotIn(name, dispatch)
            self.assertNotIn(name, PIPELINE)
        self.assertLess(
            dispatch.index("CloudGameManualEnterNotice"),
            dispatch.index("CloudGameEnterButton"),
        )
        self.assertLess(
            dispatch.index("CloudGameCloseDailyLoginPopup"),
            dispatch.index("CloudGameReady"),
        )

    # 从已安装云客户端 NTECloudGame.exe 内嵌 QML 源码中提取的界面文案。
    # 不含 libwlcgcore.dll 的 SDK 日志字符串（排队失败/排队超时等不会显示在界面上）。
    CLIENT_UI_STRINGS = {
        "排队中",
        "畅玩月卡加速排队中",
        "目前正排在第",
        "预计等待",
        "退出排队",
        "每日登录奖励",
        "首次登录奖励",
        "点击空白区域关闭",
        "努力加载中",
        "游戏启动中，黑屏时间可能较长",
        "用户正在使用其他设备排队",
        "请切换节点",
        "前往充值",
    }

    def test_shell_ocr_nodes_only_use_client_strings(self):
        self.assertFalse(PIPELINE["CloudGameProfile"]["attach"]["calibrated"])
        # 已用实机截图校准的节点必须是真实识别，不能留占位。
        for name in (
            "CloudGameLoginScreen",
            "CloudGameHomeScreen",
            "CloudGameEnterText",
            "CloudGameStartConfirmNotice",
            "CloudGameStartConfirmEnter",
        ):
            self.assertEqual(PIPELINE[name]["recognition"]["type"], "TemplateMatch")
            self.assertNotIn("inverse", PIPELINE[name])
        # 云外壳文案节点只能使用客户端二进制中真实存在的界面字符串，不得猜测。
        for name in (
            "CloudGameQueueScreen",
            "CloudGameQueueText",
            "CloudGameDailyLoginTitle",
            "CloudGameDailyLoginCloseButton",
            "CloudGameLoading",
            "CloudGameErrorState",
        ):
            node = PIPELINE[name]
            self.assertNotIn("inverse", node, name)
            self.assertEqual(node["recognition"]["type"], "OCR", name)
            expected = node["recognition"]["param"]["expected"]
            self.assertTrue(expected, name)
            self.assertLessEqual(set(expected), self.CLIENT_UI_STRINGS, name)
        # 没有关闭按钮：关闭动作只能点击弹窗自带的“点击空白区域关闭”提示。
        self.assertEqual(
            PIPELINE["CloudGameDailyLoginCloseButton"]["recognition"]["param"][
                "expected"
            ],
            ["点击空白区域关闭"],
        )
        # “请耐心等待”同时出现在故障排查确认框中，不得作为加载态证据。
        self.assertNotIn(
            "请耐心等待",
            PIPELINE["CloudGameLoading"]["recognition"]["param"]["expected"],
        )

    def test_references_and_registrations(self):
        external = {"InWorld", "SceneAnyEnterWorld"}
        for name, node in PIPELINE.items():
            refs = node.get("next", []) + node.get("on_error", [])
            rec = node.get("recognition", {})
            refs += rec.get("param", {}).get("all_of", [])
            for ref in refs:
                self.assertIn(
                    ref.removeprefix("[JumpBack]"),
                    PIPELINE.keys() | external,
                    (name, ref),
                )
                self.assertNotIn("__ScenePrivate", ref)
            self.assertEqual(
                [node.get(k) for k in ("rate_limit", "pre_delay", "post_delay")],
                [0, 0, 0],
            )
        registry = (ROOT / "agent/custom/action/__init__.py").read_text(
            encoding="utf-8"
        )
        for name in cloud.__all__:
            self.assertIn('"' + name + '"', registry)

    def test_options_are_isolated_and_do_not_disable_recognizers(self):
        option = TASK["option"]["CloudGameAutoQueue"]
        self.assertEqual(option["default_case"], "Yes")
        for case in option["cases"]:
            params = case["pipeline_override"]["CloudGameAutoQueueConfig"]["attach"]
            self.assertEqual(params["auto_queue"], case["name"] == "Yes")
            self.assertFalse(
                set(case["pipeline_override"])
                & set(TASK["option"]["CloudGameQueueTimeout"]["pipeline_override"])
            )
        field = TASK["option"]["CloudGameQueueTimeout"]["inputs"][0]
        for value in ("1", "30", "999", "1000", "1399", "1440"):
            self.assertRegex(value, field["verify"])
        for value in ("0", "1441", "-1", "nan", "1.5", "9999"):
            self.assertIsNone(re.fullmatch(field["verify"], value))

    def test_cloud_messages_exist_in_all_locales(self):
        code = (ROOT / "agent/custom/action/cloud_game.py").read_text(encoding="utf-8")
        agent_keys = set(re.findall(r'"(cloud_game\.[a-z_]+)"', code))
        interface_keys = set(
            re.findall(r'"\$([\w.]+)"', json.dumps(TASK) + json.dumps(PIPELINE))
        )
        interface_keys |= {"group.CloudGame.label", "controller_cloud_game_front_label"}
        for language in ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr"):
            for kind, keys in (("agent", agent_keys), ("interface", interface_keys)):
                data = json.loads(
                    (
                        ROOT / "assets/resource/locales" / kind / (language + ".json")
                    ).read_text(encoding="utf-8")
                )
                self.assertFalse(
                    keys - data.keys(), (kind, language, keys - data.keys())
                )
                if kind == "agent":
                    self.assertEqual(data["cloud_game.queue_status"].count("%s"), 1)

    def test_resolution_check_uses_parsed_controller_name(self):
        source = ast.parse((ROOT / "agent/main.py").read_text(encoding="utf-8"))
        fn = next(
            node
            for node in source.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_check_game_resolution"
        )
        utils = ModuleType("utils")
        pienv = ModuleType("utils.pienv")
        pienv.controller_name = lambda: "CloudGame-Front"
        win32 = ModuleType("utils.win32_process")
        win32.find_window_by_process = Mock(return_value=None)
        win32.get_client_size = Mock()
        namespace = {"logger": Mock()}
        with patch.dict(
            sys.modules,
            {"utils": utils, "utils.pienv": pienv, "utils.win32_process": win32},
        ):
            exec(
                compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"),
                namespace,
            )
            namespace[fn.name]()
            win32.find_window_by_process.assert_not_called()
            pienv.controller_name = lambda: "Win32-Front"
            namespace[fn.name]()
            win32.find_window_by_process.assert_called_once_with("HTGame.exe")


if __name__ == "__main__":
    unittest.main()
