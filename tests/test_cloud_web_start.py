"""网页云异环离线回归：启动器校验、CDP 端点发现、排队解析与大厅弹窗分支（不访问网络与桌面）。"""

import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import cloud_web_start as web  # noqa: E402
from utils import cloud_cdp  # noqa: E402

GUID = "/devtools/browser/0f7d8907-9ee1-41ec-823c-79d353660b99"


class ProfileTests(unittest.TestCase):
    def test_default_profile_is_isolated_cache_dir(self):
        self.assertEqual(web.default_profile(), web.ROOT / "cache" / "cloud-web-profile")

    def test_rejects_daily_browser_profile_and_relative_paths(self):
        for value in ("cloud-web-profile", r"C:\Users\x\AppData\Local\Microsoft\Edge\User Data\cloud-web-profile",
                      r"C:\tmp\Default", "", 3):
            with self.subTest(value=value), self.assertRaises(web.WebClientError):
                web.validate_profile(value)

    def test_accepts_suffix(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / "cloud-web-profile-test"
            self.assertEqual(web.validate_profile(path), path)

    def test_command_uses_loopback_random_port_app_mode_without_automation_flags(self):
        command = web.build_browser_command(Path(r"C:\b\msedge.exe"), Path(r"C:\p\cloud-web-profile"))
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertIn("--remote-debugging-port=0", command)
        self.assertEqual(command[-1], "--app=" + web.CLOUD_URL)
        self.assertFalse(any("enable-automation" in arg or "headless" in arg for arg in command))

    def test_find_browser_rejects_non_browser_executable(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root) / "evil.exe"
            fake.touch()
            with self.assertRaises(web.WebClientError) as ctx:
                web.find_browser(fake)
            self.assertEqual(ctx.exception.args[0], "invalid_browser")


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.profile = Path(temp.name)

    def write_port(self, text):
        (self.profile / "DevToolsActivePort").write_text(text, encoding="ascii")

    def run_discover(self, rows, exe=r"c:\edge\msedge.exe", version=None):
        version = version or {"webSocketDebuggerUrl": "ws://127.0.0.1:5000" + GUID}
        with patch.object(web, "listener_pids", return_value=rows), patch.object(web, "process_exe", return_value=exe), \
                patch.object(web, "http_json", return_value=version):
            return web.discover(self.profile)

    def test_missing_and_invalid_port_file(self):
        self.assertEqual(web.discover(self.profile), (None, "no_port_file"))
        self.write_port("abc\n")
        self.assertEqual(web.discover(self.profile)[1], "invalid_port_file")

    def test_verified_endpoint(self):
        self.write_port("5000\n" + GUID)
        endpoint, reason = self.run_discover([(0x0100007F, 42)])
        self.assertEqual(reason, "ok")
        self.assertEqual((endpoint.port, endpoint.pid), (5000, 42))

    def test_rejects_stale_non_loopback_foreign_and_guid_mismatch(self):
        self.write_port("5000\n" + GUID)
        self.assertEqual(self.run_discover([])[1], "stale")
        self.assertEqual(self.run_discover([(0, 42)])[1], "not_loopback")
        self.assertEqual(self.run_discover([(0x0100007F, 1), (0x0100007F, 2)])[1], "ambiguous_listener")
        self.assertEqual(self.run_discover([(0x0100007F, 42)], exe=r"c:\x\node.exe")[1], "not_browser")
        other = {"webSocketDebuggerUrl": "ws://127.0.0.1:5000/devtools/browser/other"}
        self.assertEqual(self.run_discover([(0x0100007F, 42)], version=other)[1], "guid_mismatch")

    def test_cloud_url_filter(self):
        self.assertTrue(web.is_cloud_url("https://yh.wanmei.com/cloud/"))
        self.assertFalse(web.is_cloud_url("https://evil.example/cloud/"))
        self.assertFalse(web.is_cloud_url("http://yh.wanmei.com/cloud/"))


class QueueParseTests(unittest.TestCase):
    def test_parse_queue_keeps_only_numbers_and_eta(self):
        info = cloud_cdp.parse_queue("排队中... 华东 爆满 切换 目前正排在第 486 / 486 名 预计等待 > 15分钟 退出排队")
        self.assertEqual(info, {"position": 486, "total": 486, "eta": ">15分钟", "vip": False})
        self.assertEqual(cloud_cdp.parse_queue("排队中...")["position"], None)

    def test_format_minutes(self):
        self.assertEqual(cloud_cdp.format_minutes(662), "11 小时 2 分钟")
        self.assertEqual(cloud_cdp.format_minutes(5), "5 分钟")
        self.assertEqual(cloud_cdp.format_minutes(None), "?")

    def test_stream_override_forces_720p_at_1x(self):
        self.assertEqual((cloud_cdp.STREAM_OVERRIDE["screenWidth"], cloud_cdp.STREAM_OVERRIDE["screenHeight"],
                          cloud_cdp.STREAM_OVERRIDE["deviceScaleFactor"]), (1280, 720, 1))


class LobbyPopupTests(unittest.TestCase):
    """弹窗分支：只点确定/推荐节点/关闭，时长不足与环境不支持直接停止。"""

    @classmethod
    def setUpClass(cls):
        import importlib

        # 流程逻辑不依赖 AgentServer，直接按模块名导入（不经过 custom 包）。
        try:
            cls.mod = importlib.import_module("cloud_web_flow")
        except ImportError as exc:  # 缺少 MaaFW 时跳过
            raise unittest.SkipTest(str(exc))

    def setUp(self):
        self.clicks = []
        session = SimpleNamespace(click_rect=lambda rect: self.clicks.append(tuple(rect)))
        self.web = SimpleNamespace(session=session)
        self.printed = []
        patcher = patch.object(self.mod, "PrintT", side_effect=lambda ctx, key, *a: self.printed.append(key))
        patcher.start()
        self.addCleanup(patcher.stop)

    def pop(self, kind, text="", btns=(), rows=()):
        return {"kind": kind, "text": text, "btns": [{"x": x, "c": c, "r": [1, 2, 3, 4]} for x, c in btns],
                "rows": list(rows)}

    def test_node_prefers_recommended_then_confirms(self):
        rows = [{"t": "华东 16ms 爆满", "on": False, "r": [0, 0, 10, 10]}, {"t": "推荐 华北 19ms", "on": False, "r": [0, 20, 10, 10]}]
        clicked = set()
        self.assertEqual(self.mod._handle_popup(None, self.web, self.pop("nodelist", rows=rows), clicked), "acted")
        self.assertEqual(self.clicks[-1], (0, 20, 10, 10))
        rows[1]["on"] = True
        pop = self.pop("nodelist", btns=[("确定", "btn confirm")], rows=rows)
        self.assertEqual(self.mod._handle_popup(None, self.web, pop, clicked), "acted")
        self.assertEqual(self.mod._handle_popup(None, self.web, pop, clicked), None)  # 不重复点击

    def test_duration_confirm_never_ticks_dont_remind(self):
        pop = self.pop("consume-duration", btns=[("不再提醒", "radio"), ("退出游戏（30s）", "btn"), ("确定", "btn confirm")])
        self.mod._handle_popup(None, self.web, pop, set())
        self.assertEqual(len(self.clicks), 1)

    def test_remind_classification(self):
        for text, key in (("您当前的剩余时长不足，暂无法进入游戏", "cloud_web.no_duration"),
                          ("当前环境不满足云游戏运行要求。", "cloud_web.env_unsupported")):
            with self.assertRaises(self.mod._WebError) as ctx:
                self.mod._handle_popup(None, self.web, self.pop("remind", text, [("确定", "btn confirm")]), set())
            self.assertEqual(ctx.exception.key, key)
        result = self.mod._handle_popup(None, self.web, self.pop("remind", "网络异常，请稍候重试！", [("确定", "btn confirm")]), set())
        self.assertEqual(result, "retry")

    def test_payment_popups_are_only_closed(self):
        pop = self.pop("jump-payment", btns=[("去支付", "btn confirm"), ("关闭", "null close-btn")])
        self.mod._handle_popup(None, self.web, pop, set())
        self.assertEqual(len(self.clicks), 1)
        self.assertIn("cloud_web.popup_closed", self.printed)

    def test_queue_popup_is_reported_not_clicked(self):
        self.assertEqual(self.mod._handle_popup(None, self.web, self.pop("queue", "排队中"), set()), "queue")
        self.assertEqual(self.clicks, [])


class StreamStopTests(unittest.TestCase):
    """最终失败止损：云端实例在串流时刷新页面断开；未运行/连不上时不动。"""

    @classmethod
    def setUpClass(cls):
        import importlib

        try:
            cls.mod = importlib.import_module("cloud_web_flow")
        except ImportError as exc:  # 缺少 MaaFW 时跳过
            raise unittest.SkipTest(str(exc))

    def setUp(self):
        self.printed = []
        patcher = patch.object(
            self.mod,
            "PrintT",
            side_effect=lambda ctx, key, *a: self.printed.append(key),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_stop(self, session):
        connect = patch.object(self.mod.CloudSession, "connect", return_value=session)
        connect.start()
        self.addCleanup(connect.stop)
        self.mod._stop_stream_on_fail(None)

    def test_running_stream_is_stopped_by_reload(self):
        states = iter([{"game": True, "pops": []}, {"game": False, "pops": []}])
        session = Mock()
        session.state.side_effect = lambda: next(states)
        with patch.object(self.mod.time, "sleep"):
            self.run_stop(session)
        self.assertEqual(self.printed, ["cloud_web.stream_stop_fail"])
        session.reload.assert_called_once()
        session.close.assert_called_once()

    def test_not_running_leaves_page_alone(self):
        session = Mock()
        session.state.return_value = {"game": False, "pops": []}
        self.run_stop(session)
        self.assertEqual(self.printed, [])
        session.reload.assert_not_called()
        session.close.assert_called_once()

    def test_unreachable_browser_is_not_fatal(self):
        with patch.object(
            self.mod.CloudSession,
            "connect",
            side_effect=self.mod.WebClientError("cdp_failed"),
        ):
            self.mod._stop_stream_on_fail(None)
        self.assertEqual(self.printed, [])

    def test_reload_without_disconnect_reports_manual_check(self):
        session = Mock()
        session.state.return_value = {"game": True, "pops": []}
        with patch.object(self.mod.time, "sleep"), patch.object(
            self.mod.time, "monotonic", side_effect=[0.0] + [30.0] * 8
        ):
            self.run_stop(session)
        self.assertEqual(self.printed, ["cloud_web.stream_stop_fail"])
        session.reload.assert_called_once()
        session.close.assert_called_once()


class _FlowClock:
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
        if len(self.sleeps) > 10000:
            raise AssertionError("虚拟等待未受截止时间约束")
        self.advance(max(seconds, 0.000000001))
        if self.on_sleep is not None:
            self.on_sleep()


class _FlowFrame:
    ndim = 3
    shape = (720, 1280, 3)

    def __init__(self, state):
        self.state = state


class _FlowContext:
    def __init__(self):
        self.tasker = SimpleNamespace(stopping=False)
        self.tasker.post_task = Mock(side_effect=AssertionError("禁止启动真实任务"))
        self.override_next = Mock(return_value=True)
        self.recognitions = []
        self.nodes = {}
        for relative in ("EnterGame.json", "CloudGame/CloudGameWeb.json"):
            path = ROOT / "assets/resource/base/pipeline" / relative
            text = path.read_text(encoding="utf-8")
            self.nodes.update(
                json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE))
            )

    def get_node_data(self, node):
        return self.nodes.get(node)

    def run_recognition(self, node, frame):
        self.recognitions.append((node, frame))
        return SimpleNamespace(
            hit=node == frame.state,
            box=[600, 600, 80, 40],
            filtered_results=[SimpleNamespace(box=[600, 600, 80, 40])],
        )


