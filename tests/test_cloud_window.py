"""窗口辅助回归：Win32 和输入全部 mock，不连接或操作真实客户端。"""

import ctypes
from contextlib import contextmanager
import importlib.util
import io
import logging
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import numpy as np


MODULE_PATH = Path(__file__).resolve().parents[1] / "agent/utils/cloud_window.py"


def load_module():
    spec = importlib.util.spec_from_file_location("_test_cloud_window", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # 工作进程和测试均不依赖项目 utils 聚合导入，也不需要 MaaFramework。
    with patch.dict(sys.modules, {"utils": None, "utils.logger": None, "maa": None}):
        spec.loader.exec_module(module)
    return module


cloud = load_module()


class FakeWin32:
    HWND = 100
    CHILD = 101

    def __init__(self, width=100, height=60):
        self.width, self.height = width, height
        self.selected = 40
        self.bitmap_info = None
        self.user32 = Mock()
        self.gdi32 = Mock()
        self.user32.IsWindow.return_value = 1
        self.user32.IsWindowVisible.return_value = 1
        self.user32.IsWindowEnabled.return_value = 1
        self.user32.IsIconic.return_value = 0
        self.user32.GetAncestor.side_effect = (
            lambda hwnd, flag: self.HWND if hwnd == self.CHILD else hwnd
        )
        self.user32.GetClientRect.side_effect = self.client_rect
        self.user32.PostMessageW.return_value = 1
        self.user32.SetThreadDpiAwarenessContext.return_value = 123
        self.user32.GetDC.return_value = 10
        self.user32.ReleaseDC.return_value = 1
        self.user32.PrintWindow.return_value = 1
        self.gdi32.CreateCompatibleDC.return_value = 20
        self.gdi32.CreateCompatibleBitmap.return_value = 30
        self.gdi32.SelectObject.side_effect = self.select_object
        self.gdi32.PatBlt.return_value = 1
        self.gdi32.GetDIBits.side_effect = self.get_dibits
        self.gdi32.DeleteDC.return_value = 1
        self.gdi32.DeleteObject.return_value = 1

    def client_rect(self, hwnd, pointer):
        rect = pointer._obj
        rect.left = rect.top = 0
        rect.right, rect.bottom = self.width, self.height
        return 1


    def select_object(self, dc, bitmap):
        previous, self.selected = self.selected, bitmap
        return previous

    def get_dibits(self, dc, bitmap, first, count, buffer, info, usage):
        if self.selected == bitmap:
            raise AssertionError("GetDIBits called with a selected bitmap")
        header = info._obj.header
        self.bitmap_info = (
            header.biSize,
            header.biWidth,
            header.biHeight,
            header.biBitCount,
            header.biSizeImage,
        )
        row_bytes = header.biWidth * 3
        stride = (row_bytes + 3) & ~3
        data = b"".join(
            bytes((row * row_bytes + column) % 256 for column in range(row_bytes))
            + b"\xff" * (stride - row_bytes)
            for row in range(count)
        )
        ctypes.memmove(buffer, data, len(data))
        return count

    def posted(self):
        """按顺序返回 (hwnd, message, wParam, x, y)。"""
        return [
            (entry.args[0], entry.args[1], entry.args[2], entry.args[3] & 0xFFFF, entry.args[3] >> 16)
            for entry in self.user32.PostMessageW.call_args_list
        ]

    def messages(self):
        return [entry.args[1] for entry in self.user32.PostMessageW.call_args_list]


class ImportAndLayoutTests(unittest.TestCase):
    def test_import_is_standalone_and_does_not_load_windows_dlls(self):
        with patch.object(sys, "platform", "linux"), patch.object(
            ctypes, "WinDLL", create=True
        ) as load_dll:
            module = load_module()
            with self.assertRaises(OSError):
                module._get_win32()
        load_dll.assert_not_called()
        self.assertIs(module.logger, logging.getLogger("maante"))

    def test_fixed_width_windows_structure_layout(self):
        self.assertEqual(ctypes.sizeof(cloud._Rect), 16)
        self.assertEqual(ctypes.sizeof(cloud._BitmapInfoHeader), 40)
        # 点击改为 PostMessage 后不再需要 SendInput 结构体。
        self.assertFalse(hasattr(cloud, "_Input"))

    def test_lazy_api_binds_pointer_sized_handles_and_point_by_value(self):
        module = load_module()
        user32, gdi32 = Mock(), Mock()
        with patch.object(module.sys, "platform", "win32"), patch.object(
            ctypes, "WinDLL", side_effect=[user32, gdi32], create=True
        ) as load_dll:
            api = module._get_win32()
            self.assertIs(module._get_win32(), api)
        self.assertEqual(load_dll.call_count, 2)
        self.assertIs(user32.GetAncestor.restype, ctypes.c_void_p)
        self.assertEqual(
            user32.PostMessageW.argtypes,
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t],
        )
        for name in ("SendInput", "SetCursorPos", "GetForegroundWindow", "WindowFromPoint"):
            self.assertNotIsInstance(getattr(user32, name).argtypes, list)
        self.assertIs(gdi32.CreateCompatibleBitmap.restype, ctypes.c_void_p)
        user32.SetProcessDPIAware.assert_not_called()
        user32.SetProcessDpiAwarenessContext.assert_not_called()


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWin32()
        platform_patch = patch.object(cloud.sys, "platform", "win32")
        api_patch = patch.object(cloud, "_get_win32", return_value=self.api)
        logger_patch = patch.object(cloud, "logger")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        self.load_api = api_patch.start()
        self.addCleanup(api_patch.stop)
        self.log = logger_patch.start()
        self.addCleanup(logger_patch.stop)
        cache_patch = patch.object(cloud, "_capture_failures", cloud.OrderedDict())
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.clock = [0.0]
        clock_patch = patch.object(cloud.time, "monotonic", side_effect=lambda: self.clock[0])
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    @contextmanager
    def recorded_logs(self):
        with patch.object(cloud, "logger", logging.getLogger("maante")), self.assertLogs(
            "maante", level="DEBUG"
        ) as logs:
            yield logs

    def assert_safe_logs(self, logs):
        text = "\n".join(logs.output) + repr([record.args for record in logs.records])
        for secret in ("fake-password", "fake-token", "private-account", "secret-instance", "secret-title", "worker-secret-bytes"):
            self.assertNotIn(secret, text)
        for record in logs.records:
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.stack_info)

    def assert_restored_dpi(self):
        self.assertEqual(
            self.api.user32.SetThreadDpiAwarenessContext.call_args_list,
            [call(cloud._DPI_PER_MONITOR_V2), call(123)],
        )

    def assert_no_input(self):
        self.api.user32.PostMessageW.assert_not_called()

    def test_non_windows_public_apis_fail_without_native_or_process_calls(self):
        with patch.object(cloud.sys, "platform", "linux"), patch.object(
            cloud.subprocess, "run"
        ) as run:
            self.assertIsNone(cloud.capture_window(self.api.HWND))
            self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        run.assert_not_called()
        self.load_api.assert_not_called()

    def test_invalid_handles_rejected_before_loading_native_api_or_spawning(self):
        with patch.object(cloud.subprocess, "run") as run:
            for hwnd in (None, False, True, 0, -1, 1.5, "100", cloud._MAX_HWND + 1):
                with self.subTest(hwnd_type=type(hwnd).__name__):
                    self.assertIsNone(cloud.capture_window(hwnd))
                    self.assertFalse(cloud.click_window(hwnd, (10, 20)))
        run.assert_not_called()
        self.load_api.assert_not_called()

    def test_invalid_timeout_rejected_without_spawning(self):
        with patch.object(cloud.subprocess, "run") as run:
            for timeout in (
                None,
                True,
                False,
                0,
                -1,
                "3",
                float("nan"),
                float("inf"),
                10**1000,
            ):
                with self.subTest(timeout_type=type(timeout).__name__):
                    self.assertIsNone(cloud.capture_window(self.api.HWND, timeout))
        run.assert_not_called()

    def test_capture_decodes_exact_native_bgr_and_uses_isolated_worker(self):
        pixels = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
        packet = cloud._HEADER.pack(3, 2) + pixels.tobytes()
        with patch.object(
            cloud.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=packet),
        ) as run:
            result = cloud.capture_window(self.api.HWND)
        np.testing.assert_array_equal(result, pixels)
        self.assertTrue(result.flags.c_contiguous)
        self.assertTrue(result.flags.writeable)
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                sys.executable,
                "-B",
                "-I",
                str(MODULE_PATH),
                "--capture",
                str(self.api.HWND),
            ],
        )
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["timeout"], 3.0)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("input", kwargs)
        self.load_api.assert_not_called()

    def test_capture_worker_failures_are_redacted(self):
        secret = r"password=fake-password token=fake-token E:\private-account\client.exe --instance secret-instance secret-title"
        failures = (
            subprocess.TimeoutExpired([secret], 0.1, output=secret.encode(), stderr=secret.encode()),
            OSError(secret),
            MemoryError(secret),
        )
        with self.recorded_logs() as logs:
            for failure in failures:
                with self.subTest(error_type=type(failure).__name__), patch.object(
                    cloud.subprocess, "run", side_effect=failure
                ):
                    self.assertIsNone(cloud.capture_window(self.api.HWND, 0.1))
                    self.assertIn("stage=capture.worker", logs.records[-1].getMessage())
                    self.assertIn("error=" + type(failure).__name__, logs.records[-1].getMessage())
            with patch.object(
                cloud.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout=secret.encode(), stderr=secret.encode()),
            ):
                self.assertIsNone(cloud.capture_window(self.api.HWND))
        self.assertEqual(len(logs.records), 4)
        self.assertTrue(all(record.levelno == logging.DEBUG for record in logs.records))
        self.assertIn("reason=worker_capture_failed | returncode=1", logs.output[-1])
        self.assertIn("error=ChildProcessError", logs.output[-1])
        self.assert_safe_logs(logs)

    def capture_process(self, packet=None):
        process = Mock()
        process.returncode = 0
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        process.stdin = io.BytesIO()
        process.communicate.return_value = (
            cloud._HEADER.pack(1, 1) + b"\x01\x02\x03" if packet is None else packet,
            None,
        )
        return process

    def assert_capture_pipes_closed(self, process):
        for pipe in (process.stdin, process.stdout, process.stderr):
            self.assertTrue(pipe.closed)

    def test_capture_stopped_is_keyword_only_and_none_keeps_run_compatibility(self):
        with patch.object(cloud.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout=cloud._HEADER.pack(1, 1) + b"\x00" * 3,
        )) as run, patch.object(cloud.subprocess, "Popen") as spawn:
            self.assertIsNotNone(cloud.capture_window(self.api.HWND, 0.4, stopped=None))
            with self.assertRaises(TypeError):
                cloud.capture_window(self.api.HWND, 0.4, lambda: False)
        run.assert_called_once()
        spawn.assert_not_called()

    def test_capture_cancelled_or_budget_exhausted_before_launch_spawns_nothing(self):
        def use_budget():
            self.clock[0] = 0.25
            return False

        with patch.object(cloud.subprocess, "Popen") as spawn, patch.object(
            cloud.subprocess, "run"
        ) as run:
            for stopped in (lambda: True, use_budget):
                self.clock[0] = 0.0
                self.assertIsNone(cloud.capture_window(self.api.HWND, 0.25, stopped=stopped))
        spawn.assert_not_called()
        run.assert_not_called()

    def test_capture_callback_success_reuses_arguments_and_closes_pipes(self):
        process = self.capture_process()
        with patch.object(cloud.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout=process.communicate.return_value[0],
        )) as run:
            expected = cloud.capture_window(self.api.HWND, 0.4)
        with patch.object(cloud.subprocess, "Popen", return_value=process) as spawn:
            actual = cloud.capture_window(self.api.HWND, 0.4, stopped=lambda: False)
        np.testing.assert_array_equal(actual, expected)
        args, options = run.call_args
        options.pop("timeout")
        spawn.assert_called_once_with(*args, **options)
        process.communicate.assert_called_once_with(timeout=0.1)
        process.kill.assert_not_called()
        self.assert_capture_pipes_closed(process)

    def test_capture_cancellation_during_worker_wait_kills_reaps_without_retry(self):
        process = self.capture_process()

        def wait(*, timeout):
            self.clock[0] += timeout
            raise subprocess.TimeoutExpired(["fake-token"], timeout)

        process.communicate.side_effect = wait
        with patch.object(cloud.subprocess, "Popen", return_value=process) as spawn:
            result = cloud.capture_window(
                self.api.HWND, 3, stopped=lambda: self.clock[0] >= 0.1,
            )
        self.assertIsNone(result)
        spawn.assert_called_once()
        process.communicate.assert_called_once_with(timeout=0.1)
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertGreater(process.wait.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(process.wait.call_args.kwargs["timeout"], 1)
        self.assert_capture_pipes_closed(process)

    def test_capture_wait_slices_include_spawn_cost_in_deadline(self):
        process = self.capture_process()
        remaining_at_wait = []

        def spawn(*args, **kwargs):
            self.clock[0] += 0.03
            return process

        def wait(*, timeout):
            remaining_at_wait.append(0.25 - self.clock[0])
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, min(0.1, remaining_at_wait[-1]))
            self.clock[0] += timeout
            raise subprocess.TimeoutExpired(["fake-token"], timeout)

        process.communicate.side_effect = wait
        with patch.object(cloud.subprocess, "Popen", side_effect=spawn) as launch:
            self.assertIsNone(cloud.capture_window(self.api.HWND, 0.25, stopped=lambda: False))
        launch.assert_called_once()
        self.assertEqual(process.communicate.call_count, 3)
        self.assertAlmostEqual(process.communicate.call_args.kwargs["timeout"], 0.02)
        self.assertAlmostEqual(self.clock[0], 0.25)
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assert_capture_pipes_closed(process)

    def test_capture_expired_during_spawn_never_starts_communicate(self):
        process = self.capture_process()

        def spawn(*args, **kwargs):
            self.clock[0] = 0.25
            return process

        with patch.object(cloud.subprocess, "Popen", side_effect=spawn) as launch:
            self.assertIsNone(cloud.capture_window(self.api.HWND, 0.25, stopped=lambda: False))
        launch.assert_called_once()
        process.communicate.assert_not_called()
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assert_capture_pipes_closed(process)

    def test_capture_discards_frame_if_stopped_or_expired_on_completion(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.clock[0] = 0.0
                process = self.capture_process()
                packet = process.communicate.return_value

                def finish(*, timeout):
                    self.clock[0] = 0.25
                    return packet

                process.communicate.side_effect = finish
                with patch.object(cloud.subprocess, "Popen", return_value=process):
                    result = cloud.capture_window(
                        self.api.HWND, 0.25,
                        stopped=lambda: cancel and self.clock[0] >= 0.25,
                    )
                self.assertIsNone(result)
                process.kill.assert_called_once()
                process.wait.assert_called_once()
                self.assert_capture_pipes_closed(process)

    def test_capture_discards_frame_if_stopped_or_expired_during_decode(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.clock[0] = 0.0
                process = self.capture_process()

                def decode(packet):
                    self.clock[0] = 0.25
                    return np.zeros((1, 1, 3), dtype=np.uint8)

                with patch.object(cloud.subprocess, "Popen", return_value=process), patch.object(
                    cloud, "_decode_frame", side_effect=decode
                ):
                    result = cloud.capture_window(
                        self.api.HWND, 0.25,
                        stopped=lambda: cancel and self.clock[0] >= 0.25,
                    )
                self.assertIsNone(result)
                self.assert_capture_pipes_closed(process)

    def test_capture_reap_timeout_still_closes_pipes_and_never_restarts_worker(self):
        process = self.capture_process()
        process.communicate.side_effect = OSError("fake-token")
        process.wait.side_effect = subprocess.TimeoutExpired(["fake-password"], 1)
        with patch.object(cloud.subprocess, "Popen", return_value=process) as spawn, self.recorded_logs() as logs:
            self.assertIsNone(cloud.capture_window(self.api.HWND, stopped=lambda: False))
        spawn.assert_called_once()
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertGreater(process.wait.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(process.wait.call_args.kwargs["timeout"], 1)
        self.assert_capture_pipes_closed(process)
        self.assert_safe_logs(logs)

    def test_capture_worker_and_cleanup_failures_are_redacted_and_close_all_pipes(self):
        secret = "fake-password fake-token private-account worker-secret-bytes"
        for cleanup_stage in (None, "kill", "wait", "close"):
            with self.subTest(cleanup_stage=cleanup_stage):
                process = self.capture_process()
                process.communicate.side_effect = OSError(secret)
                if cleanup_stage in ("kill", "wait"):
                    getattr(process, cleanup_stage).side_effect = OSError(secret)
                elif cleanup_stage == "close":
                    process.stdin = Mock()
                    process.stdin.close.side_effect = OSError(secret)
                with patch.object(cloud.subprocess, "Popen", return_value=process), self.recorded_logs() as logs:
                    self.assertIsNone(cloud.capture_window(self.api.HWND, stopped=lambda: False))
                process.kill.assert_called_once()
                process.wait.assert_called_once()
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)
                if cleanup_stage == "close":
                    process.stdin.close.assert_called_once()
                else:
                    self.assertTrue(process.stdin.closed)
                self.assert_safe_logs(logs)

    def test_capture_callback_failure_after_spawn_still_reclaims_worker(self):
        process = self.capture_process()
        stopped = Mock(side_effect=[False, OSError("fake-token")])
        with patch.object(cloud.subprocess, "Popen", return_value=process), self.recorded_logs() as logs:
            self.assertIsNone(cloud.capture_window(self.api.HWND, stopped=stopped))
        process.communicate.assert_not_called()
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assert_capture_pipes_closed(process)
        self.assert_safe_logs(logs)

    def test_capture_spawn_failure_and_invalid_worker_payload_are_redacted(self):
        with patch.object(cloud.subprocess, "Popen", side_effect=OSError("fake-token")) as spawn, self.recorded_logs() as logs:
            self.assertIsNone(cloud.capture_window(self.api.HWND, stopped=lambda: False))
        spawn.assert_called_once()
        self.assert_safe_logs(logs)
        for returncode in (0, 1, 2, 99):
            with self.subTest(returncode=returncode):
                process = self.capture_process(b"worker-secret-bytes")
                process.returncode = returncode
                with patch.object(cloud.subprocess, "Popen", return_value=process), self.recorded_logs() as logs:
                    self.assertIsNone(cloud.capture_window(self.api.HWND, stopped=lambda: False))
                self.assert_capture_pipes_closed(process)
                self.assert_safe_logs(logs)

    def test_capture_callback_cancels_real_inert_worker_and_closes_stdout(self):
        # 仅启动等待事件的 Python，绝不执行 --capture 或接触客户端。
        real_popen = subprocess.Popen
        children = []
        checks = []

        def spawn(argv, **kwargs):
            process = real_popen([
                sys.executable, "-B", "-I", "-c",
                "import threading; threading.Event().wait(30)",
            ], **kwargs)
            children.append(process)

            def cleanup():
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
                process.stdout.close()

            self.addCleanup(cleanup)
            return process

        def stopped():
            checks.append(1)
            return len(checks) >= 3

        with patch.object(cloud.subprocess, "Popen", side_effect=spawn):
            self.assertIsNone(cloud.capture_window(self.api.HWND, 3, stopped=stopped))
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_timeout_kills_and_reaps_a_real_inert_subprocess(self):
        # 只启动等待事件的 Python；不运行工作进程，不访问任何真实窗口。
        real_run, real_popen = subprocess.run, subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        def wait_only(argv, **kwargs):
            return real_run(
                [
                    sys.executable,
                    "-B",
                    "-I",
                    "-c",
                    "import threading; threading.Event().wait(30)",
                ],
                **kwargs,
            )

        with patch.object(cloud.subprocess, "Popen", side_effect=spawn), patch.object(
            cloud.subprocess, "run", side_effect=wait_only
        ):
            self.assertIsNone(cloud.capture_window(self.api.HWND, timeout=0.2))
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertEqual(self.log.debug.call_args.args[-1], "TimeoutExpired")

    def test_malformed_or_oversized_frames_rejected_before_numpy_allocation(self):
        packets = (
            None,
            b"",
            b"short",
            cloud._HEADER.pack(0, 1),
            cloud._HEADER.pack(1, 0),
            cloud._HEADER.pack(8193, 1),
            cloud._HEADER.pack(1, 8193),
            cloud._HEADER.pack(8000, 8001),
            cloud._HEADER.pack(8192, 8192),
            cloud._HEADER.pack(0xFFFFFFFF, 1),
            cloud._HEADER.pack(1, 1),
            cloud._HEADER.pack(1, 1) + b"\x00\x00",
            cloud._HEADER.pack(1, 1) + b"\x00\x00\x00extra",
        )
        with patch.object(cloud.np, "frombuffer") as allocate:
            for packet in packets:
                with self.subTest(packet_type=type(packet).__name__), patch.object(
                    cloud.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout=packet),
                ):
                    self.assertIsNone(cloud.capture_window(self.api.HWND))
            with patch.object(cloud, "_MAX_FRAME_BYTES", 10):
                with self.assertRaises(ValueError):
                    cloud._decode_frame(cloud._HEADER.pack(1, 1) + b"\x00" * 3)
        allocate.assert_not_called()
        self.assertEqual(self.log.debug.call_args.args[1], "capture.decode")

    def test_invalid_points_never_load_native_api(self):
        for point in (
            None,
            [10, 20],
            (),
            (10,),
            (1, 2, 3),
            (-1, 2),
            (1, -2),
            (True, 1),
            (1, False),
            (1.0, 2),
            (1, "2"),
            (8192, 0),
            (0, 2**40),
        ):
            with self.subTest(point_type=type(point).__name__):
                self.assertFalse(cloud.click_window(self.api.HWND, point))
        self.load_api.assert_not_called()

    def test_single_click_posts_move_down_up_at_exact_client_coordinate(self):
        self.assertTrue(cloud.click_window(self.api.HWND, (10, 20)))
        self.assertEqual(
            self.api.posted(),
            [
                (self.api.HWND, cloud._WM_MOUSEMOVE, 0, 10, 20),
                (self.api.HWND, cloud._WM_LBUTTONDOWN, cloud._MK_LBUTTON, 10, 20),
                (self.api.HWND, cloud._WM_LBUTTONUP, 0, 10, 20),
            ],
        )
        self.assert_restored_dpi()

    def test_background_click_never_touches_foreground_cursor_or_real_input(self):
        """后台点击不得激活窗口、移动真实鼠标或注入硬件输入，桌面断开时同样可用。"""
        self.assertTrue(cloud.click_window(self.api.HWND, (99, 59)))
        for name in (
            "SendInput",
            "SetCursorPos",
            "GetCursorPos",
            "SetForegroundWindow",
            "GetForegroundWindow",
            "WindowFromPoint",
            "GetAsyncKeyState",
            "ClientToScreen",
        ):
            with self.subTest(api=name):
                getattr(self.api.user32, name).assert_not_called()
        self.assertEqual(self.api.posted()[-1][3:], (99, 59))

    def test_window_state_checks_reject_without_posting(self):
        cases = (
            ("IsWindow", 0),
            ("IsWindowVisible", 0),
            ("IsWindowEnabled", 0),
            ("IsIconic", 1),
            ("GetClientRect", 0),
        )
        for name, value in cases:
            with self.subTest(check=name, value=value):
                api = FakeWin32()
                method = getattr(api.user32, name)
                method.side_effect = None
                method.return_value = value
                with patch.object(cloud, "_get_win32", return_value=api):
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                api.user32.PostMessageW.assert_not_called()
                self.assertEqual(api.user32.SetThreadDpiAwarenessContext.call_count, 2)

    def test_child_window_handle_is_not_a_target_root(self):
        self.assertFalse(cloud.click_window(self.api.CHILD, (10, 20)))
        self.assert_no_input()

    def test_outside_client_is_rejected(self):
        for point in ((100, 20), (10, 60), (100, 60)):
            with self.subTest(point=point):
                self.assertFalse(cloud.click_window(self.api.HWND, point))
        self.assert_no_input()

    def test_move_post_failure_sends_no_button(self):
        self.api.user32.PostMessageW.side_effect = [0]
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.assertEqual(self.api.messages(), [cloud._WM_MOUSEMOVE])
        self.assert_restored_dpi()

    def test_down_failure_or_exception_still_posts_release_in_finally(self):
        for result in (0, OSError("sensitive-input-error")):
            with self.subTest(error_type=type(result).__name__):
                api = FakeWin32()
                api.user32.PostMessageW.side_effect = [1, result, 1]
                with patch.object(cloud, "_get_win32", return_value=api):
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                self.assertEqual(
                    api.messages(),
                    [cloud._WM_MOUSEMOVE, cloud._WM_LBUTTONDOWN, cloud._WM_LBUTTONUP],
                )
                self.assertEqual(api.user32.SetThreadDpiAwarenessContext.call_count, 2)
        self.assertNotIn("sensitive-input-error", str(self.log.mock_calls))

    def test_release_failure_is_reported_without_retry_and_restores_dpi(self):
        for result in (0, OSError("release-error")):
            with self.subTest(error_type=type(result).__name__):
                api = FakeWin32()
                api.user32.PostMessageW.side_effect = [1, 1, result]
                with patch.object(cloud, "_get_win32", return_value=api):
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                self.assertEqual(
                    api.messages(),
                    [cloud._WM_MOUSEMOVE, cloud._WM_LBUTTONDOWN, cloud._WM_LBUTTONUP],
                )
                self.assertEqual(api.user32.SetThreadDpiAwarenessContext.call_count, 2)

    def test_missing_dpi_context_fails_closed(self):
        self.api.user32.SetThreadDpiAwarenessContext.return_value = 0
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.assert_no_input()
        self.api.user32.SetThreadDpiAwarenessContext.assert_called_once()

    def test_dpi_restore_failure_is_reported_after_releasing_input(self):
        self.api.user32.SetThreadDpiAwarenessContext.side_effect = [123, 0]
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.assertEqual(
            self.api.messages(),
            [cloud._WM_MOUSEMOVE, cloud._WM_LBUTTONDOWN, cloud._WM_LBUTTONUP],
        )
        self.assert_restored_dpi()
    def test_capture_deselects_before_getdibits_and_removes_padding(self):
        self.api.width, self.api.height = 3, 2
        image = cloud._capture_native(self.api.HWND)
        np.testing.assert_array_equal(
            image, np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
        )
        self.assertEqual(self.api.bitmap_info, (40, 3, -2, 24, 24))
        self.api.user32.PrintWindow.assert_called_once_with(self.api.HWND, 20, 3)
        self.assertEqual(
            [entry[0] for entry in self.api.gdi32.mock_calls],
            [
                "CreateCompatibleDC",
                "CreateCompatibleBitmap",
                "SelectObject",
                "PatBlt",
                "SelectObject",
                "GetDIBits",
                "DeleteDC",
                "DeleteObject",
            ],
        )
        self.api.user32.ReleaseDC.assert_called_once_with(self.api.HWND, 10)
        self.assert_restored_dpi()

    def test_worker_rejects_dimensions_before_gdi_allocation(self):
        for width, height in ((0, 1), (1, 0), (8193, 1), (1, 8193), (8000, 8001)):
            with self.subTest(width=width, height=height):
                self.api.width, self.api.height = width, height
                with self.assertRaises(ValueError):
                    cloud._capture_native(self.api.HWND)
        self.api.user32.GetDC.assert_not_called()
        self.api.gdi32.CreateCompatibleBitmap.assert_not_called()

    def test_worker_invalid_window_never_acquires_dc(self):
        self.api.user32.IsWindow.return_value = 0
        with self.assertRaises(OSError):
            cloud._capture_native(self.api.HWND)
        self.api.user32.GetDC.assert_not_called()
        self.assert_restored_dpi()

    def test_partial_gdi_acquisition_releases_all_owned_resources(self):
        cases = (
            ("user32", "GetDC", False, False, False),
            ("gdi32", "CreateCompatibleDC", False, False, True),
            ("gdi32", "CreateCompatibleBitmap", True, False, True),
            ("gdi32", "SelectObject", True, True, True),
            ("gdi32", "PatBlt", True, True, True),
            ("user32", "PrintWindow", True, True, True),
            ("gdi32", "GetDIBits", True, True, True),
        )
        for dll, method_name, delete_dc, delete_bitmap, release_dc in cases:
            with self.subTest(failure=method_name):
                api = FakeWin32()
                method = getattr(getattr(api, dll), method_name)
                method.side_effect = None
                method.return_value = 0
                with patch.object(cloud, "_get_win32", return_value=api):
                    with self.assertRaises(OSError):
                        cloud._capture_native(api.HWND)
                self.assertEqual(api.gdi32.DeleteDC.call_count, int(delete_dc))
                self.assertEqual(api.gdi32.DeleteObject.call_count, int(delete_bitmap))
                self.assertEqual(api.user32.ReleaseDC.call_count, int(release_dc))
                self.assertEqual(api.user32.SetThreadDpiAwarenessContext.call_count, 2)

    def test_cleanup_exception_does_not_skip_remaining_gdi_release(self):
        self.api.gdi32.DeleteDC.side_effect = OSError("cleanup-error")
        with self.assertRaises(OSError):
            cloud._capture_native(self.api.HWND)
        self.api.gdi32.DeleteObject.assert_called_once_with(30)
        self.api.user32.ReleaseDC.assert_called_once_with(self.api.HWND, 10)
        self.assert_restored_dpi()

    def test_failed_deselection_does_not_call_getdibits_and_still_cleans_up(self):
        self.api.gdi32.SelectObject.side_effect = [40, 0]
        with self.assertRaises(OSError):
            cloud._capture_native(self.api.HWND)
        self.api.gdi32.GetDIBits.assert_not_called()
        self.api.gdi32.DeleteDC.assert_called_once_with(20)
        self.api.gdi32.DeleteObject.assert_called_once_with(30)
        self.api.user32.ReleaseDC.assert_called_once_with(self.api.HWND, 10)
        self.assert_restored_dpi()

    def test_capture_resize_discards_frame_but_releases_objects(self):
        def resize(*args):
            self.api.width += 1
            return 1

        self.api.user32.PrintWindow.side_effect = resize
        with self.assertRaises(ValueError):
            cloud._capture_native(self.api.HWND)
        self.api.gdi32.GetDIBits.assert_not_called()
        self.assertEqual(self.api.selected, 40)
        self.api.gdi32.DeleteObject.assert_called_once_with(30)
        self.api.user32.ReleaseDC.assert_called_once()
        self.assert_restored_dpi()

    def test_worker_stdout_contains_only_dimensions_and_unpadded_pixels(self):
        self.api.width, self.api.height = 3, 2
        stdout, stderr = SimpleNamespace(buffer=io.BytesIO()), io.StringIO()
        with patch.object(cloud.sys, "stdout", stdout), patch.object(
            cloud.sys, "stderr", stderr
        ):
            result = cloud._worker_main(["--capture", str(self.api.HWND)])
        self.assertEqual(result, 0)
        self.assertEqual(
            stdout.buffer.getvalue(), cloud._HEADER.pack(3, 2) + bytes(range(18))
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.log.mock_calls, [])

    def test_worker_errors_have_no_stdout_stderr_or_logger_leak(self):
        stdout, stderr = SimpleNamespace(buffer=io.BytesIO()), io.StringIO()
        with patch.object(cloud.sys, "stdout", stdout), patch.object(
            cloud.sys, "stderr", stderr
        ), patch.object(cloud, "_capture_native", side_effect=RuntimeError("secret")):
            result = cloud._worker_main(["--capture", str(self.api.HWND)])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.buffer.getvalue(), b"")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.log.mock_calls, [])

    def test_worker_rejects_bad_arguments_without_loading_native_api(self):
        for argv in (
            [],
            ["--capture"],
            ["--other", "100"],
            ["--capture", "0"],
            ["--capture", "secret"],
        ):
            self.assertNotEqual(cloud._worker_main(argv), 0)
        with patch.object(cloud.sys, "platform", "linux"):
            self.assertNotEqual(cloud._worker_main(["--capture", "100"]), 0)
        self.load_api.assert_not_called()
        self.assertEqual(self.log.mock_calls, [])

    def test_repeated_capture_failures_are_deduplicated_until_recovery(self):
        packet = cloud._HEADER.pack(3, 2) + bytes(18)
        results = [SimpleNamespace(returncode=1, stdout=b"worker-secret-bytes")] * 100
        results += [SimpleNamespace(returncode=0, stdout=packet)] * 50
        results += [SimpleNamespace(returncode=1, stdout=b"")]
        with self.recorded_logs() as logs, patch.object(cloud.subprocess, "run", side_effect=results):
            for index in range(151):
                self.clock[0] = index * 0.1
                cloud.capture_window(87654321)
        self.assertEqual(len(logs.records), 3)
        self.assertEqual(logs.records[0].levelno, logging.DEBUG)
        self.assertIn("reason=worker_capture_failed | returncode=1 | timeout_s=3.0 | failures=1", logs.output[0])
        self.assertEqual(logs.records[1].levelno, logging.DEBUG)
        self.assertIn("stage=capture.ready | failures=100 | client=3x2 | outage=10.000s", logs.output[1])
        self.assertIn("failures=1", logs.output[2])
        self.assert_safe_logs(logs)
        self.assertNotIn("87654321", "\n".join(logs.output))

    def test_capture_diagnostic_changes_are_logged_once_each(self):
        results = (
            [SimpleNamespace(returncode=1, stdout=b"")] * 3
            + [SimpleNamespace(returncode=2, stdout=b"")] * 3
            + [SimpleNamespace(returncode=0, stdout=b"worker-secret-bytes")] * 3
        )
        with self.recorded_logs() as logs, patch.object(cloud.subprocess, "run", side_effect=results):
            for _ in results:
                self.assertIsNone(cloud.capture_window(self.api.HWND))
        self.assertEqual(len(logs.records), 3)
        self.assertIn("reason=worker_capture_failed | returncode=1", logs.output[0])
        self.assertIn("reason=worker_arguments_rejected | returncode=2 | timeout_s=3.0 | failures=4", logs.output[1])
        self.assertIn("stage=capture.decode | reason=dimensions_out_of_bounds | returncode=0", logs.output[2])
        self.assert_safe_logs(logs)

    def test_capture_failure_state_is_bounded_and_successes_are_silent(self):
        packet = cloud._HEADER.pack(1, 1) + bytes(3)
        with patch.object(
            cloud.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=packet)
        ):
            for _ in range(20):
                self.assertIsNotNone(cloud.capture_window(self.api.HWND))
        self.assertEqual(self.log.mock_calls, [])
        with patch.object(
            cloud.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=b"")
        ):
            for hwnd in range(1, 40):
                self.assertIsNone(cloud.capture_window(hwnd))
        self.assertEqual(len(cloud._capture_failures), cloud._CAPTURE_LOG_LIMIT)
        self.assertNotIn(1, cloud._capture_failures)
        self.assertIn(39, cloud._capture_failures)
        self.assertEqual(self.log.debug.call_count, 39)

    def test_click_success_and_failure_logs_have_reason_without_coordinates(self):
        with self.recorded_logs() as logs:
            self.assertTrue(cloud.click_window(self.api.HWND, (37, 41)))
        self.assertEqual([record.levelno for record in logs.records], [logging.DEBUG, logging.DEBUG])
        self.assertIn("stage=click.ready", logs.output[-1])
        cases = (
            ("IsWindowVisible", 0, "click.validate", "window_hidden"),
            ("IsIconic", 1, "click.validate", "window_minimized"),
            ("IsWindowEnabled", 0, "click.validate", "window_disabled"),
        )
        for name, value, stage, reason in cases:
            with self.subTest(check=name):
                api = FakeWin32()
                method = getattr(api.user32, name)
                method.side_effect = None
                method.return_value = value
                with patch.object(cloud, "_get_win32", return_value=api), self.recorded_logs() as logs:
                    self.assertFalse(cloud.click_window(api.HWND, (37, 41)))
                self.assertEqual(logs.records[-1].levelno, logging.DEBUG)
                self.assertIn("stage=%s | reason=%s" % (stage, reason), logs.output[-1])
                text = "\n".join(logs.output) + repr([record.args for record in logs.records])
                for sensitive in ("37", "41"):
                    self.assertNotIn(sensitive, text)
                api.user32.PostMessageW.assert_not_called()

    def test_click_post_failures_are_specific_and_redacted(self):
        for sequence, reason in (
            ([0], "move_post_failed"),
            ([1, 0, 1], "button_down_failed"),
            ([1, 1, 0], "button_release_failed"),
            ([1, OSError("fake-password fake-token private-account"), 1], "button_input_failed"),
        ):
            with self.subTest(reason=reason):
                api = FakeWin32()
                api.user32.PostMessageW.side_effect = sequence
                with patch.object(cloud, "_get_win32", return_value=api), self.recorded_logs() as logs:
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                self.assertIn("stage=click.input | reason=%s" % reason, logs.output[-1])
                self.assert_safe_logs(logs)
    def test_worker_revalidates_image_dimensions_before_writing(self):
        stdout = SimpleNamespace(buffer=io.BytesIO())
        invalid_image = SimpleNamespace(shape=(8001, 8000, 3), dtype=np.uint8)
        with patch.object(
            cloud, "_capture_native", return_value=invalid_image
        ), patch.object(cloud.sys, "stdout", stdout):
            self.assertEqual(cloud._worker_main(["--capture", "100"]), 1)
        self.assertEqual(stdout.buffer.getvalue(), b"")


if __name__ == "__main__":
    unittest.main()
