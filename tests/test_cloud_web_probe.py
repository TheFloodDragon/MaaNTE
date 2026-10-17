"""阶段 0 探针离线回归：浏览器、桌面、网络和真实输入均使用替身。"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import cloud_web_probe as probe


def layout(width=64, height=36, dpr=1):
    return {"target": True, "dpr": dpr, "inner": [width, height], "video": {
        "rect": [0, 0, width, height], "width": 1920, "height": 1080,
        "readyState": 4, "paused": False, "currentTime": 1,
        "clear": True, "visible": True, "simple": True, "objectFit": "contain"}}


def window(hwnd=42, pid=123):
    return probe.WindowInfo(hwnd, pid, (10, 20, 74, 56), (64, 36), (10, 20), 96)


def actual(**kwargs):
    value = {"foreground": True, "minimized": False, "visible": True,
             "sampled_uncovered": True, "sampled_occluded": False}
    value.update(kwargs)
    return value


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(probe, "ROOT", self.root).start()
        self.profile = self.root / ".narrafork" / "edge-cloud-profile"
        self.profile.mkdir(parents=True)
        self.token = "a" * 32
        (self.profile / ".probe-lock").write_text(self.token, encoding="ascii")
        self.state = {"schema": 2, "owner": "cloud-web-probe", "pid": 123, "hwnd": 42,
                      "created": 123456789, "exe": probe.normalized_exe(self.root / "msedge.exe"),
                      "profile": str(self.profile), "token": self.token, "cdp": True, "layout": "app"}
        self.output = self.root / ".narrafork" / "reports"
        self.state_path = self.output / "state.json"
        self.args = probe.build_parser().parse_args(["probe", "--state", str(self.state_path),
                                                    "--output", str(self.output)])
        probe._WORKER_KEEPALIVE.clear()

    def test_help_for_all_commands(self):
        for args in (["--help"], ["launch", "--help"], ["probe", "--help"], ["close", "--help"]):
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(args), 0)

    def test_default_cli_zero_input(self):
        args = probe.build_parser().parse_args(["probe"])
        self.assertFalse(args.allow_input)
        self.assertIsNone(args.click)
        self.assertIsNone(args.key)
        self.assertIsNone(args.input_method)
        self.assertEqual(args.methods, probe.CAPTURE_METHODS)
        self.assertEqual(args.states, probe.STATES)
        self.assertFalse(probe.validate_input_args(args))

    def test_launch_and_close_parse(self):
        args = probe.build_parser().parse_args(["launch", "--cdp", "--size", "1920x1080"])
        self.assertEqual(args.size, (1920, 1080))
        self.assertTrue(args.cdp)
        self.assertEqual(probe.build_parser().parse_args(["close"]).command, "close")

    def test_invalid_arguments_are_private(self):
        cases = [["close", "--pid", "123"], ["probe", "--keep-open"],
                 ["launch", "--url", "https://private.invalid/?secret=123"],
                 ["launch", "--wait", "nan"], ["launch", "--size", "9999x9999"],
                 ["probe", "--methods", "SECRET"],
                 ["probe", "--click", "10,10", "--key", "ESC"]]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()) as out:
                self.assertEqual(probe.main(args), 2)
                self.assertEqual(out.getvalue(), "cloud-web-probe: invalid_arguments; use --help\n")

    def test_parameter_parsers(self):
        self.assertEqual(probe.parse_point("12,34"), (12, 34))
        self.assertEqual(probe.parse_key("ESC"), 27)
        self.assertEqual(probe.parse_key("0x7c"), 124)
        for fn, values in ((probe.parse_key, ["0", "256", "SECRET"]),
                           (probe.parse_point, ["-1,3", "1,2,3", "nan,1"]),
                           (probe.positive_timeout, ["0", "inf", "121"])):
            for value in values:
                with self.subTest(value=value), self.assertRaises(Exception):
                    fn(value)

    def test_edge_command_is_isolated_and_cdp_optional(self):
        command = probe.build_edge_command(self.state["exe"], self.profile)
        self.assertIn("--app=" + probe.DEFAULT_URL, command)
        self.assertIn("--user-data-dir=" + str(self.profile), command)
        self.assertIn("--edge-skip-compat-layer-relaunch", command)
        self.assertFalse(any("remote-debugging" in part for part in command))
        command = probe.build_edge_command(self.state["exe"], self.profile, cdp=True, layout="kiosk")
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertIn("--remote-debugging-port=0", command)
        self.assertIn("--kiosk", command)

    def test_profile_and_artifacts_cannot_escape_ignore_directory(self):
        for path in (self.root, self.root / "User Data", Path("relative"), self.root / ".narrafork" / "other"):
            with self.subTest(path=path), self.assertRaises(probe.ProbeError):
                probe.validate_profile(path)
        with self.assertRaises(probe.ProbeError):
            probe.artifact_path(self.root / "report.json")

    def test_only_exact_cloud_url(self):
        for value in (probe.DEFAULT_URL + "?token=SECRET", probe.DEFAULT_URL + "other",
                      "http://yh.wanmei.com/cloud/", "https://yh.wanmei.com/cloud"):
            with self.subTest(value=value), self.assertRaises(probe.ProbeError):
                probe.validate_url(value)

    def test_error_codes_never_preserve_arbitrary_text(self):
        cases = [ValueError("SECRET URL"), probe.ProbeError("SECRET URL"),
                 probe.ProbeError(["SECRET"]), KeyboardInterrupt("SECRET"),
                 subprocess.TimeoutExpired("SECRET URL", 3), PermissionError("SECRET")]
        for exc in cases:
            result = probe.safe_reason(exc)
            self.assertIn(result, probe.ERROR_CODES)
            self.assertNotIn("SECRET", result)
        self.assertEqual(probe.safe_reason(probe.ProbeError("process_mismatch")), "process_mismatch")

    def test_select_window_has_no_pid_or_title_fallback(self):
        self.assertEqual(probe.select_window([window()], pid=123).hwnd, 42)
        for windows in ([window(pid=999)], [], [window(), window(hwnd=43)]):
            with self.assertRaises(probe.ProbeError):
                probe.select_window(windows, pid=123)

    def test_state_requires_owned_fingerprint(self):
        self.assertEqual(probe.validate_state(self.state), self.state)
        for key, replacement in (("created", None), ("pid", True), ("hwnd", 0),
                                 ("owner", "user-browser"), ("exe", "relative/msedge.exe"),
                                 ("token", "b" * 32)):
            broken = dict(self.state, **{key: replacement})
            with self.subTest(key=key), self.assertRaises(probe.ProbeError):
                probe.validate_state(broken)

    def kernel(self):
        kernel, user = Mock(), Mock()
        kernel.OpenProcess.return_value = 77
        kernel.WaitForSingleObject.return_value = 0x102
        patch.object(probe, "_win32", return_value=SimpleNamespace(kernel=kernel, user=user)).start()
        patch.object(probe, "window_info", return_value=window()).start()
        patch.object(probe, "process_identity", return_value={
            "created": self.state["created"], "exe": self.state["exe"]}).start()
        return kernel, user

    def test_reused_pid_never_gets_close_or_terminate(self):
        kernel, user = self.kernel()
        probe.process_identity.return_value = {"created": self.state["created"] + 1, "exe": self.state["exe"]}
        with self.assertRaises(probe.ProbeError):
            probe.close_browser(self.state)
        user.PostMessageW.assert_not_called()
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(77)

    def test_wrong_executable_never_gets_terminate(self):
        kernel, user = self.kernel()
        probe.process_identity.return_value = {"created": self.state["created"], "exe": "other"}
        with self.assertRaises(probe.ProbeError):
            probe.close_browser(self.state)
        kernel.TerminateProcess.assert_not_called()
        user.PostMessageW.assert_not_called()

    def test_wrong_hwnd_never_gets_close_or_terminate(self):
        kernel, user = self.kernel()
        probe.window_info.side_effect = probe.ProbeError("process_mismatch")
        with self.assertRaises(probe.ProbeError):
            probe.close_browser(self.state)
        kernel.TerminateProcess.assert_not_called()
        user.PostMessageW.assert_not_called()

    def test_missing_process_is_not_close_success(self):
        kernel, user = self.kernel()
        kernel.OpenProcess.return_value = 0
        with self.assertRaises(probe.ProbeError):
            probe.close_browser(self.state)
        user.PostMessageW.assert_not_called()

    def test_close_uses_same_verified_handle(self):
        kernel, user = self.kernel()
        kernel.WaitForSingleObject.side_effect = [0x102, 0x102, 0]
        self.assertTrue(probe.close_browser(self.state))
        kernel.OpenProcess.assert_called_once()
        kernel.TerminateProcess.assert_called_once_with(77, 1)
        user.PostMessageW.assert_called_once_with(42, 0x0010, 0, 0)
        self.assertEqual(probe.process_identity.call_count, 2)
        self.assertFalse((self.profile / ".probe-lock").exists())

    def test_graceful_close_does_not_terminate(self):
        kernel, _ = self.kernel()
        kernel.WaitForSingleObject.side_effect = [0x102, 0]
        self.assertTrue(probe.close_browser(self.state))
        kernel.TerminateProcess.assert_not_called()

    def test_close_command_only_closes_once(self):
        with patch.object(probe, "_load_state", return_value=self.state), \
             patch.object(probe, "close_browser", return_value=True) as close:
            self.assertEqual(probe.run_close(self.args), (0, {"closed": True}))
        close.assert_called_once_with(self.state)

    def target(self, url=None, websocket="ws://127.0.0.1:1234/devtools/page/ABC"):
        return {"type": "page", "url": url or probe.DEFAULT_URL, "title": "PRIVATE_TITLE",
                "webSocketDebuggerUrl": websocket}

    def test_cdp_selects_exact_single_target(self):
        self.assertEqual(probe.parse_target_list(json.dumps([self.target()]), 1234),
                         "ws://127.0.0.1:1234/devtools/page/ABC")
        for targets in ([self.target(probe.DEFAULT_URL + "?secret=1")], [self.target()] * 2,
                        [self.target("https://yh.wanmei.com/other")], {}):
            with self.subTest(targets=targets), self.assertRaises(probe.ProbeError):
                probe.parse_target_list(json.dumps(targets), 1234)

    def test_cdp_rejects_nonloopback_wrong_port_and_credentials(self):
        values = ["ws://evil.invalid:1234/devtools/page/ABC", "ws://localhost:1234/devtools/page/ABC",
                  "ws://127.0.0.1:9999/devtools/page/ABC", "ws://user:secret@127.0.0.1:1234/devtools/page/ABC",
                  "wss://127.0.0.1:1234/devtools/page/ABC", "ws://127.0.0.1:1234/devtools/page/ABC?q=1",
                  "ws://127.0.0.1:1234/devtools/page/ABC#secret"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(probe.ProbeError):
                probe.parse_target_list(json.dumps([self.target(websocket=value)]), 1234)

    def test_cdp_response_limit(self):
        with self.assertRaises(probe.ProbeError):
            probe.parse_target_list(" " * (probe.MAX_RESPONSE + 1))

    def test_http_redirects_disabled(self):
        with self.assertRaises(probe.ProbeError):
            probe.NoRedirect().redirect_request(None, None, 302, "SECRET", None, "http://evil/")
        opener = probe._proxy_free_opener()
        proxies = [handler for handler in opener.handlers if isinstance(handler, probe.urllib.request.ProxyHandler)]
        self.assertTrue(all(not handler.proxies for handler in proxies))
        self.assertTrue(any(isinstance(handler, probe.NoRedirect) for handler in opener.handlers))

    def test_http_read_is_bounded_and_url_checked(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = "http://127.0.0.1:1234/json/list"
        response.read.return_value = json.dumps([self.target()]).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(probe, "read_devtools_port", return_value=1234), \
             patch.object(probe, "verify_cdp_listener"), \
             patch.object(probe, "_proxy_free_opener", return_value=opener), \
             patch.object(probe, "CdpSession") as session:
            probe.connect_cdp(self.profile, 123)
            response.read.assert_called_once_with(probe.MAX_RESPONSE + 1)
            session.assert_called_once_with("ws://127.0.0.1:1234/devtools/page/ABC")
            response.geturl.return_value = "http://evil/"
            with self.assertRaises(probe.ProbeError):
                probe.connect_cdp(self.profile, 123)

    def test_cdp_listener_must_belong_to_launched_pid_on_loopback(self):
        library = Mock()
        address, owner = 0x0100007F, 123
        def table(buffer, size, *_args):
            size_ptr = probe.ctypes.cast(size, probe.ctypes.POINTER(probe.w.DWORD))
            size_ptr.contents.value = 4 + 6 * probe.ctypes.sizeof(probe.w.DWORD)
            if buffer is None:
                return 122
            probe.w.DWORD.from_buffer(buffer).value = 1
            row = (probe.w.DWORD * 6).from_buffer(buffer, 4)
            row[:] = (2, address, probe.socket.htons(1234), 0, 0, owner)
            return 0
        library.GetExtendedTcpTable.side_effect = table
        with patch.object(probe.sys, "platform", "win32"), \
             patch.object(probe.ctypes, "WinDLL", return_value=library, create=True):
            probe.verify_cdp_listener(1234, 123)
            owner = 999
            with self.assertRaises(probe.ProbeError):
                probe.verify_cdp_listener(1234, 123)
            owner, address = 123, 0
            with self.assertRaises(probe.ProbeError):
                probe.verify_cdp_listener(1234, 123)

    def test_devtools_file_is_bounded_and_checked(self):
        path = self.profile / "DevToolsActivePort"
        path.write_text("1234\n/devtools/browser/ABC\n", encoding="ascii")
        self.assertEqual(probe.read_devtools_port(self.profile), 1234)
        for content in ("0\n/devtools/browser/ABC", "1234\n//evil", "1" * 300):
            path.write_text(content, encoding="ascii")
            with self.assertRaises(probe.ProbeError):
                probe.read_devtools_port(self.profile)

    def test_layout_expression_is_readonly(self):
        for forbidden in ("addEventListener", "preventDefault", "stopPropagation", "stopImmediatePropagation",
                          "dispatchEvent", "requestFullscreen", "document.cookie", "localStorage"):
            self.assertNotIn(forbidden, probe.LAYOUT_SCRIPT)
        self.assertNotIn("document.querySelector('video')", probe.LAYOUT_SCRIPT)
        self.assertIn("#gameVideo", probe.LAYOUT_SCRIPT)

    def test_layout_report_is_whitelisted(self):
        raw = layout()
        raw.update(url="SECRET", title="SECRET", credentials="SECRET")
        raw["video"]["src"] = "SECRET"
        cleaned = probe.sanitize_layout(raw)
        self.assertNotIn("SECRET", json.dumps(cleaned))
        self.assertEqual(cleaned, layout())
        raw["video"]["rect"][0] = float("nan")
        with self.assertRaises(probe.ProbeError):
            probe.sanitize_layout(raw)

    def test_layout_navigation_away_rejected(self):
        with self.assertRaises(probe.ProbeError):
            probe.sanitize_layout({"target": False, "url": "SECRET"})

    def test_cdp_sends_only_fixed_readonly_expression(self):
        session = probe.CdpSession("ws://127.0.0.1:1234/devtools/page/ABC")
        session._ws = Mock()
        session._ws.recv.return_value = json.dumps({"id": 1, "result": {"result": {"value": layout()}}})
        self.assertEqual(session.layout(), layout())
        command = json.loads(session._ws.send.call_args.args[0])
        self.assertEqual(command["method"], "Runtime.evaluate")
        self.assertEqual(command["params"]["expression"], probe.LAYOUT_SCRIPT)
        self.assertFalse(command["params"]["userGesture"])

    def test_viewport_offset_never_guesses_titlebar(self):
        self.assertIsNone(probe.viewport_offset((1280, 760), (1280, 720), 1))
        self.assertEqual(probe.viewport_offset((1280, 720), (640, 360), 2), (0, 0))
        metrics = probe.video_metrics(layout(), (64, 50))
        self.assertIsNone(metrics["covers_client"])
        self.assertIsNone(metrics["content_rect_px"])

    def test_css_to_physical_roi(self):
        metrics = probe.video_metrics(layout(dpr=2), (128, 72))
        self.assertEqual(metrics["content_rect_px"], [0, 0, 128, 72])
        self.assertTrue(metrics["covers_client"])
        self.assertTrue(metrics["video_ready"])
        self.assertTrue(metrics["is_16_9"])

    def test_letterbox_is_not_covers_client(self):
        data = layout(64, 64)
        metrics = probe.video_metrics(data, (64, 64))
        self.assertEqual(metrics["content_rect_px"], [0, 14, 64, 36])
        self.assertFalse(metrics["covers_client"])

    def test_unknown_or_not_ready_video_never_passes(self):
        self.assertFalse(probe.video_metrics(None, (64, 36))["video_ready"])
        for key, value in (("width", 0), ("readyState", 1), ("paused", True), ("simple", False),
                           ("clear", False), ("visible", False)):
            data = layout()
            data["video"][key] = value
            with self.subTest(key=key):
                self.assertFalse(probe.video_metrics(data, (64, 36))["video_ready"])
        stats = [{"nonblack_ratio": 1, "spatial_std": 50}] * 3
        diffs = [{"compatible": True, "changed_ratio": 0.5}] * 2
        self.assertEqual(probe.capture_verdict(stats, diffs, state_achieved=True), "inconclusive")
        self.assertEqual(probe.capture_verdict(stats, diffs, video_ready=True, state_achieved=True), "pass")

    @unittest.skipIf(probe.np is None, "numpy unavailable")
    def test_frames_empty_and_outside_roi_rejected(self):
        for frame in (None, probe.np.zeros((0, 2, 3), dtype="uint8"), probe.np.zeros((4, 4), dtype="uint8")):
            with self.assertRaises(probe.ProbeError):
                probe.frame_stats(frame)
        frame = probe.np.zeros((4, 4, 3), dtype="uint8")
        for roi in ((-1, 0, 2, 2), (0, 0, 5, 4), (0, 0, 0, 3)):
            with self.assertRaises(probe.ProbeError):
                probe.frame_stats(frame, roi)

    @unittest.skipIf(probe.np is None, "numpy unavailable")
    def test_uniform_colored_occluder_is_not_video(self):
        frames = [probe.np.full((8, 8, 3), color, dtype="uint8")
                  for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
        stats = [probe.frame_stats(frame) for frame in frames]
        diffs = [probe.frame_diff(frames[i], frames[i + 1]) for i in range(2)]
        self.assertTrue(all(item["spatial_std"] == 0 for item in stats))
        self.assertEqual(probe.capture_verdict(stats, diffs, video_ready=True, state_achieved=True), "inconclusive")

    @unittest.skipIf(probe.np is None, "numpy unavailable")
    def test_shell_changes_outside_video_roi_do_not_pass(self):
        a = probe.np.zeros((10, 10, 3), dtype="uint8")
        b = a.copy()
        b[:2] = 200
        self.assertGreater(probe.frame_diff(a, b)["changed_ratio"], 0)
        self.assertEqual(probe.frame_diff(a, b, (2, 2, 6, 6))["changed_ratio"], 0)

    def test_requested_state_is_not_actual_state(self):
        self.assertTrue(probe.state_achieved("foreground", actual()))
        for name in ("background", "occluded", "minimized"):
            self.assertFalse(probe.state_achieved(name, actual()))
        self.assertTrue(probe.state_achieved("minimized", actual(minimized=True)))
        self.assertFalse(probe.state_achieved("foreground", actual(sampled_uncovered=False)))

    def test_window_minimize_failure_not_recorded_as_success(self):
        user = Mock()
        user.IsIconic.return_value = False
        with patch.object(probe, "bound_window", return_value=window()), \
             patch.object(probe, "_win32", return_value=SimpleNamespace(user=user)), \
             patch.object(probe.time, "sleep"):
            with probe.window_state(self.state, "minimized"):
                self.assertFalse(probe.state_achieved("minimized", actual()))
        user.ShowWindow.assert_called_once_with(42, 6)

    def test_input_requires_optin_and_single_action_method(self):
        for values in (["--click", "1,2"], ["--key", "ESC"], ["--allow-input"],
                       ["--allow-input", "--click", "1,2"],
                       ["--allow-input", "--key", "ESC", "--input-method", "SendMessageWithWindowPos"]):
            args = probe.build_parser().parse_args(["probe", *values])
            with self.subTest(values=values), self.assertRaises(probe.ProbeError):
                probe.validate_input_args(args)
        args = probe.build_parser().parse_args(["probe", "--allow-input", "--click", "1,2", "--input-method", "Seize"])
        self.assertTrue(probe.validate_input_args(args))

    def test_seize_requires_foreground_bound_point(self):
        user = Mock()
        user.IsIconic.return_value = False
        user.GetForegroundWindow.return_value = 42
        user.WindowFromPoint.return_value = 44
        user.GetAncestor.return_value = 42
        with patch.object(probe, "bound_window", return_value=window()), \
             patch.object(probe, "desktop_interactive", return_value=True), \
             patch.object(probe, "_win32", return_value=SimpleNamespace(user=user)):
            self.assertEqual(probe.input_guard(self.state, window(), "Seize", (2, 2)), window())
            user.GetAncestor.return_value = 999
            with self.assertRaises(probe.ProbeError):
                probe.input_guard(self.state, window(), "Seize", (2, 2))
            user.GetAncestor.return_value = 42
            user.GetForegroundWindow.return_value = 999
            with self.assertRaises(probe.ProbeError):
                probe.input_guard(self.state, window(), "Seize", (2, 2))

    def test_input_rejects_locked_desktop_geometry_change_and_outside_point(self):
        with patch.object(probe, "desktop_interactive", return_value=False):
            with self.assertRaises(probe.ProbeError):
                probe.input_guard(self.state, window(), "PostMessage", (1, 1))
        with patch.object(probe, "desktop_interactive", return_value=True), \
             patch.object(probe, "bound_window", return_value=window(hwnd=99)):
            with self.assertRaises(probe.ProbeError):
                probe.input_guard(self.state, window(), "PostMessage", (1, 1))

    def fake_native(self):
        api = SimpleNamespace(Win32Controller=Mock(),
                              Input=SimpleNamespace(Null=0, Seize=1, PostMessage=2),
                              Screencap=SimpleNamespace(PrintWindow=1, FramePool=2))
        controller = api.Win32Controller.return_value
        controller.cached_image = probe.np.zeros((36, 64, 3), dtype="uint8")
        patch.object(probe, "load_framework", return_value=api).start()
        patch.object(probe, "bound_window", return_value=window()).start()
        patch.object(probe, "wait_job", return_value=SimpleNamespace(succeeded=True)).start()
        patch.object(probe, "input_guard", return_value=window()).start()
        return api, controller

    @unittest.skipIf(probe.np is None, "numpy unavailable")
    def test_explicit_input_executes_once_and_effect_unverified(self):
        _, controller = self.fake_native()
        result = probe.native_worker({"state": self.state, "operation": "input", "allow_input": True,
                                      "method": "Seize", "click": [2, 3], "key": None,
                                      "expires": time.monotonic() + 100})
        controller.post_click.assert_called_once_with(2, 3)
        controller.post_click_key.assert_not_called()
        self.assertEqual(result["effect"], "unverified")
        self.assertTrue(result["native_success"])
        self.assertEqual(result["executions"], 1)
        self.assertIn(controller, probe._WORKER_KEEPALIVE)

    @unittest.skipIf(probe.np is None, "numpy unavailable")
    def test_expired_worker_never_dispatches_input(self):
        _, controller = self.fake_native()
        with self.assertRaises(probe.ProbeError):
            probe.native_worker({"state": self.state, "operation": "input", "allow_input": True,
                                 "method": "Seize", "click": [2, 3], "key": None,
                                 "expires": time.monotonic() - 1})
        controller.post_click.assert_not_called()
        controller.post_click_key.assert_not_called()

    @unittest.skipIf(probe.np is None, "numpy unavailable")
    def test_raw_frame_size_mismatch_prevents_click(self):
        _, controller = self.fake_native()
        controller.cached_image = probe.np.zeros((40, 64, 3), dtype="uint8")
        with self.assertRaises(probe.ProbeError):
            probe.native_worker({"state": self.state, "operation": "input", "allow_input": True,
                                 "method": "Seize", "click": [2, 3], "key": None,
                                 "expires": time.monotonic() + 100})
        controller.post_click.assert_not_called()

    @unittest.skipIf(probe.np is None, "numpy unavailable")
    def test_capture_worker_never_enables_input_or_passes_unknown_video(self):
        api, controller = self.fake_native()
        with patch.object(probe, "observe_state", return_value=actual()), patch.object(probe.time, "sleep"):
            result = probe.native_worker({"state": dict(self.state, cdp=False), "operation": "capture",
                                          "method": "PrintWindow", "requested_state": "foreground",
                                          "expires": time.monotonic() + 100})
        self.assertEqual(api.Win32Controller.call_args.kwargs["mouse_method"], 0)
        self.assertEqual(api.Win32Controller.call_args.kwargs["keyboard_method"], 0)
        controller.post_click.assert_not_called()
        controller.post_click_key.assert_not_called()
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertIsNone(result["layout"])
        self.assertEqual(len(result["frames"]), 3)

    @unittest.skipIf(probe.np is None, "numpy unavailable")
    def test_video_pass_requires_advancing_media_and_three_changed_frames(self):
        _, controller = self.fake_native()
        rng = probe.np.random.default_rng(3)
        frames = [rng.integers(20, 255, (36, 64, 3), dtype="uint8") for _ in range(3)]
        def capture():
            controller.cached_image = frames.pop(0)
            return Mock()
        controller.post_screencap.side_effect = capture
        after = layout()
        after["video"]["currentTime"] = 2
        cdp = Mock()
        cdp.layout.side_effect = [layout(), after]
        with patch.object(probe, "connect_cdp", return_value=cdp), \
             patch.object(probe, "observe_state", return_value=actual()), patch.object(probe.time, "sleep"):
            result = probe.native_worker({"state": self.state, "operation": "capture",
                                          "method": "FramePool", "requested_state": "foreground",
                                          "expires": time.monotonic() + 100})
        self.assertEqual(result["verdict"], "pass")
        self.assertTrue(result["media_time_advanced"])
        cdp.close.assert_called_once()

    def test_worker_timeout_kills_only_launched_worker(self):
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired("SECRET", 1)
        process.poll.return_value = None
        with patch.object(probe.subprocess, "Popen", return_value=process):
            with self.assertRaises(subprocess.TimeoutExpired):
                probe.run_worker({"operation": "capture"}, timeout=1)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)

    def test_worker_interrupt_also_reaps(self):
        process = Mock()
        process.communicate.side_effect = KeyboardInterrupt()
        process.poll.return_value = None
        with patch.object(probe.subprocess, "Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                probe.run_worker({"operation": "capture"})
        process.kill.assert_called_once()

    def test_worker_arbitrary_exception_body_is_not_returned(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (b'{"error":"SECRET"}', None)
        process.poll.return_value = 0
        with patch.object(probe.subprocess, "Popen", return_value=process):
            with self.assertRaises(probe.ProbeError) as caught:
                probe.run_worker({"operation": "capture"})
        self.assertEqual(str(caught.exception), "worker_failed")

    def report_from(self, output):
        if output.get("report"):
            return json.loads(Path(output["report"]).read_text(encoding="utf-8"))
        return output["inline_report"]

    def test_probe_default_zero_input_closes_and_writes_report(self):
        with patch.object(probe, "_load_state", return_value=self.state), \
             patch.object(probe, "bound_window", return_value=window()), \
             patch.object(probe, "capture_matrix", return_value={}), \
             patch.object(probe, "close_browser", return_value=True) as close, \
             patch.object(probe, "run_worker") as worker:
            code, output = probe.run_probe(self.args)
        self.assertEqual(code, 0)
        close.assert_called_once_with(self.state)
        worker.assert_not_called()
        report = self.report_from(output)
        self.assertEqual(report["input"], {"enabled": False, "executions": 0})
        self.assertEqual(report["gate"], "inconclusive")
        self.assertTrue(report["closed"])
        self.assertEqual(list(self.output.rglob("*.png")), [])

    def test_probe_exception_and_interrupt_always_report_and_close(self):
        for exc in (ValueError("SECRET URL"), KeyboardInterrupt("SECRET"), probe.ProbeError("screencap_failed")):
            with self.subTest(exc=type(exc).__name__), \
                 patch.object(probe, "_load_state", return_value=self.state), \
                 patch.object(probe, "bound_window", return_value=window()), \
                 patch.object(probe, "capture_matrix", side_effect=exc), \
                 patch.object(probe, "close_browser", return_value=True) as close:
                code, output = probe.run_probe(self.args)
            self.assertEqual(code, 130 if isinstance(exc, KeyboardInterrupt) else 1)
            close.assert_called_once_with(self.state)
            report = self.report_from(output)
            self.assertTrue(report["closed"])
            self.assertNotIn("SECRET", json.dumps(report))
            self.assertTrue(report["errors"])

    def test_probe_bad_state_reports_without_closing_stranger(self):
        with patch.object(probe, "_load_state", side_effect=probe.ProbeError("state_missing")), \
             patch.object(probe, "close_browser") as close:
            code, output = probe.run_probe(self.args)
        self.assertEqual(code, 1)
        close.assert_not_called()
        self.assertEqual(self.report_from(output)["errors"], ["state_missing"])

    def test_probe_close_failure_reported_without_success(self):
        with patch.object(probe, "_load_state", return_value=self.state), \
             patch.object(probe, "bound_window", return_value=window()), \
             patch.object(probe, "capture_matrix", return_value={}), \
             patch.object(probe, "close_browser", side_effect=probe.ProbeError("process_mismatch")):
            code, output = probe.run_probe(self.args)
        self.assertEqual(code, 1)
        self.assertFalse(self.report_from(output)["closed"])

    def test_report_write_failure_has_inline_sanitized_report_after_cleanup(self):
        with patch.object(probe, "_load_state", return_value=self.state), \
             patch.object(probe, "bound_window", return_value=window()), \
             patch.object(probe, "capture_matrix", return_value={}), \
             patch.object(probe, "close_browser", return_value=True) as close, \
             patch.object(probe, "write_json", side_effect=OSError("SECRET disk path")):
            code, output = probe.run_probe(self.args)
        close.assert_called_once()
        self.assertEqual(code, 1)
        self.assertIsNone(output["report"])
        self.assertIn("report_write_failed", output["inline_report"]["errors"])
        self.assertNotIn("SECRET", json.dumps(output))

    def test_launch_state_write_failure_closes_owned_process(self):
        args = probe.build_parser().parse_args(["launch", "--state", str(self.state_path),
                                               "--output", str(self.output), "--profile", str(self.profile)])
        process = Mock()
        with patch.object(probe, "find_edge", return_value=Path(self.state["exe"])), \
             patch.object(probe, "launch_browser", return_value=(process, self.state)), \
             patch.object(probe, "write_json", side_effect=OSError("SECRET")), \
             patch.object(probe, "close_browser", return_value=True) as close:
            code, output = probe.run_launch(args)
        self.assertEqual(code, 1)
        close.assert_called_once_with(self.state)
        self.assertNotIn("SECRET", json.dumps(output))

    def test_launch_interrupt_reaps_spawned_process(self):
        (self.profile / ".probe-lock").unlink()
        process = Mock(pid=123)
        process.poll.return_value = None
        process._handle = 77
        with patch.object(probe, "physical_coordinates"), \
             patch.object(probe, "desktop_interactive", return_value=True), \
             patch.object(probe.subprocess, "Popen", return_value=process), \
             patch.object(probe, "process_identity", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                probe.launch_browser(Path(self.state["exe"]), self.profile, url=probe.DEFAULT_URL,
                                     size=(1280, 720), layout="app", cdp=True, keep_rendering=True)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        self.assertFalse((self.profile / ".probe-lock").exists())

    def test_existing_profile_lock_never_spawns_browser(self):
        with patch.object(probe, "physical_coordinates"), \
             patch.object(probe, "desktop_interactive", return_value=True), \
             patch.object(probe.subprocess, "Popen") as popen:
            with self.assertRaises(probe.ProbeError):
                probe.launch_browser(Path(self.state["exe"]), self.profile, url=probe.DEFAULT_URL,
                                     size=(1280, 720), layout="app", cdp=False, keep_rendering=True)
        popen.assert_not_called()
        self.assertEqual((self.profile / ".probe-lock").read_text(), self.token)

    def test_retire_state_only_removes_same_launch_token(self):
        probe.write_json(self.state_path, dict(self.state, token="b" * 32))
        probe.retire_state(self.state_path, self.state)
        self.assertTrue(self.state_path.exists())
        probe.write_json(self.state_path, self.state)
        probe.retire_state(self.state_path, self.state)
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