class _FlowSession:
    def __init__(self, pages, frames):
        self.pages = list(pages)
        self.frames = list(frames)
        self.state_reads = 0
        self.capture_reads = 0
        self.on_state = None
        self.on_capture = None
        self.state = Mock(side_effect=self._state)
        self.capture = Mock(side_effect=self._capture)
        self.click = Mock(return_value=True)
        self.key = Mock(return_value=True)
        self.click_rect = Mock(return_value=True)
        self.pointer_lock = Mock()
        self.set_stream_override = Mock()
        self.window_state = Mock()
        self.bring_to_front = Mock()
        self.ensure_not_minimized = Mock(return_value=False)
        self.frame_to_css = Mock(side_effect=lambda content, point: point)
        self.close = Mock()
        self.reload = Mock(side_effect=AssertionError("普通流程不应刷新或重启页面"))

    def _state(self):
        page = self.pages[min(self.state_reads, len(self.pages) - 1)]
        self.state_reads += 1
        if self.on_state is not None:
            self.on_state(self.state_reads, page)
        return page

    def _capture(self, content):
        state = self.frames[min(self.capture_reads, len(self.frames) - 1)]
        self.capture_reads += 1
        if self.on_capture is not None:
            self.on_capture(self.capture_reads)
        return _FlowFrame(state)


