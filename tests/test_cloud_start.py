"""cloud_start 冷启动入口的离线测试：不启动真实客户端或 MXU，不发送输入。"""

import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import cloud_start  # noqa: E402


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode


class CloudStartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="maante-cloud-start-")
        self.addCleanup(temporary.cleanup)
        self.exe = Path(temporary.name) / "NTECloudGame.exe"
        self.exe.write_bytes(b"")
        self.clock = [0.0]
        patcher = patch.object(
            cloud_start.time, "monotonic", side_effect=lambda: self.clock[0]
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch.object(
            cloud_start.time,
            "sleep",
            side_effect=lambda seconds: self.clock.__setitem__(
                0, self.clock[0] + max(seconds, 0.1)
            ),
        )
        sleeper.start()
        self.addCleanup(sleeper.stop)
        platform = patch.object(cloud_start.sys, "platform", "win32")
        platform.start()
        self.addCleanup(platform.stop)

    def window(self, hwnd=100, pid=7):
        return cloud_start.ClientWindow(hwnd=hwnd, pid=pid, size=(1280, 720))

    def assert_safe_logs(self, logs):
        text = "\n".join(logs.output) + repr([record.args for record in logs.records])
        for secret in ("fake-password", "fake-token", "private-account", "secret-instance", "secret-title"):
            self.assertNotIn(secret, text)
        for record in logs.records:
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.stack_info)

    def prepare_api(self, width=800, height=450, work_width=1920, work_height=1080):
        user = Mock()
        state = {"client": (width, height), "outer": (100, 100, width + 116, height + 139)}

        def fill(pointer, values):
            rect = pointer._obj
            rect.left, rect.top, rect.right, rect.bottom = values
            return 1

        def reposition(hwnd, after, left, top, outer_width, outer_height, flags):
            state["outer"] = (left, top, left + outer_width, top + outer_height)
            state["client"] = (outer_width - 16, outer_height - 39)
            return 1

        user.SetThreadDpiAwarenessContext.return_value = 123
        user.IsIconic.return_value = 0
        user.GetClientRect.side_effect = lambda hwnd, rect: fill(rect, (0, 0, *state["client"]))
        user.GetWindowRect.side_effect = lambda hwnd, rect: fill(rect, state["outer"])
        user.SystemParametersInfoW.side_effect = lambda flag, unused, rect, other: fill(
            rect, (0, 0, work_width, work_height)
        )
        user.SetWindowPos.side_effect = reposition
        return user, state

    def test_executable_validation_rejects_wrong_targets(self):
        for value in (None, "", "relative.exe", str(ROOT / "missing.exe")):
            with self.subTest(value=value), self.assertRaises(cloud_start.ClientError):
                cloud_start.validate_executable(value, cloud_start.CLIENT_NAME)
        other = self.exe.with_name("Other.exe")
        other.write_bytes(b"")
        self.addCleanup(other.unlink)
        with self.assertRaises(cloud_start.ClientError):
            cloud_start.validate_executable(other, cloud_start.CLIENT_NAME)
        self.assertEqual(
            cloud_start.validate_executable(str(self.exe), "ntecloudgame.EXE"),
            self.exe.resolve(),
        )

    def test_invalid_timeout_is_rejected_before_launch(self):
        with patch.object(cloud_start.subprocess, "Popen") as popen:
            for value in (0, -1, True, 301, float("nan")):
                with self.subTest(value=value), self.assertRaises(
                    cloud_start.ClientError
                ):
                    cloud_start.start_client(self.exe, timeout=value)
            popen.assert_not_called()

    def test_existing_client_is_reused_without_second_launch(self):
        with patch.object(cloud_start, "client_pids", return_value={7}), patch.object(
            cloud_start, "client_windows", side_effect=[[], [self.window()]]
        ), patch.object(cloud_start, "window_responding", return_value=True), patch.object(
            cloud_start.subprocess, "Popen"
        ) as popen:
            window, process = cloud_start.start_client(self.exe, timeout=30)
        popen.assert_not_called()
        self.assertIsNone(process)
        self.assertEqual(window.hwnd, 100)

    def test_launch_waits_for_window_and_never_uses_shell(self):
        fake = FakeProcess()
        with patch.object(cloud_start, "client_pids", return_value=set()), patch.object(
            cloud_start, "client_windows", side_effect=[[], [], [self.window()]]
        ), patch.object(cloud_start, "window_responding", return_value=True), patch.object(
            cloud_start.subprocess, "Popen", return_value=fake
        ) as popen:
            window, process = cloud_start.start_client(self.exe, timeout=30)
        self.assertIs(process, fake)
        self.assertFalse(fake.terminated)
        self.assertEqual(window.hwnd, 100)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(self.exe.resolve())])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], str(self.exe.resolve().parent))

    def test_timeout_and_ambiguous_windows_only_clean_owned_process(self):
        for windows, error in (
            ([], "client_start_timeout"),
            ([self.window(1), self.window(2)], "ambiguous_window"),
        ):
            with self.subTest(error=error):
                fake = FakeProcess()
                with patch.object(
                    cloud_start, "client_pids", return_value=set()
                ), patch.object(
                    cloud_start, "client_windows", return_value=windows
                ), patch.object(
                    cloud_start, "window_responding", return_value=True
                ), patch.object(cloud_start.subprocess, "Popen", return_value=fake):
                    with self.assertRaises(cloud_start.ClientError) as caught:
                        cloud_start.start_client(self.exe, timeout=2)
                self.assertEqual(str(caught.exception), error)
                self.assertTrue(fake.terminated)

    def test_reused_client_is_never_terminated_on_failure(self):
        with patch.object(cloud_start, "client_pids", return_value={7}), patch.object(
            cloud_start, "client_windows", return_value=[]
        ), patch.object(cloud_start.subprocess, "Popen") as popen, patch.object(
            cloud_start, "_terminate_owned"
        ) as cleanup:
            with self.assertRaises(cloud_start.ClientError):
                cloud_start.start_client(self.exe, timeout=2)
        popen.assert_not_called()
        cleanup.assert_called_once_with(None)

    def test_unresponsive_window_is_not_ready(self):
        fake = FakeProcess()
        with patch.object(cloud_start, "client_pids", return_value=set()), patch.object(
            cloud_start, "client_windows", return_value=[self.window()]
        ), patch.object(cloud_start, "window_responding", return_value=False), patch.object(
            cloud_start.subprocess, "Popen", return_value=fake
        ):
            with self.assertRaises(cloud_start.ClientError) as caught:
                cloud_start.start_client(self.exe, timeout=2)
        self.assertEqual(str(caught.exception), "client_start_timeout")

    def test_updater_relaunch_is_awaited_after_launcher_exits(self):
        # 实机：NTECloudGame.exe 先退出并交给 NTECloudUpdate.exe，随后由更新器重新拉起。
        fake = FakeProcess()
        fake.returncode = 0
        with patch.object(cloud_start, "client_pids", return_value=set()), patch.object(
            cloud_start, "client_windows", side_effect=[[], [], [], [self.window()]]
        ), patch.object(cloud_start, "window_responding", return_value=True), patch.object(
            cloud_start.subprocess, "Popen", return_value=fake
        ):
            window, process = cloud_start.start_client(self.exe, timeout=30)
        self.assertEqual(window.hwnd, 100)
        self.assertIs(process, fake)
        self.assertFalse(fake.terminated)
        self.assertFalse(fake.killed)

    def test_desktop_interactive_reports_disconnected_session(self):
        with patch.object(cloud_start.sys, "platform", "linux"):
            self.assertFalse(cloud_start.desktop_interactive())
        user = Mock()
        user.OpenInputDesktop.return_value = 0
        with patch.object(cloud_start, "_apis", return_value=(user, Mock())):
            self.assertFalse(cloud_start.desktop_interactive())
            user.CloseDesktop.assert_not_called()
            user.OpenInputDesktop.return_value = 42
            self.assertTrue(cloud_start.desktop_interactive())
            user.CloseDesktop.assert_called_once_with(42)

    def test_stop_request_exits_promptly(self):
        fake = FakeProcess()
        calls = []
        with patch.object(cloud_start, "client_pids", return_value=set()), patch.object(
            cloud_start, "client_windows", return_value=[]
        ), patch.object(cloud_start.subprocess, "Popen", return_value=fake):
            with self.assertRaises(cloud_start.ClientError) as caught:
                cloud_start.start_client(
                    self.exe, timeout=60, stopped=lambda: calls.append(1) or len(calls) > 1
                )
        self.assertEqual(str(caught.exception), "stopped")
        self.assertLessEqual(self.clock[0], 0.5)
        self.assertTrue(fake.terminated)

    def test_cli_launches_mxu_after_ready_and_keeps_arguments_intact(self):
        mxu = self.exe.with_name("MXU.exe")
        mxu.write_bytes(b"")
        self.addCleanup(mxu.unlink)
        with patch.object(
            cloud_start, "start_client", return_value=(self.window(), None)
        ), patch.object(cloud_start.subprocess, "Popen") as popen, patch.object(
            cloud_start, "desktop_interactive", return_value=True
        ):
            code = cloud_start.main(
                ["--client", str(self.exe), "--mxu", str(mxu), "--instance", "云 异环 实例"]
            )
        self.assertEqual(code, 0)
        args, kwargs = popen.call_args
        self.assertEqual(
            args[0], [str(mxu.resolve()), "--autostart", "--instance", "云 异环 实例"]
        )
        self.assertFalse(kwargs["shell"])

    def test_cli_rejects_instance_without_mxu_and_reports_failures(self):
        with patch.object(cloud_start, "start_client") as start:
            self.assertEqual(
                cloud_start.main(["--client", str(self.exe), "--instance", "x"]), 1
            )
            start.assert_not_called()
        with patch.object(
            cloud_start, "start_client", side_effect=cloud_start.ClientError("client_exited")
        ):
            self.assertEqual(cloud_start.main(["--client", str(self.exe)]), 1)
        with patch.object(cloud_start, "start_client", side_effect=KeyboardInterrupt):
            self.assertEqual(cloud_start.main(["--client", str(self.exe)]), 130)

    def test_long_poll_logs_only_wait_state_changes_and_ready(self):
        fake = FakeProcess()
        with patch.object(cloud_start, "client_pids", return_value=set()), patch.object(
            cloud_start, "client_windows", side_effect=[[]] * 100 + [[self.window()]] * 101
        ) as windows, patch.object(
            cloud_start, "window_responding", side_effect=[False] * 100 + [True]
        ), patch.object(cloud_start.subprocess, "Popen", return_value=fake), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            window, process = cloud_start.start_client(self.exe, timeout=90)
        text = "\n".join(logs.output)
        self.assertEqual(windows.call_count, 201)
        self.assertIs(process, fake)
        self.assertEqual(window, self.window())
        self.assertEqual(text.count("reason=window_missing"), 1)
        self.assertEqual(text.count("reason=window_unresponsive"), 1)
        self.assertIn("INFO:maante:已启动本地云客户端", text)
        self.assertIn("WARNING:maante:本地云客户端窗口等待状态变化", text)
        self.assertIn("stage=start.ready | candidates=1 | client=1280x720 | elapsed=20.000s", text)
        self.assertLess(len(logs.records), 10)
        self.assertFalse(fake.terminated)

    def test_reuse_and_updater_progress_are_logged_once(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                fake = FakeProcess()
                fake.returncode = 0
                with patch.object(
                    cloud_start, "client_pids", return_value={7, 8} if reuse else set()
                ), patch.object(
                    cloud_start, "client_windows", side_effect=[[]] * 100 + [[self.window()]]
                ), patch.object(
                    cloud_start, "window_responding", return_value=True
                ), patch.object(
                    cloud_start.subprocess, "Popen", return_value=fake
                ) as popen, self.assertLogs("maante", level="DEBUG") as logs:
                    cloud_start.start_client(self.exe, timeout=30)
                text = "\n".join(logs.output)
                self.assertEqual(text.count("reason=window_missing"), 1)
                if reuse:
                    popen.assert_not_called()
                    self.assertIn("stage=start.reuse | processes=2", text)
                    self.assertNotIn("stage=start.updater", text)
                else:
                    popen.assert_called_once()
                    self.assertEqual(text.count("stage=start.updater"), 1)
                    self.assertIn("returncode=0", text)
                self.assertTrue(all(record.levelno < logging.ERROR for record in logs.records))
                self.assertFalse(fake.terminated)

    def test_timeout_logs_terminal_reason_then_owned_cleanup(self):
        fake = FakeProcess()
        with patch.object(cloud_start, "client_pids", return_value=set()), patch.object(
            cloud_start, "client_windows", return_value=[]
        ), patch.object(cloud_start.subprocess, "Popen", return_value=fake), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            with self.assertRaisesRegex(cloud_start.ClientError, "client_start_timeout"):
                cloud_start.start_client(self.exe, timeout=2)
        errors = [record.getMessage() for record in logs.records if record.levelno == logging.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("stage=start.wait | result=failed | reason=client_start_timeout | elapsed=2.000s", errors[0])
        text = "\n".join(logs.output)
        self.assertEqual(text.count("reason=window_missing"), 1)
        self.assertIn("stage=cleanup.done | method=terminate", text)
        self.assertTrue(fake.terminated)

    def test_cancellation_logs_warning_and_preserves_cleanup_and_raise(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                fake = FakeProcess()
                failure = KeyboardInterrupt("fake-password fake-token private-account")
                with patch.object(
                    cloud_start, "client_pids", return_value=set()
                ), patch.object(
                    cloud_start, "client_windows", side_effect=failure if interrupt else None,
                    return_value=[],
                ), patch.object(
                    cloud_start.subprocess, "Popen", return_value=fake
                ), self.assertLogs("maante", level="DEBUG") as logs:
                    with self.assertRaises(KeyboardInterrupt if interrupt else cloud_start.ClientError):
                        cloud_start.start_client(
                            self.exe, timeout=60,
                            stopped=Mock(side_effect=[False, False, True]),
                        )
                warnings = [record.getMessage() for record in logs.records if record.levelno == logging.WARNING]
                self.assertEqual(len(warnings), 1)
                self.assertIn("result=cancelled", warnings[0])
                self.assertIn("reason=interrupted" if interrupt else "reason=stopped", warnings[0])
                self.assertTrue(all(record.levelno < logging.ERROR for record in logs.records))
                self.assertTrue(fake.terminated)
                self.assert_safe_logs(logs)

    def test_start_exceptions_keep_identity_and_redact_untrusted_error_bodies(self):
        secret = r"password=fake-password token=fake-token E:\private-account\client.exe --instance secret-instance secret-title"
        for failure, reason in (
            (OSError(secret), "os_error"),
            (cloud_start.ClientError(secret), "client_error"),
            (RuntimeError(secret), "unexpected_error"),
        ):
            with self.subTest(reason=reason):
                fake = FakeProcess()
                with patch.object(
                    cloud_start, "client_pids", return_value=set()
                ), patch.object(
                    cloud_start, "client_windows", side_effect=failure
                ), patch.object(
                    cloud_start.subprocess, "Popen", return_value=fake
                ), self.assertLogs("maante", level="DEBUG") as logs:
                    with self.assertRaises(type(failure)) as raised:
                        cloud_start.start_client(self.exe)
                self.assertIs(raised.exception, failure)
                self.assertTrue(fake.terminated)
                errors = [record.getMessage() for record in logs.records if record.levelno == logging.ERROR]
                self.assertEqual(len(errors), 1)
                self.assertIn("stage=start.windows", errors[0])
                self.assertIn("reason=" + reason, errors[0])
                self.assert_safe_logs(logs)

    def test_ambiguous_candidates_are_counted_without_window_identity(self):
        with patch.object(cloud_start, "client_pids", return_value={7}), patch.object(
            cloud_start, "client_windows", return_value=[self.window(87654321), self.window(87654322)]
        ), self.assertLogs("maante", level="DEBUG") as logs:
            with self.assertRaisesRegex(cloud_start.ClientError, "ambiguous_window"):
                cloud_start.start_client(self.exe)
        text = "\n".join(logs.output)
        self.assertIn("candidates=2", text)
        self.assertIn("stage=start.windows | result=failed | reason=ambiguous_window", text)
        self.assertNotIn("87654321", text)
        self.assertNotIn("87654322", text)

    def test_owned_cleanup_timeout_logs_escalation_without_process_data(self):
        fake = FakeProcess()
        timeout = subprocess.TimeoutExpired(
            ["fake-password", "fake-token", r"E:\private-account\client.exe"], 5,
            output=b"secret-title", stderr=b"secret-instance",
        )
        with patch.object(fake, "wait", side_effect=[timeout, 1]), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            cloud_start._terminate_owned(fake)
        text = "\n".join(logs.output)
        self.assertIn("stage=cleanup.wait | reason=terminate_timeout | timeout=5s", text)
        self.assertIn("stage=cleanup.done | method=kill", text)
        self.assertTrue(fake.killed)
        self.assert_safe_logs(logs)

    def test_cleanup_swallowed_and_propagated_failures_keep_original_semantics(self):
        secret = "fake-password fake-token private-account secret-instance secret-title"
        fake = FakeProcess()
        with patch.object(fake, "terminate", side_effect=PermissionError(secret)), self.assertLogs(
            "maante", level="WARNING"
        ) as logs:
            cloud_start._terminate_owned(fake)
        self.assertIn("stage=cleanup.terminate | reason=permission_denied", logs.output[0])
        self.assert_safe_logs(logs)
        failure = OSError(secret)
        with patch.object(
            fake, "wait", side_effect=subprocess.TimeoutExpired([secret], 5)
        ), patch.object(fake, "kill", side_effect=failure), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            with self.assertRaises(OSError) as raised:
                cloud_start._terminate_owned(fake)
        self.assertIs(raised.exception, failure)
        self.assertIn("stage=cleanup.kill | reason=os_error", logs.output[-1])
        self.assert_safe_logs(logs)

    def test_start_primary_exception_survives_cleanup_exceptions(self):
        secret = "fake-password fake-token private-account secret-instance"
        for primary in (
            cloud_start.ClientError("client_start_timeout"),
            OSError(secret), RuntimeError(secret), KeyboardInterrupt(secret),
        ):
            for cleanup in (OSError(secret), RuntimeError(secret), KeyboardInterrupt(secret)):
                with self.subTest(primary=type(primary).__name__, cleanup=type(cleanup).__name__):
                    fake = FakeProcess()
                    with patch.object(cloud_start, "client_pids", return_value=set()), patch.object(
                        cloud_start, "client_windows", side_effect=primary
                    ), patch.object(cloud_start.subprocess, "Popen", return_value=fake), patch.object(
                        cloud_start, "_terminate_owned", side_effect=cleanup
                    ) as terminate, self.assertLogs("maante", level="DEBUG") as logs:
                        with self.assertRaises(BaseException) as raised:
                            cloud_start.start_client(self.exe)
                    self.assertIs(raised.exception, primary)
                    terminate.assert_called_once_with(fake)
                    self.assert_safe_logs(logs)

    def test_start_stopped_survives_real_cleanup_kill_wait_failure(self):
        fake = FakeProcess()
        cleanup = OSError("fake-password fake-token")
        with patch.object(cloud_start, "client_pids", return_value=set()), patch.object(
            cloud_start.subprocess, "Popen", return_value=fake
        ), patch.object(fake, "wait", side_effect=[
            subprocess.TimeoutExpired(["secret-instance"], 5), cleanup,
        ]) as wait, self.assertLogs("maante", level="DEBUG") as logs:
            with self.assertRaisesRegex(cloud_start.ClientError, "^stopped$"):
                cloud_start.start_client(self.exe, stopped=Mock(side_effect=[False, True]))
        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)
        self.assertEqual(wait.call_count, 2)
        self.assertIn("stage=cleanup.kill_wait", "\n".join(logs.output))
        self.assert_safe_logs(logs)

    def test_cli_primary_exit_codes_survive_cleanup_exceptions(self):
        for primary, expected in (
            (KeyboardInterrupt("fake-token"), 130),
            (OSError("fake-password"), 1),
            (cloud_start.ClientError("client_start_timeout"), 1),
        ):
            for cleanup in (OSError("private-account"), KeyboardInterrupt("secret-instance")):
                with self.subTest(primary=type(primary).__name__, cleanup=type(cleanup).__name__):
                    fake = FakeProcess()
                    with patch.object(cloud_start, "start_client", return_value=(self.window(), fake)), patch.object(
                        cloud_start, "desktop_interactive", side_effect=primary
                    ), patch.object(cloud_start, "_terminate_owned", side_effect=cleanup) as terminate, self.assertLogs(
                        "maante", level="DEBUG"
                    ) as logs:
                        try:
                            code = cloud_start.main(["--client", str(self.exe)])
                        except BaseException as failure:
                            self.fail("cleanup escaped CLI: %s" % type(failure).__name__)
                        self.assertEqual(code, expected)
                    terminate.assert_called_once_with(fake)
                    self.assert_safe_logs(logs)

    def test_reused_client_failure_only_passes_none_to_cleanup(self):
        primary = cloud_start.ClientError("ambiguous_window")
        with patch.object(cloud_start, "client_pids", return_value={7}), patch.object(
            cloud_start, "client_windows", side_effect=primary
        ), patch.object(cloud_start.subprocess, "Popen") as spawn, patch.object(
            cloud_start, "_terminate_owned"
        ) as terminate, self.assertLogs("maante", level="DEBUG"):
            with self.assertRaises(cloud_start.ClientError) as raised:
                cloud_start.start_client(self.exe)
        self.assertIs(raised.exception, primary)
        spawn.assert_not_called()
        terminate.assert_called_once_with(None)

    def test_prepare_resize_reports_dimensions_result_and_restores_dpi(self):
        user, state = self.prepare_api()
        with patch.object(cloud_start, "_apis", return_value=(user, Mock())), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            self.assertTrue(cloud_start.prepare_client_window(87654321))
        text = "\n".join(logs.output)
        self.assertIn("stage=window.resize | target=1280x720", text)
        self.assertIn("result=ready | reason=resized | client=1280x720", text)
        self.assertIn("elapsed=0.000s", text)
        self.assertNotIn("87654321", text)
        self.assertEqual(state["client"], (1280, 720))
        user.SetWindowPos.assert_called_once()
        self.assertEqual(user.SetThreadDpiAwarenessContext.call_count, 2)
        user.SetThreadDpiAwarenessContext.assert_called_with(123)

    def test_prepare_already_ready_and_small_work_area_have_distinct_reasons(self):
        user, state = self.prepare_api(width=1280, height=720)
        state["outer"] = (312, 160, 1608, 919)
        with patch.object(cloud_start, "_apis", return_value=(user, Mock())), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            self.assertTrue(cloud_start.prepare_client_window(100))
        self.assertIn("reason=already_ready", logs.output[-1])
        user.SetWindowPos.assert_not_called()
        user, _ = self.prepare_api(work_width=1024, work_height=600)
        with patch.object(cloud_start, "_apis", return_value=(user, Mock())), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            self.assertFalse(cloud_start.prepare_client_window(100))
        self.assertIn("required=1296x759 | work=1024x600", "\n".join(logs.output))
        self.assertEqual(logs.records[-1].levelno, logging.WARNING)
        self.assertIn("reason=work_area_too_small", logs.output[-1])
        user.SetWindowPos.assert_not_called()
        user.SetThreadDpiAwarenessContext.assert_called_with(123)

    def test_prepare_exceptions_log_actual_stage_without_masking_error(self):
        user, _ = self.prepare_api()
        failure = OSError(r"fake-password fake-token E:\private-account\secret-title")
        user.GetClientRect.side_effect = failure
        with patch.object(cloud_start, "_apis", return_value=(user, Mock())), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            with self.assertRaises(OSError) as raised:
                cloud_start.prepare_client_window(100)
        self.assertIs(raised.exception, failure)
        self.assertIn("stage=window.bounds | result=failed | reason=os_error", logs.output[-1])
        user.SetThreadDpiAwarenessContext.assert_called_with(123)
        self.assert_safe_logs(logs)

    def test_cli_mxu_logs_stages_and_redacts_launch_failure(self):
        mxu = self.exe.with_name("MXU.exe")
        for failure in (None, PermissionError("fake-password fake-token private-account")):
            with self.subTest(failure=failure is not None):
                fake = FakeProcess()
                with patch.object(
                    cloud_start, "validate_executable", return_value=mxu
                ), patch.object(
                    cloud_start, "start_client", return_value=(self.window(), fake)
                ), patch.object(
                    cloud_start, "desktop_interactive", return_value=True
                ), patch.object(
                    cloud_start.subprocess, "Popen", side_effect=failure
                ) as popen, self.assertLogs("maante", level="DEBUG") as logs:
                    code = cloud_start.main([
                        "--client", str(self.exe), "--mxu", str(mxu),
                        "--instance", "secret-instance secret-title",
                    ])
                text = "\n".join(logs.output)
                self.assertIn("stage=cli.mxu_launch | mode=autostart", text)
                self.assertEqual(popen.call_args.args[0][-1], "secret-instance secret-title")
                self.assertEqual(code, 1 if failure else 0)
                self.assertEqual(fake.terminated, failure is not None)
                if failure:
                    self.assertEqual(logs.records[-1].levelno, logging.ERROR)
                    self.assertIn("stage=cli.mxu_launch | reason=permission_denied", text)
                else:
                    self.assertIn("stage=cli.mxu_ready", text)
                    self.assertIn("stage=cli.ready", text)
                self.assert_safe_logs(logs)

    def test_cli_start_failure_is_not_reported_twice_and_uses_safe_codes(self):
        with self.assertLogs("maante", level="DEBUG") as logs:
            code = cloud_start.main(["--client", "fake-password fake-token private-account.exe"])
        self.assertEqual(code, 1)
        errors = [record for record in logs.records if record.levelno == logging.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("stage=start.executable | result=failed | reason=invalid_executable", errors[0].getMessage())
        self.assertIn("stage=cli.client | reason=invalid_executable", logs.output[-1])
        self.assertEqual(logs.records[-1].levelno, logging.DEBUG)
        self.assert_safe_logs(logs)

    def test_cli_validation_and_keyboard_interrupt_have_safe_stage_context(self):
        with self.assertLogs("maante", level="DEBUG") as logs:
            code = cloud_start.main(["--client", str(self.exe), "--instance", "secret-instance"])
        self.assertEqual(code, 1)
        self.assertEqual(logs.records[-1].levelno, logging.ERROR)
        self.assertIn("stage=cli.arguments | reason=instance_requires_mxu", logs.output[-1])
        self.assert_safe_logs(logs)
        fake = FakeProcess()
        with patch.object(
            cloud_start, "start_client", return_value=(self.window(), fake)
        ), patch.object(
            cloud_start, "desktop_interactive", side_effect=KeyboardInterrupt("fake-password fake-token")
        ), self.assertLogs("maante", level="DEBUG") as logs:
            code = cloud_start.main(["--client", str(self.exe)])
        self.assertEqual(code, 130)
        self.assertTrue(fake.terminated)
        self.assertEqual(logs.records[-1].levelno, logging.WARNING)
        self.assertIn("stage=cli.desktop | reason=interrupted", logs.output[-1])
        self.assert_safe_logs(logs)


if __name__ == "__main__":
    unittest.main()
