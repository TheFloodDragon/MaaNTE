"""使用 MaaFramework 5.10.4 原生调度器验证云启动图；合成画面不代表实机 OCR 已通过。"""

import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import maa
from maa.library import Library
import numpy as np
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.define import LoggingLevelEnum
from maa.resource import Resource
from maa.tasker import Tasker
from PIL import Image

from test_cloud_game import PIPELINE, ROOT, TASK, cloud

# 导入 maa.agent 会选择 AgentServer 库；此测试需要真实调度器而不是 IPC 代理。
Library.open(
    Path(os.environ.get("MAAFW_BINARY_PATH", Path(maa.__file__).parent / "bin")),
    agent_server=False,
)

STATES = {
    1: {"CloudGameLoginScreen"},
    2: {"CloudGameHomeScreen", "CloudGameEnterText"},
    # 客户端没有队列选择页：确认启动后直接进入排队。
    4: {"CloudGameQueueScreen"},
    5: {"CloudGameLoading"},
    6: {"InWorld"},
    7: {"CloudGameDailyLoginTitle", "CloudGameDailyLoginCloseButton"},
    8: {"CloudGameErrorState"},
    9: {"CloudGameGameLogin"},
    # 任何识别节点都不命中的云外壳画面，用于验证未知状态会安全超时。
    10: set(),
    # 启动确认弹窗是独立顶层窗口：主窗口仍停留在首页，弹窗画面单独取帧。
    12: {"CloudGameStartConfirmNotice", "CloudGameStartConfirmEnter"},
}

# 主窗口画面之外的 owned 弹窗画面标记，必须与主窗口状态区分开。


def _configure_maa_once():
    """MaaFramework 5.10.4 的日志是异步线程；在用例之间反复 set_log_dir 会与
    尚未退出的工作线程竞争，导致随机的原生 abort。因此全局选项只设置一次，
    日志目录固定在 .narrafork，不放入待删除的临时资源目录。"""
    Tasker.set_log_dir(ROOT / ".narrafork")
    Tasker.set_stdout_level(LoggingLevelEnum.Off)
    Tasker.set_save_draw(False)
    Tasker.set_save_on_error(False)
    Tasker.set_debug_mode(False)


_configure_maa_once()
DIALOG_MARKER = 12
LOGIN_NODES = (
    "CloudGameLoginForm",
    "CloudGameVerification",
    "CloudGameAuthorization",
    "CloudGameLoginFailure",
)
HOME_TEMPLATE_LAYOUTS = {
    # 当前 720p 首页裁剪坐标；测试仅拼入无账号信息的模板，不读取个人截图。
    "current_720p": {
        "CloudGameHomeScreen": ("HomeSettingsIcon720.png", (1046, 76, 45, 45)),
        "CloudGameEnterText": ("StartGame720.png", (965, 568, 91, 29)),
    },
    # 旧坐标来自仓库夹具 tests/fixtures/cloud_game/home_720p.png 的精确模板匹配；
    # 测试同样只用模板合成帧，不依赖这张完整截图。
    "legacy": {
        "CloudGameHomeScreen": ("HomeSettingsIcon.png", (988, 65, 56, 56)),
        "CloudGameEnterText": ("StartGameSmall.png", (888, 681, 110, 31)),
    },
}