class WebEntryFlowTests(unittest.TestCase):
    """使用真实四阶段和公共 world 循环，只替换时钟、DOM、截图及输入传输。"""

    READY = "CloudWebWorldReady"
    ENTER = "CloudWebWorldClickEnter"
    MONTHLY = "CloudWebWorldMonthlyPass"

    @classmethod
    def setUpClass(cls):
        import importlib

        cls.mod = importlib.import_module("cloud_web_flow")
        cls.common = importlib.import_module("game_entry")

    def setUp(self):
        self.clock = _FlowClock()
        self.context = _FlowContext()
        self.printed = []
        self.progresses = []
        self.world_timeout = 3
        self.session = _FlowSession([self.game()], [self.READY])
        self.state = self.mod._Web(
            task_id=1,
            auto_queue=False,
            login_timeout=2,
            queue_timeout=2,
            fullscreen=False,
            session=self.session,
            started_at=0,
        )
        # 替换模块的 time 变量，而不是修改全局 time.monotonic/sleep。
        for module in (self.mod, self.common):
            self.patch(module, "time", self.clock)
            self.patch(
                module,
                "PrintT",
                Mock(side_effect=lambda ctx, key, *args: self.printed.append(key)),
            )
            self.patch(module, "logger", Mock())
        self.patch(self.mod, "_web", self.state)
        self.patch(self.mod, "WorldProgress", Mock(side_effect=self.make_progress))
        self.cleanup = self.patch(
            self.mod,
            "cleanup_cloud_web_session",
            Mock(wraps=self.mod.cleanup_cloud_web_session),
        )
        self.connect = self.patch(
            self.mod.CloudSession,
            "connect",
            Mock(side_effect=AssertionError("禁止连接真实 CDP")),
        )
        self.patch(
            self.mod,
            "start_browser",
            Mock(side_effect=AssertionError("禁止启动真实浏览器")),
        )

    def patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def make_progress(self, login_timeout):
        progress = self.common.WorldProgress(
            login_timeout, world_timeout=self.world_timeout, transition_timeout=1
        )
        self.progresses.append(progress)
        return progress

    def page(self, **overrides):
        page = {
            "home": True,
            "game": False,
            "logged_in": True,
            "free_min": 20,
            "paid_min": 0,
            "video": None,
            "loading": False,
            "pops": [],
            "gift": None,
            "start": [100, 200, 100, 40],
            "events": [],
        }
        page.update(overrides)
        return page

    def game(self, **overrides):
        page = self.page(
            home=False,
            game=True,
            start=None,
            video={
                "content": [0, 0, 1280, 720],
                "rs": 2,
                "w": 1280,
                "h": 720,
                "paused": False,
            },
        )
        page.update(overrides)
        return page

    def popup(self, kind, text="", label="关闭", css="close-btn"):
        return {
            "kind": kind,
            "text": text,
            "btns": [{"x": label, "c": css, "r": [10, 20, 30, 40]}],
            "rows": [],
        }

    def queued(self):
        return self.page(
            pops=[
                self.popup(
                    "queue",
                    "排队中，目前正排在第 3 / 4 名 预计等待 1分钟",
                    "退出排队",
                    "cancel",
                )
            ]
        )

    def pages(self, *pages):
        self.session.pages = list(pages)
        self.session.state_reads = 0

    def frames(self, *states):
        self.session.frames = list(states)
        self.session.capture_reads = 0

    def run_action(self, action):
        argv = SimpleNamespace(node_name=action, custom_action_param="")
        return getattr(self.mod, action)().run(self.context, argv)

    def assert_no_input(self):
        self.session.click.assert_not_called()
        self.session.key.assert_not_called()
        self.session.click_rect.assert_not_called()
        self.connect.assert_not_called()
        self.context.tasker.post_task.assert_not_called()

    def assert_failed_and_cleaned(self, result, key=None):
        self.assertFalse(result.success)
        self.cleanup.assert_called_once()
        self.session.close.assert_called_once()
        self.session.pointer_lock.assert_any_call(False)
        self.assertIsNone(self.mod._web)
        self.assertNotIn("enter_game.world_ready", self.printed)
        if key is not None:
            self.assertIn(key, self.printed)
        self.assertFalse(
            any(
                "CloudWebLobby" in call.args[1]
                for call in self.context.override_next.call_args_list
            )
        )

    def test_login_auto_off_home_routes_to_manual_wait_not_task_done(self):
        self.pages(self.page())
        self.assertTrue(self.run_action("CloudWebLogin").success)
        self.context.override_next.assert_called_once_with(
            "CloudWebLogin", ["CloudWebManualEnterNotice"]
        )
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLobby"), "enter_game.manual_timeout"
        )
        self.assert_no_input()

    def test_login_auto_off_existing_queue_skips_lobby_and_manual_wait(self):
        self.pages(self.queued())
        self.assertTrue(self.run_action("CloudWebLogin").success)
        self.context.override_next.assert_called_once_with(
            "CloudWebLogin", ["CloudWebQueue"]
        )
        self.assertNotIn("enter_game.manual_enter", self.printed)
        self.assert_no_input()

    def test_login_auto_off_existing_stream_skips_lobby_and_manual_wait(self):
        self.assertTrue(self.run_action("CloudWebLogin").success)
        self.context.override_next.assert_called_once_with(
            "CloudWebLogin", ["CloudWebQueue"]
        )
        self.assertNotIn("cloud_web.duration_checking", self.printed)
        self.assert_no_input()

    def test_login_wait_restores_window_then_routes_user_started_queue(self):
        self.pages(self.page(logged_in=False), self.queued())
        self.state.login_timeout = 5
        self.assertTrue(self.run_action("CloudWebLogin").success)
        self.session.window_state.assert_called_once_with("normal")
        self.session.bring_to_front.assert_called_once()
        self.context.override_next.assert_called_once_with(
            "CloudWebLogin", ["CloudWebQueue"]
        )
        self.assert_no_input()

    def test_login_existing_deadline_is_not_reset(self):
        self.pages(self.page(logged_in=False))
        self.state.login_timeout = 60
        self.state.login_deadline = 1
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLogin"), "cloud_web.login_timeout"
        )
        self.assertEqual(self.state.login_deadline, 1)
        self.assertAlmostEqual(self.clock.now, 1)
        self.assert_no_input()

    def test_login_late_dom_success_does_not_defeat_deadline(self):
        self.pages(self.page(logged_in=False), self.game())
        self.state.login_timeout = 3

        def late(count, page):
            if count == 2:
                self.clock.advance(2)

        self.session.on_state = late
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLogin"), "cloud_web.login_timeout"
        )
        self.context.override_next.assert_not_called()
        self.assert_no_input()

    def test_manual_home_timeout_never_starts_queue_or_reports_success(self):
        self.pages(self.page())
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLobby"), "enter_game.manual_timeout"
        )
        self.assertAlmostEqual(self.clock.now, 2)
        self.assertEqual(self.printed.count("enter_game.manual_enter"), 1)
        self.context.override_next.assert_not_called()
        self.assert_no_input()

    def test_manual_existing_deadline_is_not_extended(self):
        self.pages(self.page())
        self.state.login_timeout = 60
        self.state.manual_deadline = 0.5
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLobby"), "enter_game.manual_timeout"
        )
        self.assertEqual(self.state.manual_deadline, 0.5)
        self.assertAlmostEqual(self.clock.now, 0.5)
        self.assert_no_input()

    def test_manual_repeated_lobby_entry_preserves_original_deadline(self):
        self.pages(self.queued())
        self.assertTrue(self.run_action("CloudWebLobby").success)
        deadline = self.state.manual_deadline
        self.clock.advance(1)
        self.pages(self.page())
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLobby"), "enter_game.manual_timeout"
        )
        self.assertEqual(self.state.manual_deadline, deadline)
        self.assertAlmostEqual(self.clock.now, 2)
        self.assert_no_input()

    def test_manual_user_enters_queue_and_flow_takes_over_without_input(self):
        self.pages(self.page(), self.queued())
        self.assertTrue(self.run_action("CloudWebLobby").success)
        self.context.override_next.assert_called_once_with(
            "CloudWebLobby", ["CloudWebQueue"]
        )
        self.assertAlmostEqual(self.clock.now, 0.5)
        self.cleanup.assert_not_called()
        self.assert_no_input()

    def test_manual_user_starts_stream_and_flow_takes_over_without_input(self):
        self.pages(self.page(), self.game())
        self.assertTrue(self.run_action("CloudWebLobby").success)
        self.context.override_next.assert_called_once_with(
            "CloudWebLobby", ["CloudWebQueue"]
        )
        self.assert_no_input()

    def test_manual_mode_does_not_accept_confirmation_or_daily_gift(self):
        self.pages(
            self.page(
                pops=[self.popup("consume-duration", label="确定", css="confirm")],
                gift={"r": [10, 20, 30, 40]},
            )
        )
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLobby"), "enter_game.manual_timeout"
        )
        self.assert_no_input()

    def test_auto_lobby_starts_only_once_while_home_is_unchanged(self):
        self.state.auto_queue = True
        self.pages(self.page(), self.page(), self.queued())
        self.assertTrue(self.run_action("CloudWebLobby").success)
        self.session.click_rect.assert_called_once_with([100, 200, 100, 40])
        self.context.override_next.assert_called_once_with(
            "CloudWebLobby", ["CloudWebQueue"]
        )

    def test_lobby_cancellation_during_dom_read_prevents_input(self):
        self.state.auto_queue = True
        self.pages(self.page())
        self.session.on_state = lambda count, page: setattr(
            self.context.tasker, "stopping", True
        )
        self.assert_failed_and_cleaned(self.run_action("CloudWebLobby"))
        self.assert_no_input()

    def test_late_lobby_dom_read_cannot_start_queue(self):
        self.state.auto_queue = True
        self.pages(self.page())
        self.session.on_state = lambda count, page: self.clock.advance(61)
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLobby"), "cloud_web.lobby_timeout"
        )
        self.assert_no_input()

    def test_late_manual_dom_queue_does_not_extend_deadline(self):
        self.pages(self.queued())
        self.session.on_state = lambda count, page: self.clock.advance(3)
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebLobby"), "enter_game.manual_timeout"
        )
        self.context.override_next.assert_not_called()
        self.assert_no_input()

    def test_queue_first_video_frame_is_not_world_success(self):
        self.assertTrue(self.run_action("CloudWebQueue").success)
        self.assertIn("cloud_web.stream_ready", self.printed)
        self.assertNotIn("enter_game.world_ready", self.printed)
        self.session.capture.assert_not_called()
        self.assert_no_input()

    def test_queue_then_world_requires_no_icon_enter_click_and_two_ready_frames(self):
        self.state.queue_deadline = 1
        self.assertTrue(self.run_action("CloudWebQueue").success)
        self.assertIsNone(self.state.queue_deadline)
        self.frames(self.ENTER, self.ENTER, self.READY, self.READY)
        self.assertTrue(self.run_action("CloudWebWorld").success)
        self.session.click.assert_called_once_with(640.0, 620.0)
        self.assertEqual(self.session.capture_reads, 4)
        self.assertIn("enter_game.world_ready", self.printed)
        self.cleanup.assert_not_called()

    def test_queue_deadline_clears_at_game_container_before_first_video_frame(self):
        self.state.queue_deadline = 0.25
        self.pages(
            self.game(loading=True, video=None),
            self.game(loading=True, video=None),
            self.game(),
        )
        self.assertTrue(self.run_action("CloudWebQueue").success)
        self.assertIsNone(self.state.queue_deadline)
        self.assertGreater(self.clock.now, 0.25)
        self.assertNotIn("cloud_web.queue_timeout", self.printed)
        self.assert_no_input()

    def test_existing_queue_does_not_reset_deadline(self):
        self.state.queue_deadline = 0.5
        self.pages(self.queued(), self.queued(), self.page())
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebQueue"), "cloud_web.queue_timeout"
        )
        self.assertEqual(self.state.queue_deadline, 0.5)
        self.session.click_rect.assert_called_once_with([10, 20, 30, 40])
        self.context.override_next.assert_not_called()

    def test_queue_timeout_without_cancel_button_is_failure_not_requeue(self):
        page = self.queued()
        page["pops"][0]["btns"] = []
        self.pages(page)
        self.state.queue_deadline = 0
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebQueue"), "enter_game.failed"
        )
        self.context.override_next.assert_not_called()
        self.assert_no_input()

    def test_queue_return_home_fails_without_routing_back_to_lobby(self):
        self.pages(self.page())
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebQueue"), "cloud_web.stream_lost"
        )
        self.context.override_next.assert_not_called()
        self.assert_no_input()

    def test_queue_cancellation_during_dom_read_never_clicks_cancel(self):
        self.pages(self.queued())
        self.state.queue_deadline = 0
        self.session.on_state = lambda count, page: setattr(
            self.context.tasker, "stopping", True
        )
        self.assert_failed_and_cleaned(self.run_action("CloudWebQueue"))
        self.assert_no_input()

    def test_queue_fullscreen_mode_remains_supported(self):
        self.state.fullscreen = True
        self.assertTrue(self.run_action("CloudWebQueue").success)
        self.session.window_state.assert_called_once_with("fullscreen")

    def test_queue_fullscreen_failure_attempts_normal_window(self):
        self.state.fullscreen = True
        self.session.window_state.side_effect = [
            self.mod.WebClientError("window_state_failed"),
            None,
        ]
        self.assertTrue(self.run_action("CloudWebQueue").success)
        self.assertEqual(
            [call.args[0] for call in self.session.window_state.call_args_list],
            ["fullscreen", "normal"],
        )

    def test_world_already_ready_needs_two_fresh_frames_and_never_clicks(self):
        self.assertTrue(self.run_action("CloudWebWorld").success)
        self.assertEqual(self.session.capture_reads, 2)
        frames = [
            frame for node, frame in self.context.recognitions if node == self.READY
        ]
        self.assertEqual(len(frames), 2)
        self.assertIsNot(frames[0], frames[1])
        self.assert_no_input()

    def test_world_has_own_budget_not_expired_queue_deadline(self):
        self.state.queue_deadline = -1
        self.assertTrue(self.run_action("CloudWebWorld").success)
        self.assertIsNone(self.state.queue_deadline)
        self.assertEqual(self.progresses[0].world_deadline, 3)
        self.assert_no_input()

    def test_world_return_home_fails_without_lobby_route(self):
        self.pages(self.page())
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "cloud_web.stream_lost"
        )
        self.context.override_next.assert_not_called()
        self.assert_no_input()

    def test_world_missing_game_container_is_failure_not_lobby_retry(self):
        self.pages(self.page(home=False))
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "cloud_web.stream_lost"
        )
        self.context.override_next.assert_not_called()
        self.assert_no_input()

    def test_world_minimized_restore_discards_frame_then_confirms_world(self):
        self.session.ensure_not_minimized.side_effect = [True, False, False]
        self.assertTrue(self.run_action("CloudWebWorld").success)
        self.assertEqual(self.session.ensure_not_minimized.call_count, 3)
        self.assertEqual(self.session.capture_reads, 2)
        self.assertGreaterEqual(self.clock.now, 0.4)
        self.assert_no_input()

    def test_world_unknown_popup_blocks_video_and_cannot_be_success(self):
        self.pages(self.game(pops=[self.popup("unknown")]))
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "enter_game.world_timeout"
        )
        self.session.capture.assert_not_called()
        self.assertEqual(self.context.recognitions, [])
        self.assert_no_input()

    def test_world_unknown_popup_between_ready_frames_resets_confirmation(self):
        self.pages(
            self.game(),
            self.game(pops=[self.popup("unknown")]),
            self.game(),
            self.game(),
        )
        self.assertTrue(self.run_action("CloudWebWorld").success)
        self.assertEqual(self.session.state_reads, 4)
        self.assertEqual(self.session.capture_reads, 3)
        self.assert_no_input()

    def test_world_repeated_close_popup_is_clicked_only_once(self):
        popup_page = self.game(pops=[self.popup("setting")])
        self.pages(popup_page, popup_page, self.game(), self.game())
        self.assertTrue(self.run_action("CloudWebWorld").success)
        self.session.click_rect.assert_called_once_with([10, 20, 30, 40])
        self.session.click.assert_not_called()
        self.assertEqual(self.session.capture_reads, 2)

    def test_world_close_popup_stuck_times_out_without_duplicate_input(self):
        self.pages(self.game(pops=[self.popup("setting")]))
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "enter_game.world_timeout"
        )
        self.session.click_rect.assert_called_once_with([10, 20, 30, 40])
        self.session.capture.assert_not_called()

    def test_world_late_popup_dom_read_does_not_close_popup(self):
        self.pages(self.game(pops=[self.popup("setting")]))
        self.session.on_state = lambda count, page: self.clock.advance(4)
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "enter_game.world_timeout"
        )
        self.assert_no_input()

    def test_world_cancelled_popup_dom_read_does_not_close_popup(self):
        self.pages(self.game(pops=[self.popup("setting")]))
        self.session.on_state = lambda count, page: setattr(
            self.context.tasker, "stopping", True
        )
        self.assert_failed_and_cleaned(self.run_action("CloudWebWorld"))
        self.assert_no_input()

    def test_world_late_before_input_dom_read_does_not_click_old_target(self):
        self.frames(self.ENTER)

        def late(count, page):
            if count == 3:
                self.clock.advance(4)

        self.session.on_state = late
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "enter_game.world_timeout"
        )
        self.assertEqual(self.session.capture_reads, 2)
        self.assert_no_input()

    def test_world_cancelled_before_input_dom_read_does_not_click(self):
        self.frames(self.ENTER)

        def stop(count, page):
            if count == 3:
                self.context.tasker.stopping = True

        self.session.on_state = stop
        self.assert_failed_and_cleaned(self.run_action("CloudWebWorld"))
        self.assertEqual(self.session.capture_reads, 2)
        self.assert_no_input()

    def test_world_new_popup_before_click_rejects_old_video_target(self):
        self.frames(self.ENTER)
        self.pages(self.game(), self.game(), self.game(pops=[self.popup("unknown")]))
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "enter_game.input_rejected"
        )
        self.assertEqual(self.session.capture_reads, 2)
        self.assert_no_input()

    def test_world_new_popup_before_escape_rejects_key(self):
        self.frames(self.MONTHLY)
        self.pages(self.game(), self.game(), self.game(pops=[self.popup("unknown")]))
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "enter_game.input_rejected"
        )
        self.assert_no_input()

    def test_world_late_second_capture_cannot_accept_success(self):
        def late(count):
            if count == 2:
                self.clock.advance(4)

        self.session.on_capture = late
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "enter_game.world_timeout"
        )
        self.assert_no_input()

    def test_world_stream_error_popup_stops_instead_of_dismissing_and_retrying(self):
        self.pages(
            self.game(
                pops=[self.popup("remind", "网络异常，请稍候重试", "确定", "confirm")]
            )
        )
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "cloud_web.stream_lost"
        )
        self.assert_no_input()

    def test_world_cdp_failure_triggers_cleanup_without_turning_error_into_success(
        self,
    ):
        self.session.capture.side_effect = self.mod.WebClientError("cdp_failed")
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "cloud_web.browser_failed"
        )
        self.assert_no_input()

    def test_world_click_transport_error_is_not_retried(self):
        self.frames(self.ENTER)
        self.session.click.side_effect = self.mod.WebClientError("cdp_failed")
        self.assert_failed_and_cleaned(
            self.run_action("CloudWebWorld"), "cloud_web.browser_failed"
        )
        self.session.click.assert_called_once()

    def test_stop_during_manual_wait_is_prompt_and_cleans_up(self):
        self.pages(self.page())
        self.clock.on_sleep = lambda: setattr(self.context.tasker, "stopping", True)
        self.assert_failed_and_cleaned(self.run_action("CloudWebLobby"))
        self.assertLessEqual(self.clock.now, 0.1)
        self.assert_no_input()


if __name__ == "__main__":
    unittest.main()
