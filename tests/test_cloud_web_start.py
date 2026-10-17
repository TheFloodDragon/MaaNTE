"""网页云异环离线回归：启动器校验、CDP 端点发现、排队解析与大厅弹窗分支（不访问网络与桌面）。"""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