class VirtualCloudController(CustomController):
    """主窗口与 owned 弹窗分开建模，复现云客户端 1.0.0.1 的真实窗口结构。"""

    def __init__(self, stage=7, game_login=False):
        self.stage = stage
        self.game_login = game_login
        self.dialog = stage == DIALOG_MARKER
        if self.dialog:
            self.stage = 2
        self.queue_frames = 0
        self.clicks = []
        self.world_navigation = 0
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        super().__init__()

    def connect(self):
        return True

    def request_uuid(self):
        return "cloud-offline-test"

    def get_features(self):
        return 0

    def _frame(self, marker):
        self.image[0, 0, 0] = marker
        return self.image.copy()

    def dialog_frame(self):
        """模拟独立窗口取帧：弹窗不存在时返回 None，绝不退回主窗口画面。"""
        return self._frame(DIALOG_MARKER) if self.dialog else None

    def screencap(self):
        if self.stage == 4:
            self.queue_frames += 1
            if self.queue_frames >= 4:
                self.stage = 9 if self.game_login else 6
        # 弹窗弹出时主窗口仍显示首页，控制器截图看不到弹窗。
        return self._frame(self.stage)

    def click(self, x, y):
        self.clicks.append((DIALOG_MARKER if self.dialog else self.stage, x, y))
        if self.dialog:
            # 确认弹窗关闭后直接进入排队，没有队列选择页。
            self.dialog = False
            self.stage = 4
            return True
        if self.stage == 2:
            # 首页点击只弹出独立确认窗口，主窗口不变。
            self.dialog = True
            return True
        # 真实流程：每日弹窗 -> 首页 -> (确认弹窗) -> 排队
        self.stage = {7: 2}.get(self.stage, self.stage)
        return True


class SyntheticCloudRecognition(CustomRecognition):
    def analyze(self, context, argv):
        state = int(argv.image[0, 0, 0])
        node = json.loads(argv.custom_recognition_param)["node"]
        if node in STATES.get(state, set()):
            return self.AnalyzeResult(box=[100, 200, 80, 30], detail={})
        return None


class VirtualEnterWorld(CustomAction):
    def __init__(self, controller):
        self.controller = controller
        super().__init__()

    def run(self, context, argv):
        self.controller.world_navigation += 1
        self.controller.stage = 6
        return True


class NativeCloudFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="cloud-native-", dir=ROOT / ".narrafork"
        )
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        # 全局日志/调试选项只在模块加载时设置一次（见 _configure_maa_once）。
        cloud._session = None
        cloud.PrintT.reset_mock()
        self.bundles = 0

    def run_flow(self, stage=7, calibrated=True, auto_queue=True, game_login=False):
        data = copy.deepcopy(PIPELINE)
        data["CloudGameProfile"]["attach"]["calibrated"] = calibrated
        # 登录障碍节点也改为合成识别，避免在没有 OCR 模型的 bundle 中真实运行 OCR。
        raw_nodes = set().union(*STATES.values()) | set(LOGIN_NODES)
        for name in raw_nodes:
            data.setdefault(name, {})
            data[name]["recognition"] = {
                "type": "Custom",
                "param": {
                    "custom_recognition": "synthetic_cloud_scene",
                    "custom_recognition_param": {"node": name},
                },
            }
            data[name]["inverse"] = False
        data["SceneAnyEnterWorld"] = {
            "action": {
                "type": "Custom",
                "param": {"custom_action": "test_enter_world"},
            },
            "pre_delay": 0,
            "post_delay": 0,
            "rate_limit": 0,
        }
        params = data["CloudGameStartEntrance"]["action"]["param"][
            "custom_action_param"
        ]
        params["poll_interval_seconds"] = 0.01
        params["transition_timeout_seconds"] = 1
        params["login_timeout_seconds"] = 1
        bundle = self.make_bundle(data)
        controller = VirtualCloudController(stage, game_login)
        confirm_target = cloud._ConfirmTarget(
            hwnd=1001,
            owner=1000,
            rect=(0, 0, 1280, 720),
            client_size=(1280, 720),
            box=(100, 200, 80, 30),
        )

        def find_confirm(_context, _screen, _target, expected_hwnd=None):
            if not controller.dialog:
                return None
            cloud._confirm_target = confirm_target
            if cloud._session is not None:
                cloud._session.confirm_target = confirm_target
            return confirm_target

        def click_confirm(target):
            if target != confirm_target:
                return False
            return controller.click(
                target.box[0] + target.box[2] // 2,
                target.box[1] + target.box[3] // 2,
            )

        def confirm_state(_target):
            return "present" if controller.dialog else "gone"

        def prepare(_context, state):
            # 真实调度器下没有本地云窗口；只提供会话锁定的主窗口句柄，
            # 让 _click_main_window 的身份校验与坐标换算仍真实执行。
            state.main_hwnd = 1000

        def main_window(hwnd):
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

        def native_click(hwnd, point):
            # 主窗口点击改走原生输入后，虚拟控制器仍需观察到这次点击才能推进画面。
            if cloud._hwnd_value(hwnd) != 1000:
                return False
            return controller.click(*point)

        resource = Resource()
        callbacks = {
            "cloud_game_reset": cloud.CloudGameReset(),
            "cloud_game_wait_login": cloud.CloudGameWaitLogin(),
            "cloud_game_queue_wait": cloud.CloudGameQueueWait(),
            "cloud_game_click": cloud.CloudGameClick(),
            "cloud_game_confirm_ready": cloud.CloudGameConfirmReady(),
            "cloud_game_fail": cloud.CloudGameFail(),
            "cloud_game_finish": cloud.CloudGameFinish(),
            "test_enter_world": VirtualEnterWorld(controller),
        }
        for name, action in callbacks.items():
            self.assertTrue(resource.register_custom_action(name, action))
        recognizers = {
            "synthetic_cloud_scene": SyntheticCloudRecognition(),
            # 用真实的 owned 弹窗识别，验证它能从独立窗口拿到可点击坐标。
            "cloud_game_owned_dialog": cloud.CloudGameOwnedDialog(),
        }
        for name, recognizer in recognizers.items():
            self.assertTrue(resource.register_custom_recognition(name, recognizer))
        self.assertTrue(resource.post_bundle(bundle).wait().succeeded)
        self.assertTrue(controller.post_connection().wait().succeeded)
        tasker = Tasker()
        self.assertTrue(tasker.bind(resource, controller))
        cases = TASK["option"]["CloudGameAutoQueue"]["cases"]
        overrides = copy.deepcopy(cases[0 if auto_queue else 1]["pipeline_override"])
        # 使用真实任务的独立配置节点，超时覆盖不能恢复关闭的自动排队开关。
        time_override = json.loads(
            json.dumps(
                TASK["option"]["CloudGameQueueTimeout"]["pipeline_override"]
            ).replace('"{minutes}"', "7")
        )
        overrides.update(time_override)
        with ExitStack() as stack:
            # 真实调度器下同样没有本地云窗口；入口的本地就绪检查由 test_cloud_start 覆盖。
            stack.enter_context(
                patch.object(cloud, "_prepare_cloud_client", side_effect=prepare)
            )
            # 主窗口点击走“激活 + 原生点击”，这里只替换窗口枚举与最底层的原生发送，
            # _click_main_window 的身份校验、坐标换算与错误语义仍真实执行。
            stack.enter_context(
                patch.object(cloud, "_window_by_hwnd", side_effect=main_window)
            )
            stack.enter_context(
                patch.object(cloud, "_native_click", side_effect=native_click)
            )
            stack.enter_context(
                patch.object(cloud, "_owned_dialog_candidates", return_value=[])
            )
            # 结束节点会释放会话；这里保留会话以便断言解析后的参数。
            stack.enter_context(
                patch.object(cloud, "cleanup_cloud_session", lambda: None)
            )
            stack.enter_context(
                patch.object(cloud, "_find_owned_confirm", side_effect=find_confirm)
            )
            stack.enter_context(
                patch.object(cloud, "_click_owned_target", side_effect=click_confirm)
            )
            stack.enter_context(
                patch.object(cloud, "_confirm_target_state", side_effect=confirm_state)
            )
            job = tasker.post_task("CloudGameStartEntrance", overrides).wait()
        result = (job.succeeded, list(controller.clicks), controller.world_navigation)
        self.session = cloud._session
        self.last_params = resource.get_node_data("CloudGameStartEntrance")
        tasker.post_stop().wait()
        # 显式按 Tasker -> Resource -> Controller 顺序释放，不在这里重设全局日志选项。
        del tasker
        del resource
        del controller
        return result

    def test_daily_home_queue_world_runs_end_to_end(self):
        success, clicks, navigation = self.run_flow()
        self.assertTrue(success)
        # 每日弹窗 -> 开始游戏 -> 启动确认，各点击一次；排队页只读不点击
        self.assertEqual([stage for stage, _, _ in clicks], [7, 2, DIALOG_MARKER])
        self.assertEqual(navigation, 0)
        self.assertEqual(cloud._session.timeout_minutes, 7)
        self.assertEqual(cloud._session.poll_interval, 0.01)
        self.assertTrue(cloud._session.auto_queue)

    def test_auto_queue_off_does_not_enter(self):
        success, clicks, _ = self.run_flow(stage=2, auto_queue=False)
        self.assertTrue(success)
        self.assertEqual(clicks, [])
        self.assertFalse(cloud._session.auto_queue)

    def test_uncalibrated_profile_is_a_failed_task(self):
        success, clicks, _ = self.run_flow(calibrated=False)
        self.assertFalse(success)
        self.assertEqual(clicks, [])

    def test_unknown_shell_screen_is_a_failed_task(self):
        # 未取证的云外壳画面不能触发任何点击，只能在过渡超时后安全失败。
        success, clicks, _ = self.run_flow(stage=10)
        self.assertFalse(success)
        self.assertEqual(clicks, [])

    def test_already_in_world_needs_no_input(self):
        success, clicks, _ = self.run_flow(stage=6)
        self.assertTrue(success)
        self.assertEqual(clicks, [])

    def test_game_login_hands_off_to_public_scene_interface(self):
        success, clicks, navigation = self.run_flow(stage=2, game_login=True)
        self.assertTrue(success)
        self.assertEqual([stage for stage, _, _ in clicks], [2, DIALOG_MARKER])
        self.assertEqual(navigation, 1)

    def make_bundle(self, nodes):
        """为每次调用建立独立 bundle，便于同一测试内多次识别真实截图。"""
        bundle = self.path / f"bundle{self.bundles}"
        self.bundles += 1
        pipeline_dir = bundle / "pipeline"
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "cloud.json").write_text(json.dumps(nodes), encoding="utf-8")
        image_dir = bundle / "image/CloudGame"
        image_dir.mkdir(parents=True)
        for path in (ROOT / "assets/resource/base/image/CloudGame").glob("*.png"):
            (image_dir / path.name).write_bytes(path.read_bytes())
        return bundle

    def assert_real_frame(
        self, frame, expected, node="CloudGameLoginScreen", expected_box=None
    ):
        class LoginProbe(CustomAction):
            detail = None
            shape = None

            def run(probe, context, argv):
                controller = context.tasker.controller
                if not controller.post_screencap().wait().succeeded:
                    return False
                image = controller.cached_image
                probe.shape = image.shape
                probe.detail = context.run_recognition(node, image)
                return probe.detail is not None

        data = {
            name: copy.deepcopy(PIPELINE[name])
            for name in (
                "CloudGameLoginScreen",
                "CloudGameStartConfirmNotice",
                "CloudGameStartConfirmEnter",
                "CloudGameHomeScreen",
                "CloudGameEnterText",
                "CloudGameHome",
            )
        }
        data["LoginProbe"] = {
            "action": {
                "type": "Custom",
                "param": {"custom_action": "test_login_probe"},
            },
            "pre_delay": 0,
            "post_delay": 0,
            "rate_limit": 0,
        }
        bundle = self.make_bundle(data)
        resource = Resource()
        probe = LoginProbe()
        self.assertTrue(resource.register_custom_action("test_login_probe", probe))
        self.assertTrue(resource.post_bundle(bundle).wait().succeeded)
        controller = VirtualCloudController(stage=0)
        controller.image = frame.copy()
        self.assertTrue(controller.post_connection().wait().succeeded)
        tasker = Tasker()
        self.assertTrue(tasker.bind(resource, controller))
        try:
            self.assertTrue(tasker.post_task("LoginProbe").wait().succeeded)
            self.assertEqual(probe.shape[:2], (720, 1280))
            self.assertIsNotNone(probe.detail)
            self.assertEqual(probe.detail.hit, expected)
            algorithm = "And" if node == "CloudGameHome" else "TemplateMatch"
            self.assertEqual(probe.detail.algorithm, algorithm)
            if expected and algorithm == "TemplateMatch":
                self.assertGreaterEqual(probe.detail.best_result.score, 0.85)
            if expected_box is not None:
                self.assertEqual(tuple(probe.detail.box), expected_box)
            if not expected:
                self.assertIsNone(probe.detail.box)
            self.assertEqual(controller.clicks, [])
        finally:
            tasker.post_stop().wait()

    def make_home_frame(self, templates, missing=None):
        """只在合成背景上贴入版本化模板，不依赖窗口、OCR 模型或完整截图。"""
        frame = np.full((720, 1280, 3), 24, dtype=np.uint8)
        for node, (name, box) in templates.items():
            if node == missing:
                continue
            with Image.open(
                ROOT / "assets/resource/base/image/CloudGame" / name
            ) as image:
                template = np.asarray(image.convert("RGB"))[:, :, ::-1]
            x, y, width, height = box
            self.assertEqual(template.shape, (height, width, 3))
            frame[y : y + height, x : x + width] = template
        return frame

    def test_home_templates_match_current_and_legacy_layouts(self):
        for layout, templates in HOME_TEMPLATE_LAYOUTS.items():
            frame = self.make_home_frame(templates)
            for node, (_, box) in templates.items():
                with self.subTest(layout=layout, node=node):
                    self.assert_real_frame(frame, True, node, expected_box=box)
            with self.subTest(layout=layout, node="CloudGameHome"):
                # And 必须返回开始按钮而不是设置图标的框，保证后续点击目标正确。
                self.assert_real_frame(
                    frame,
                    True,
                    "CloudGameHome",
                    expected_box=templates["CloudGameEnterText"][1],
                )

    def test_home_requires_both_settings_and_start_button(self):
        for layout, templates in HOME_TEMPLATE_LAYOUTS.items():
            for missing in templates:
                with self.subTest(layout=layout, missing=missing):
                    frame = self.make_home_frame(templates, missing=missing)
                    for node, (_, box) in templates.items():
                        self.assert_real_frame(
                            frame,
                            node != missing,
                            node,
                            expected_box=box if node != missing else None,
                        )
                    self.assert_real_frame(frame, False, "CloudGameHome")

    def load(self, name):
        return np.asarray(
            Image.open(ROOT / "tests/fixtures/cloud_game" / name).convert("RGB")
        )[:, :, ::-1]

    def test_real_720p_login_prompt_is_not_an_authenticated_home(self):
        self.assert_real_frame(self.load("login_required_720p.png"), True)

    def test_real_120dpi_frame_matches_after_controller_normalization(self):
        self.assert_real_frame(self.load("login_required_120dpi.png"), True)

    def test_current_125dpi_login_prompt_returns_its_box(self):
        # 当前登录文字顶部位于 y=532，旧 y=550 的 ROI 会截断模板。
        self.assert_real_frame(
            self.load("login_required_current_125dpi.png"),
            True,
            expected_box=(538, 532, 204, 32),
        )

    def test_black_loading_frame_is_not_a_login_prompt(self):
        self.assert_real_frame(np.zeros((720, 1280, 3), dtype=np.uint8), False)

    def test_real_start_confirmation_dialog_is_recognized(self):
        image = self.load("start_confirm_720p.png")
        for node in ("CloudGameStartConfirmNotice", "CloudGameStartConfirmEnter"):
            with self.subTest(node=node):
                self.assert_real_frame(image, True, node)
        # 启动确认弹窗不得被当成登录页，否则会误报需要重新登录。
        self.assert_real_frame(image, False, "CloudGameLoginScreen")

    def test_confirm_enter_roi_excludes_the_exit_button(self):
        """“退出启动”会立即终止本次启动，因此确认 ROI 不能与它有任何重叠。

        实机夹具量得（1280x720 基准，含抗锯齿圆角）：退出按钮 x 348..627，
        进入按钮 x 652..933，两者间隙仅 25px。ROI 左边界 636 落在该间隙内。
        这里断言几何关系而不只是“能命中”，避免以后调 ROI 时重新把退出按钮圈进来。
        """
        image = self.load("start_confirm_720p.png")
        roi = PIPELINE["CloudGameStartConfirmEnter"]["recognition"]["param"]["roi"]
        roi_left, roi_right = roi[0], roi[0] + roi[2]
        # 从夹具自身测量按钮列，不写死坐标。用整段按钮高度的逐列最大值：
        # 窄行均值会被按钮上的深色文字拉低，把浅色的“进入游戏”整段判成背板。
        # 阈值 80 介于背板(36)与两个按钮之间；取 100 会让偏暗的退出按钮碎成短段。
        row = image[415:480].max(axis=(0, 2))
        spans, start = [], None
        for x, lit in enumerate(row > 80):
            if lit and start is None:
                start = x
            elif not lit and start is not None:
                if x - start > 100:
                    spans.append((start, x))
                start = None
        self.assertEqual(len(spans), 2, spans)
        (_, exit_right), (enter_left, enter_right) = spans
        self.assertLessEqual(exit_right, roi_left)
        self.assertLess(roi_left, enter_left)
        self.assertLessEqual(enter_right, roi_right)
        # 命中框必须落在进入按钮上，而不是间隙或退出按钮。
        self.assert_real_frame(
            image,
            True,
            "CloudGameStartConfirmEnter",
            expected_box=(650, 415, 280, 65),
        )

    def _as_raw_capture(self, frame, size):
        """把 720p 夹具重采样成给定客户区尺寸，模拟独立弹窗窗口的原始取帧。"""
        rgb = Image.fromarray(frame[:, :, ::-1], "RGB").resize(
            size, Image.Resampling.LANCZOS
        )
        return np.asarray(rgb)[:, :, ::-1].copy()

    def test_owned_dialog_normalization_keeps_the_enter_button_clickable(self):
        """弹窗客户区不是 720p 时，归一化 + 识别 + 反向换算仍须指向“进入游戏”。

        确认弹窗是与主窗口分离的独立顶层窗口，取帧尺寸由它自己的客户区决定；
        实机探针见过 (900, 1600, 3)。因此画面要先归一化到 720p 才能用 720p 的
        ROI 识别，命中框又要按客户区尺寸换算回去点击。这里覆盖整条往返链路：
        非 16:9 尺寸在归一化时会被拉伸变形，是其中最薄弱的一环。
        """
        fixture = self.load("start_confirm_720p.png")
        enter_left, enter_right = 652, 933
        sizes = (
            (1280, 720),  # 与基准一致，不重采样
            (1600, 900),
            (1024, 576),
            (960, 540),
            (1280, 800),  # 以下为非 16:9，归一化会改变长宽比
            (1280, 1024),
            (1440, 720),
            (1100, 800),
        )
        for size in sizes:
            with self.subTest(client=size):
                normalized = cloud._normalize_owned_frame(
                    self._as_raw_capture(fixture, size)
                )
                self.assertIsNotNone(normalized)
                self.assertEqual(normalized.shape, (720, 1280, 3))
                # 两次 LANCZOS 重采样后模板仍须命中，且命中框保持稳定。
                self.assert_real_frame(
                    normalized,
                    True,
                    "CloudGameStartConfirmEnter",
                    expected_box=(650, 415, 280, 65),
                )
                x, y = cloud._map_720p_to_client(
                    cloud._box_center((650, 415, 280, 65)), size
                )
                scale = size[0] / 1280
                self.assertLess(enter_left * scale, x)
                self.assertLess(x, enter_right * scale)
                self.assertLess(0, y)
                self.assertLess(y, size[1])

    def test_unusable_owned_frames_are_rejected_rather_than_guessed(self):
        """取帧结果不可用时必须返回 None，不能拿残帧当弹窗画面去识别。"""
        for bad in (
            None,
            "not-an-array",
            np.zeros((0, 0, 3), dtype=np.uint8),
            np.zeros((720, 1280), dtype=np.uint8),
            np.zeros((2, 720, 1280), dtype=np.uint8),
            np.zeros((720, 1280, 2), dtype=np.uint8),
        ):
            with self.subTest(frame=type(bad).__name__):
                self.assertIsNone(cloud._normalize_owned_frame(bad))

    def test_login_prompt_is_not_a_start_confirmation(self):
        for fixture in ("login_required_720p.png", "login_required_current_125dpi.png"):
            for node in ("CloudGameStartConfirmNotice", "CloudGameStartConfirmEnter"):
                with self.subTest(fixture=fixture, node=node):
                    self.assert_real_frame(self.load(fixture), False, node)

    def test_real_home_is_recognized(self):
        for node in ("CloudGameHomeScreen", "CloudGameEnterText"):
            with self.subTest(node=node):
                self.assert_real_frame(self.load("home_720p.png"), True, node)
        self.assert_real_frame(
            self.load("home_720p.png"),
            True,
            "CloudGameHome",
            expected_box=HOME_TEMPLATE_LAYOUTS["legacy"]["CloudGameEnterText"][1],
        )
        # 首页不得被当成登录页或启动确认弹窗，否则会重复点击或误报登录失效。
        for node in ("CloudGameLoginScreen", "CloudGameStartConfirmNotice"):
            with self.subTest(node=node):
                self.assert_real_frame(self.load("home_720p.png"), False, node)

    def test_login_and_confirmation_frames_are_not_home(self):
        for fixture in (
            "login_required_720p.png",
            "login_required_current_125dpi.png",
            "start_confirm_720p.png",
        ):
            for node in ("CloudGameHomeScreen", "CloudGameEnterText", "CloudGameHome"):
                with self.subTest(fixture=fixture, node=node):
                    self.assert_real_frame(self.load(fixture), False, node)


