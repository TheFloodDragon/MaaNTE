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
        all_results = []
        results = []
        if node == "CloudGameQueueText":
            row = 200
            for text in self.current.texts:
                # 标签在左、紧接的数值在右；真实 expected 会先剔除独立数值框。
                labelled = bool(cloud._QUEUE_LABEL.fullmatch(cloud._normalize_text(text)))
                if labelled:
                    row += 60
                box = [100 if labelled else 320, row, 180 if labelled else 120, 30]
                all_results.append(SimpleNamespace(text=text, box=box))
            expected = self.nodes[node]["recognition"]["param"]["expected"]
            results = [result for result in all_results if any(re.search(pattern, result.text) for pattern in expected)]
            hit = bool(results)
        elif hit:
            results = [SimpleNamespace(text="", box=[100, 200, 80, 30])] * (2 if self.current.multiple else 1)
        return SimpleNamespace(hit=hit, box=[100, 200, 80, 30], filtered_results=results, all_results=all_results)

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
        cloud._diagnostics.clear()
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
        # 主窗口点击也走“激活 + 原生点击”（实机证据：未激活的 Qt OpenGL 窗口
        # 丢弃控制器合成输入）。离线测试只替换最底层的原生发送与窗口枚举，
        # _click_main_window 自身的身份校验、坐标换算与错误语义仍真实执行。
        self.native_click_ok = True
        self.native_clicks = []
        self.window_patch = patch.object(
            cloud, "_window_by_hwnd", side_effect=self.fake_window_by_hwnd
        )
        self.window_patch.start()
        self.addCleanup(self.window_patch.stop)
        self.native_click_patch = patch.object(
            cloud, "_native_click", side_effect=self.fake_native_click
        )
        self.native_click_patch.start()
        self.addCleanup(self.native_click_patch.stop)

    def fake_window_by_hwnd(self, hwnd):
        """只认会话锁定的主窗口 1000，客户区与 720p 基准一致（换算为恒等）。"""
        if cloud._hwnd_value(hwnd) != 1000:
            return None
        return cloud._WindowInfo(
            hwnd=1000,
            rect=(0, 0, 1280, 720),
            client_size=(1280, 720),
            owner=0,
            title=cloud._CLOUD_TITLE,
            class_name="Qt51517QWindowOwnDC",
            pid=4242,
        )

    def fake_native_click(self, hwnd, point):
        self.native_clicks.append((hwnd, point))
        if not self.native_click_ok:
            return False
        if self.confirm_context is not None:
            self.confirm_context.click(*point)
        return True

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

    def fake_click_confirm(self, target, context=None):
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

    def test_confirm_click_stays_on_enter_button_at_every_scale(self):
        """弹窗客户区不是 720p 时，换算后的点击仍必须落在“进入游戏”上。

        实机夹具量得（1280x720 基准）：退出启动 x 348..627，进入游戏 x 652..933。
        点错左侧退出按钮会立即终止本次启动，且弹窗只有 30 秒倒计时，
        因此这里对多种客户区尺寸断言换算结果，而不是只验证 720p 这一种。
        """
        # 识别框来自 tests/test_cloud_game_native.py 的真实模板匹配结果。
        centre = cloud._box_center((650, 415, 280, 65))
        self.assertEqual(centre, (790, 447))
        exit_span, enter_span = (348, 627), (652, 933)
        for size in ((1280, 720), (1600, 900), (1024, 576), (1920, 1080), (960, 540)):
            with self.subTest(client=size):
                x, y = cloud._map_720p_to_client(centre, size)
                scale = size[0] / 1280
                self.assertLess(exit_span[1] * scale, x)
                self.assertLess(enter_span[0] * scale, x)
                self.assertLess(x, enter_span[1] * scale)
                self.assertLess(0, y)
                self.assertLess(y, size[1])

    def test_zero_sized_client_is_rejected_instead_of_clamped(self):
        """客户区尺寸不可用时必须报错，不能把坐标压成 0 后盲点左上角。"""
        for size in ((0, 720), (1280, 0), (0, 0), (-1280, -720)):
            with self.subTest(client=size):
                with self.assertRaises(cloud._CloudError):
                    cloud._map_720p_to_client((790, 447), size)

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

        def click(target, context=None):
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

    def test_main_window_clicks_go_through_activation_not_the_controller(self):
        """实机取证：未激活的 Qt OpenGL 窗口会丢弃控制器合成输入。

        2026-09-24 20:49 控制器把 720p (640,548) 正确换算到屏幕 (960,703)，
        但窗口 active/focus 均为 0，客户端日志无新增记录、登录窗口未出现；
        21:24 同一像素改走“激活 + 原生点击”后立刻唤出登录窗口。
        因此主窗口输入必须走原生路径，并落在会话锁定的那个 HWND 上。
        """
        context = self.reset([scene("CloudGameLoginScreen"), scene(*DAILY)])
        self.assertTrue(
            cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success
        )
        # 客户区与 720p 基准一致，换算为恒等；HWND 必须是入口锁定的主窗口。
        self.assertEqual(self.native_clicks, [(1000, (140, 215))])

    def test_main_window_click_is_mapped_to_the_client_resolution(self):
        """非 720p 客户区时，原生点击必须按真实客户区换算，而不是沿用 720p 坐标。"""
        context = self.reset([scene("CloudGameLoginScreen"), scene(*DAILY)])
        with patch.object(
            cloud,
            "_window_by_hwnd",
            return_value=cloud._WindowInfo(
                hwnd=1000,
                rect=(0, 0, 1600, 900),
                client_size=(1600, 900),
                owner=0,
                title=cloud._CLOUD_TITLE,
                class_name="Qt51517QWindowOwnDC",
                pid=4242,
            ),
        ):
            self.assertTrue(
                cloud.CloudGameWaitLogin()
                .run(context, arg("CloudGameLoginWait"))
                .success
            )
        self.assertEqual(self.native_clicks, [(1000, (175, 269))])

    def test_rejected_native_input_fails_without_a_second_attempt(self):
        """激活失败或安全检查拒绝时必须显式失败，不静默继续、也不改用控制器重试。"""
        self.native_click_ok = False
        context = self.reset([scene(*HOME)])
        self.assertFalse(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
            .success
        )
        self.assertTrue(self.messages("cloud_game.input_rejected"))
        self.assertEqual(len(self.native_clicks), 1)
        self.assertEqual(context.clicks, [])

    def test_unverified_main_window_is_never_clicked(self):
        """会话锁定的窗口消失或身份不符时，不得回退到其他窗口发送输入。"""
        context = self.reset([scene(*HOME)])
        with patch.object(cloud, "_window_by_hwnd", return_value=None):
            self.assertFalse(
                cloud.CloudGameClick()
                .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
                .success
            )
        self.assertTrue(self.messages("cloud_game.client_missing"))
        self.assertEqual(self.native_clicks, [])

    def test_enter_click_confirms_immediately_without_a_dispatch_round_trip(self):
        """确认弹窗只有 30 秒倒计时；一旦在本次 enter 点击后的轮询中看到它，必须
        当场确认，不能把动作交还给 Dispatch 节点再走一轮全图 OCR 才轮到确认。"""
        context = self.reset([scene(*HOME), scene(*HOME), scene(*CONFIRM)])
        self.assertTrue(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
            .success
        )
        # 开始游戏点击 + 同一动作内的确认点击。
        self.assertEqual(context.clicks, [(140, 215), (140, 215)])
        self.assertTrue(self.confirm_clicked)
        self.assertFalse(
            cloud.CloudGameClick()
            .run(context, arg("CloudGameEnterButton", {"kind": "enter"}))
            .success
        )
        self.assertEqual(len(context.clicks), 2)

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
        first = ("预计等待时间", "１０～２０分钟", "目前正排在第", "１２")
        later = ("预计等待时间", "5分钟", "目前正排在第", "3")
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


    def test_logging_action_lifecycle_includes_result_and_elapsed(self):
        context = self.reset([scene(*HOME), scene(*HOME)])
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            result = cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait"))
        self.assertTrue(result.success)
        messages = [record.getMessage() for record in captured.records]
        starts = [message for message in messages if "云游戏操作开始" in message]
        ends = [message for message in messages if "云游戏操作结束" in message]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertIn("action=CloudGameWaitLogin.run", starts[0])
        self.assertIn("task_id=42", ends[0])
        self.assertIn("result=succeeded", ends[0])
        self.assertIn("elapsed_s=2.000", ends[0])
        self.assertTrue(all(record.levelno < logging.WARNING for record in captured.records))


    def test_logging_capture_failure_has_specific_cause(self):
        context = self.reset([scene(missing=True)])
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            result = cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait"))
        self.assertFalse(result.success)
        errors = [record for record in captured.records if record.levelno == logging.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("code=cloud_game.client_missing", errors[0].getMessage())
        self.assertIn("reason=screencap_failed", errors[0].getMessage())
        self.assertIn("source=_capture:", errors[0].getMessage())


    def test_logging_unexpected_exception_excludes_sensitive_data(self):
        secret = "PRIVATE-password-token-98765"
        private_path = r"C:\Users\private-account\client.exe"
        context = self.reset([scene(*HOME)])
        with self.assertLogs(cloud.logger, level="DEBUG") as captured, patch.object(
            context, "run_recognition", side_effect=RuntimeError(secret + private_path)
        ):
            result = cloud.CloudGameClick().run(
                context,
                arg("private-node-98765", {"kind": "enter", "password": secret, "token": secret}),
            )
        self.assertFalse(result.success)
        output = "\n".join(captured.output)
        self.assertIn("error=RuntimeError", output)
        self.assertIn("reason=unexpected_exception", output)
        for value in (secret, private_path, "private-node-98765"):
            self.assertNotIn(value, output)
            self.assertNotIn(value, repr([record.args for record in captured.records]))
        self.assertTrue(all(record.exc_info is None for record in captured.records))
        self.assertIsNone(cloud._session)


    def test_logging_invalid_option_names_the_field_not_its_value(self):
        secret = "PRIVATE-token-in-config"
        context = FakeContext([scene(*HOME)])
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            result = cloud.CloudGameReset().run(
                context, arg("CloudGameStartEntrance", {"poll_interval_seconds": secret})
            )
        self.assertFalse(result.success)
        output = "\n".join(captured.output)
        self.assertIn("option=poll_interval_seconds", output)
        self.assertIn("reason=invalid_numeric_option", output)
        self.assertNotIn(secret, output)


    def test_logging_stop_is_not_reported_as_error(self):
        context = self.reset([scene("CloudGameLoading")])
        self.clock.on_sleep = lambda: setattr(context.tasker, "stopping", True)
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            result = cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait"))
            cloud.CloudGameFail().run(context, arg("CloudGameStartFail"))
        self.assertFalse(result.success)
        self.assertIn("result=stopped", "\n".join(captured.output))
        self.assertTrue(all(record.levelno < logging.WARNING for record in captured.records))


    def test_logging_login_state_is_not_repeated_each_poll(self):
        context = self.reset(
            [scene("CloudGameVerification")] * 8 + [scene(*HOME)] * 2,
            login_timeout_seconds=30,
        )
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            result = cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait"))
        self.assertTrue(result.success)
        states = [record.getMessage() for record in captured.records if "phase=login" in record.getMessage()]
        self.assertEqual(len(states), 2)
        self.assertIn("to=cloud_game.verification_required", states[0])
        self.assertIn("to=CloudGameHome", states[1])


    def test_logging_queue_reports_transitions_without_ocr_text(self):
        secret = "PRIVATE-ocr-token-12345"
        context = self.reset(
            [scene("CloudGameQueueScreen", texts=("预计等待 " + secret, "4~8 分钟"))] * 12
            + [scene("CloudGameLoading")] * 4
            + [scene(*WORLD)]
        )
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            result = cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait"))
        self.assertTrue(result.success)
        output = "\n".join(captured.output)
        states = [record.getMessage() for record in captured.records if "phase=queue" in record.getMessage()]
        self.assertEqual(len(states), 3)
        for message, status in zip(states, ("CloudGameQueueScreen", "CloudGameLoading", "CloudGameInWorld")):
            self.assertIn("to=" + status, message)
        # 含凭据片段的标签整行拒绝，既不写日志也不向用户播报。
        self.assertEqual(output.count("云游戏队列提示更新"), 0)
        self.assertEqual(self.messages("cloud_game.queue_status"), [])
        self.assertNotIn(secret, output)
        self.assertNotIn("4~8 分钟", output)


    def test_logging_click_records_kind_and_transition(self):
        context = self.reset([scene(*HOME), scene("CloudGameQueueScreen")])
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            result = cloud.CloudGameClick().run(context, arg("CloudGameEnter", {"kind": "enter"}))
        self.assertTrue(result.success)
        output = "\n".join(captured.output)
        self.assertIn("kind=enter", output)
        self.assertIn("phase=click_enter", output)
        self.assertIn("to=CloudGameQueueScreen", output)
        self.assertIn("result=succeeded", output)
        self.assertEqual(len(self.native_clicks), 1)


    def test_logging_owned_probe_deduplicates_failure_and_reports_recovery(self):
        secret = "PRIVATE-window-title-token"
        context = self.reset([scene(*CONFIRM)])
        window = cloud._WindowInfo(
            hwnd=1001, rect=(0, 8, 1280, 728), client_size=(1280, 720),
            owner=1000, title=secret, class_name="Qt51517QWindowToolSaveBits", pid=7,
        )
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            with patch.object(context, "run_recognition", side_effect=RuntimeError(secret)):
                for _ in range(8):
                    self.assertIsNone(cloud._recognize_owned_confirm(context, window, context.image, *CONFIRM))
            for _ in range(8):
                self.assertIsNotNone(cloud._recognize_owned_confirm(context, window, context.image, *CONFIRM))
        self.assertEqual(len(captured.records), 2)
        self.assertIn("弹窗识别异常", captured.records[0].getMessage())
        self.assertIn("button_count=1", captured.records[1].getMessage())
        self.assertNotIn(secret, "\n".join(captured.output))


    def test_logging_diagnostic_cache_is_bounded_and_session_scoped(self):
        with patch.object(cloud.logger, "debug") as log:
            for index in range(cloud._MAX_DIAGNOSTICS + 10):
                cloud._log_diagnostic(("probe", index), (False,), "probe=%s", False)
            self.assertLessEqual(len(cloud._diagnostics), cloud._MAX_DIAGNOSTICS)
            log.reset_mock()
            for _ in range(10):
                cloud._log_diagnostic(("repeat",), (False,), "probe=%s", False)
            cloud._log_diagnostic(("repeat",), (True,), "probe=%s", True)
            self.assertEqual(log.call_count, 2)
            cloud.cleanup_cloud_session()
            self.assertFalse(cloud._diagnostics)
            cloud._log_diagnostic(("repeat",), (True,), "probe=%s", True)
            self.assertEqual(log.call_count, 3)


    def test_logging_finish_records_result_and_releases_diagnostics(self):
        context = self.reset([scene(*WORLD)])
        cloud._log_state(cloud._session, "ready", "CloudGameInWorld")
        cloud._diagnostics[("probe",)] = (True,)
        with self.assertLogs(cloud.logger, level="DEBUG") as captured:
            result = cloud.CloudGameFinish().run(context, arg("CloudGameStartDone"))
            cloud.cleanup_cloud_session()
        self.assertTrue(result.success)
        output = "\n".join(captured.output)
        self.assertIn("云游戏流程结束 | result=succeeded", output)
        self.assertEqual(output.count("云游戏会话清理"), 1)
        self.assertIsNone(cloud._session)
        self.assertFalse(cloud._diagnostics)

    def test_ready_rejects_world_behind_unknown_owned_window(self):
        context = self.reset([scene(*WORLD)], transition_timeout_seconds=2)
        with patch.object(cloud, "_owned_dialog_candidates", return_value=[self.owned_window()]):
            self.assertFalse(cloud.CloudGameConfirmReady().run(context, arg("CloudGameReady")).success)
        self.assertEqual(context.clicks, [])
        self.assertIsNone(cloud._session)

    def test_main_click_rejects_owned_overlay(self):
        context = self.reset([scene(*HOME)])
        with patch.object(cloud, "_owned_dialog_candidates", return_value=[self.owned_window()]):
            with self.assertRaises(cloud._CloudError):
                cloud._click_main_window(context, cloud._session, (100, 200))
        self.assertEqual(self.native_clicks, [])

    def test_login_target_must_be_globally_unique_and_unobstructed(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                context = self.reset([scene(*HOME)])
                def recognize(node, image):
                    hit = image == 1 and node in ("CloudGameLoginOtherMethods", "CloudGameLoginSubmit")
                    return SimpleNamespace(hit=hit, box=[100, 200, 80, 30], filtered_results=[SimpleNamespace()] if hit else [])
                with patch.object(cloud, "_owned_dialog_candidates", return_value=[self.owned_window(), self.owned_window(1002)]), patch.object(
                    cloud, "_capture_owned_window", side_effect=[1, None if unknown else 1]
                ), patch.object(context, "run_recognition", side_effect=recognize):
                    message, target = cloud._login_obstacle(context, 0)
                self.assertIsNone(target)
                self.assertIsNotNone(message)
                self.assertEqual(context.clicks, [])

    def test_ready_honors_existing_queue_deadline(self):
        context = self.reset([scene(*WORLD)])
        cloud._session.queue_deadline = self.clock.now + 0.5
        self.assertFalse(cloud.CloudGameConfirmReady().run(context, arg("CloudGameReady")).success)
        self.assertTrue(self.messages("cloud_game.queue_timeout"))

    def test_login_existing_confirmation_routes_without_slow_dispatch(self):
        context = self.reset([scene(*HOME, *CONFIRM)])
        self.assertTrue(cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success)
        self.assertEqual(context.nodes["CloudGameLoginWait"]["next"], ["CloudGameStartConfirm"])
        self.assertEqual(context.clicks, [])
        self.assertIsNotNone(cloud._session.confirm_target)

    def test_queue_confirmation_routes_directly(self):
        context = self.reset([scene(*CONFIRM)])
        self.assertTrue(cloud.CloudGameQueueWait().run(context, arg("CloudGameQueueWait")).success)
        self.assertEqual(context.nodes["CloudGameQueueWait"]["next"], ["CloudGameStartConfirm"])

    def test_owned_capture_is_single_attempt_and_bounded(self):
        context = self.reset([scene(*HOME)])
        module = ModuleType("utils.cloud_window")
        module.capture_window = Mock(return_value=None)
        with patch.dict(sys.modules, {"utils.cloud_window": module}):
            self.assertIsNone(cloud._capture_owned_window(self.owned_window(), context))
        module.capture_window.assert_called_once()
        self.assertLessEqual(module.capture_window.call_args.kwargs["timeout"], 3)
        self.assertFalse(module.capture_window.call_args.kwargs["stopped"]())

    def test_owned_capture_does_not_start_after_cancellation(self):
        context = self.reset([scene(*HOME)])
        context.tasker.stopping = True
        module = ModuleType("utils.cloud_window")
        module.capture_window = Mock()
        with patch.dict(sys.modules, {"utils.cloud_window": module}), self.assertRaises(cloud._CloudError):
            cloud._capture_owned_window(self.owned_window(), context)
        module.capture_window.assert_not_called()

    def test_queue_real_rank_text_is_not_dropped(self):
        self.assertEqual(cloud._queue_status(["目前正排在第 12 / 350 名", "预计等待 3-5 分钟"]),
                         "目前正排在第 12 / 350 名 | 预计等待 3-5 分钟")

    def test_remembered_target_changed_to_credentials_is_not_submitted(self):
        context = self.reset([scene(*HOME)], login_timeout_seconds=1)
        target = cloud._ConfirmTarget(1001, 1000, (0, 0, 1280, 720), (1280, 720), (100, 200, 80, 30))
        with patch.object(cloud, "_login_obstacle", side_effect=[
            ("cloud_game.login_remembered", target), ("cloud_game.login_required", None)
        ]), patch.object(cloud, "_click_owned_target") as click:
            self.assertFalse(cloud.CloudGameWaitLogin().run(context, arg("CloudGameLoginWait")).success)
        click.assert_not_called()
        self.assertEqual(context.clicks, [])


    def test_late_recognition_is_rejected_before_input(self):
        context = self.reset([scene(*HOME)], transition_timeout_seconds=1)
        recognize = context.run_recognition
        def slow(node, image):
            self.clock.now += 2
            return recognize(node, image)
        with patch.object(context, "run_recognition", side_effect=slow):
            self.assertFalse(cloud.CloudGameClick().run(context, arg("CloudGameEnterButton", {"kind": "enter"})).success)
        self.assertEqual(self.native_clicks, [])


    def test_queue_values_require_nearby_labels(self):
        rank = SimpleNamespace(text="目前正排在第", box=[200, 200, 200, 30])
        details = SimpleNamespace(filtered_results=[rank], all_results=[
            rank, SimpleNamespace(text="12 / 350名", box=[410, 200, 130, 30]),
            SimpleNamespace(text="99887766", box=[410, 245, 150, 30]),
            SimpleNamespace(text="77", box=[1000, 650, 50, 30]),
            SimpleNamespace(text="token=fake-token", box=[410, 210, 150, 30]),
        ])
        status = cloud._queue_status(cloud._queue_texts(details))
        self.assertIn("12 / 350名", status)
        self.assertNotIn("99887766", status)
        self.assertNotIn("77", status)
        self.assertNotIn("token", status)


    def test_owned_click_rechecks_budget_after_identity_queries(self):
        self.click_confirm_patch.stop()
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                context = self.reset([scene(*HOME)])
                cloud._session.operation_deadline = self.clock.now + 1
                item = self.owned_window()
                target = cloud._ConfirmTarget(item.hwnd, item.owner, item.rect, item.client_size, (100, 200, 80, 30))
                def changed(_):
                    if cancelled:
                        context.tasker.stopping = True
                    else:
                        self.clock.now += 2
                    return item
                with patch.object(cloud, "_owned_dialog_candidates", return_value=[item]), patch.object(
                    cloud, "_target_window_info", side_effect=changed
                ), self.assertRaises(cloud._CloudError):
                    cloud._click_owned_target(target, context)
                self.assertEqual(self.native_clicks, [])


    def test_main_click_rechecks_deadline_after_overlay_query(self):
        context = self.reset([scene(*HOME)])
        cloud._session.operation_deadline = self.clock.now + 1
        def slow():
            self.clock.now += 2
            return []
        with patch.object(cloud, "_owned_dialog_candidates", side_effect=slow), self.assertRaises(cloud._CloudError):
            cloud._click_main_window(context, cloud._session, (100, 200))
        self.assertEqual(self.native_clicks, [])


    def test_queue_free_text_with_sensitive_suffix_is_not_reported(self):
        self.assertNotIn("fake-token", cloud._queue_status(["预计等待 token=fake-token"]))


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
        # 主路径模板已按实机(1280x720)重拍验证并跑通，校准门槛已开启。
        self.assertTrue(PIPELINE["CloudGameProfile"]["attach"]["calibrated"])
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
