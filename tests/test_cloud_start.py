"""cloud_start 冷启动入口的离线测试：不启动真实客户端或 MXU，不发送输入。"""

import subprocess
import sys
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
        self.exe = ROOT / "tests" / "fixtures" / "NTECloudGame.exe"
        self.exe.parent.mkdir(parents=True, exist_ok=True)
        self.exe.write_bytes(b"")
        self.addCleanup(self.exe.unlink)
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
        mxu = ROOT / "tests" / "fixtures" / "MXU.exe"
        mxu.write_bytes(b"")
        self.addCleanup(mxu.unlink)
        with patch.object(
            cloud_start, "start_client", return_value=(self.window(), None)
        ), patch.object(cloud_start.subprocess, "Popen") as popen:
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


if __name__ == "__main__":
    unittest.main()