SHELL_OCR_NODES = (
    "CloudGameQueueScreen",
    "CloudGameQueueText",
    "CloudGameDailyLoginTitle",
    "CloudGameDailyLoginCloseButton",
    "CloudGameLoading",
    "CloudGameErrorState",
)
CJK_FONT = Path(r"C:\Windows\Fonts\msyh.ttc")
OCR_MODEL = ROOT / "assets/MaaCommonAssets/OCR/ppocr_v3/zh_cn"


class ShellOcrProbe(CustomAction):
    def __init__(self):
        self.hits = {}
        super().__init__()

    def run(self, context, argv):
        controller = context.tasker.controller
        if not controller.post_screencap().wait().succeeded:
            return False
        image = controller.cached_image
        self.hits = {}
        for node in SHELL_OCR_NODES:
            detail = context.run_recognition(node, image)
            if detail is not None and detail.hit:
                self.hits[node] = [r.text for r in detail.filtered_results]
        return True


@unittest.skipUnless(
    CJK_FONT.exists() and (OCR_MODEL / "rec.onnx").exists(),
    "需要微软雅黑字体与 zh_cn OCR 模型",
)
class ShellOcrContractTests(unittest.TestCase):
    """用真实 OCR 引擎验证外壳 OCR 词表。文字按客户端 QML 模板渲染，不代表实机 ROI 与字体已校准。"""

    # (画面, 文本行, 应命中的节点)。占位符按客户端 QML 的 .arg() 用法填入示例值。
    CASES = (
        (
            "普通排队",
            ["排队中...", "目前正排在第 12 / 350 名", "预计等待 > 5分钟", "退出排队"],
            {"CloudGameQueueScreen", "CloudGameQueueText"},
        ),
        (
            "月卡排队",
            ["畅玩月卡加速排队中...", "目前正排在第 3 / 120 名", "预计等待 > 1分钟"],
            {"CloudGameQueueScreen", "CloudGameQueueText"},
        ),
        (
            "每日奖励",
            ["每日登录奖励", "免费时长 +60分钟", "点击空白区域关闭"],
            {"CloudGameDailyLoginTitle", "CloudGameDailyLoginCloseButton"},
        ),
        (
            "首次奖励",
            ["首次登录奖励", "点击空白区域关闭"],
            {"CloudGameDailyLoginTitle", "CloudGameDailyLoginCloseButton"},
        ),
        ("努力加载", ["努力加载中"], {"CloudGameLoading"}),
        (
            "启动黑屏",
            ["游戏启动中，黑屏时间可能较长", "请耐心等待~"],
            {"CloudGameLoading"},
        ),
        ("多设备排队", ["用户正在使用其他设备排队"], {"CloudGameErrorState"}),
        ("切换节点", ["请切换节点"], {"CloudGameErrorState"}),
        ("引导充值", ["剩余时长不足", "前往充值"], {"CloudGameErrorState"}),
        # 负样本：首页余额、故障排查确认框、退出排队确认框、黑屏都不得命中任何外壳节点。
        ("首页", ["免费时长: 11小时58分钟", "付费时长: 0分钟", "开始游戏"], set()),
        ("故障排查", ["检测过程中可能会出现短暂卡顿,请耐心等待。是否继续？"], set()),
        ("退出排队确认", ["是否确认退出排队？"], set()),
        ("黑屏", [], set()),
    )

    @staticmethod
    def render(lines):
        from PIL import ImageDraw, ImageFont

        font = ImageFont.truetype(str(CJK_FONT), 24)
        image = Image.new("RGB", (1280, 720), (18, 20, 26))
        draw = ImageDraw.Draw(image)
        y = 260
        for line in lines:
            width = draw.textlength(line, font=font)
            draw.text(((1280 - width) / 2, y), line, font=font, fill=(235, 235, 235))
            y += 48
        return np.asarray(image)[:, :, ::-1].copy()

    def test_shell_ocr_words_hit_only_their_screens(self):
        tmp = tempfile.TemporaryDirectory(prefix="cloud-ocr-", dir=ROOT / ".narrafork")
        self.addCleanup(tmp.cleanup)
        bundle = Path(tmp.name) / "bundle"
        (bundle / "pipeline").mkdir(parents=True)
        data = {name: copy.deepcopy(PIPELINE[name]) for name in SHELL_OCR_NODES}
        data["ShellOcrProbe"] = {
            "action": {"type": "Custom", "param": {"custom_action": "shell_ocr"}},
            "pre_delay": 0,
            "post_delay": 0,
            "rate_limit": 0,
        }
        (bundle / "pipeline/cloud.json").write_text(json.dumps(data), encoding="utf-8")
        import shutil

        shutil.copytree(OCR_MODEL, bundle / "model/ocr")
        resource = Resource()
        probe = ShellOcrProbe()
        self.assertTrue(resource.register_custom_action("shell_ocr", probe))
        self.assertTrue(resource.post_bundle(bundle).wait().succeeded)
        controller = VirtualCloudController(stage=0)
        self.assertTrue(controller.post_connection().wait().succeeded)
        tasker = Tasker()
        self.assertTrue(tasker.bind(resource, controller))
        try:
            for title, lines, expected in self.CASES:
                with self.subTest(screen=title):
                    controller.image = self.render(lines)
                    self.assertTrue(tasker.post_task("ShellOcrProbe").wait().succeeded)
                    self.assertEqual(set(probe.hits), expected, probe.hits)
            self.assertEqual(controller.clicks, [])
        finally:
            tasker.post_stop().wait()


if __name__ == "__main__":
    unittest.main()
