"""窗口辅助回归：Win32 和输入全部 mock，不连接或操作真实客户端。"""

import ctypes
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
        self.origin = (100, 200)
        self.cursor = (0, 0)
        self.desktop = {76: 0, 77: 0, 78: 1920, 79: 1080}
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
        self.user32.GetForegroundWindow.return_value = self.HWND
        self.user32.GetClientRect.side_effect = self.client_rect
        self.user32.ClientToScreen.side_effect = self.client_to_screen
        self.user32.WindowFromPoint.return_value = self.CHILD
        self.user32.GetSystemMetrics.side_effect = self.desktop.__getitem__
        self.user32.SetCursorPos.side_effect = self.set_cursor
        self.user32.GetCursorPos.side_effect = self.get_cursor
        self.user32.GetAsyncKeyState.return_value = 0
        self.user32.SendInput.return_value = 1
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

    def client_to_screen(self, hwnd, pointer):
        pointer._obj.x += self.origin[0]
        pointer._obj.y += self.origin[1]
        return 1

    def set_cursor(self, x, y):
        self.cursor = (x, y)
        return 1

    def get_cursor(self, pointer):
        pointer._obj.x, pointer._obj.y = self.cursor
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

    def button_flags(self):
        return [
            entry.args[1]._obj.mi.dwFlags
            for entry in self.user32.SendInput.call_args_list
        ]


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
        self.assertEqual(ctypes.sizeof(cloud._Point), 8)
        self.assertEqual(ctypes.sizeof(cloud._Rect), 16)
        self.assertEqual(ctypes.sizeof(cloud._BitmapInfoHeader), 40)
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(cloud._Input), expected)

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
        self.assertEqual(user32.WindowFromPoint.argtypes, [module._Point])
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

    def assert_restored_dpi(self):
        self.assertEqual(
            self.api.user32.SetThreadDpiAwarenessContext.call_args_list,
            [call(cloud._DPI_PER_MONITOR_V2), call(123)],
        )

    def assert_no_input(self):
        self.api.user32.SendInput.assert_not_called()

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
        secret = "do-not-log-command-or-screenshot"
        failures = (
            subprocess.TimeoutExpired([secret], 0.1, output=secret.encode()),
            OSError(secret),
            MemoryError(secret),
        )
        for failure in failures:
            with self.subTest(error_type=type(failure).__name__), patch.object(
                cloud.subprocess, "run", side_effect=failure
            ):
                self.assertIsNone(cloud.capture_window(self.api.HWND, 0.1))
                self.log.debug.assert_called_with(
                    "窗口辅助失败 | stage=%s | error=%s",
                    "capture.worker",
                    type(failure).__name__,
                )
        with patch.object(
            cloud.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout=secret.encode()),
        ):
            self.assertIsNone(cloud.capture_window(self.api.HWND))
        self.assertNotIn(secret, str(self.log.mock_calls))
        self.assertEqual(self.log.debug.call_args.args[-1], "ChildProcessError")

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
        self.assertEqual(self.log.debug.call_args.args[-2], "capture.decode")

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

    def test_single_click_uses_exact_client_coordinate_and_releases_once(self):
        self.assertTrue(cloud.click_window(self.api.HWND, (10, 20)))
        self.api.user32.SetCursorPos.assert_called_once_with(110, 220)
        self.assertEqual(self.api.button_flags(), [cloud._LEFT_DOWN, cloud._LEFT_UP])
        for entry in self.api.user32.SendInput.call_args_list:
            self.assertEqual(entry.args[0], 1)
            self.assertEqual(entry.args[1]._obj.type, 0)
            self.assertEqual(entry.args[2], ctypes.sizeof(cloud._Input))
        self.assertEqual(self.api.user32.WindowFromPoint.call_count, 2)
        self.assert_restored_dpi()

    def test_target_descendant_foreground_and_negative_monitor_coordinates(self):
        self.api.user32.GetForegroundWindow.return_value = self.api.CHILD
        self.api.origin = (-1000, -500)
        self.api.desktop.update({76: -1920, 77: -1080, 78: 3840, 79: 2160})
        self.assertTrue(cloud.click_window(self.api.HWND, (10, 20)))
        self.api.user32.SetCursorPos.assert_called_once_with(-990, -480)

    def test_window_focus_hit_and_user_button_checks_reject_without_input(self):
        cases = (
            ("IsWindow", 0),
            ("IsWindowVisible", 0),
            ("IsWindowEnabled", 0),
            ("IsIconic", 1),
            ("GetForegroundWindow", 0),
            ("GetForegroundWindow", 900),
            ("WindowFromPoint", 0),
            ("WindowFromPoint", 900),
            ("GetAsyncKeyState", 0x8000),
            ("GetClientRect", 0),
            ("ClientToScreen", 0),
        )
        for name, value in cases:
            with self.subTest(check=name, value=value):
                api = FakeWin32()
                method = getattr(api.user32, name)
                method.side_effect = None
                method.return_value = value
                with patch.object(cloud, "_get_win32", return_value=api):
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                api.user32.SendInput.assert_not_called()
                api.user32.SetCursorPos.assert_not_called()
                self.assertEqual(api.user32.SetThreadDpiAwarenessContext.call_count, 2)

    def test_child_window_handle_is_not_a_target_root(self):
        self.assertFalse(cloud.click_window(self.api.CHILD, (10, 20)))
        self.api.user32.SetCursorPos.assert_not_called()
        self.assert_no_input()

    def test_outside_client_or_virtual_desktop_rejected(self):
        for point in ((100, 20), (10, 60)):
            self.assertFalse(cloud.click_window(self.api.HWND, point))
        self.api.origin = (1920, 0)
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.api.origin = (100, 200)
        self.api.desktop[78] = 0
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.api.user32.SetCursorPos.assert_not_called()
        self.assert_no_input()

    def test_failed_cursor_move_does_not_send_buttons(self):
        self.api.user32.SetCursorPos.side_effect = None
        self.api.user32.SetCursorPos.return_value = 0
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.assert_no_input()
        self.assert_restored_dpi()

    def test_clipped_or_unreadable_cursor_does_not_send_buttons(self):
        self.api.user32.SetCursorPos.side_effect = lambda x, y: 1
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.assert_no_input()
        self.api.user32.GetCursorPos.side_effect = None
        self.api.user32.GetCursorPos.return_value = 0
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.assert_no_input()

    def test_focus_or_occlusion_change_after_move_does_not_send_buttons(self):
        for name, values in (
            ("GetForegroundWindow", [self.api.HWND, 900]),
            ("WindowFromPoint", [self.api.CHILD, 900]),
            ("GetAsyncKeyState", [0, 0x8000]),
        ):
            with self.subTest(check=name):
                api = FakeWin32()
                getattr(api.user32, name).side_effect = values
                with patch.object(cloud, "_get_win32", return_value=api):
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                api.user32.SetCursorPos.assert_called_once()
                api.user32.SendInput.assert_not_called()

    def test_window_moving_or_resizing_during_cursor_move_rejects_input(self):
        for change in ("move", "resize"):
            with self.subTest(change=change):
                api = FakeWin32()

                def moved(x, y):
                    api.cursor = (x, y)
                    if change == "move":
                        api.origin = (101, 200)
                    else:
                        api.width += 1
                    return 1

                api.user32.SetCursorPos.side_effect = moved
                with patch.object(cloud, "_get_win32", return_value=api):
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                api.user32.SendInput.assert_not_called()

    def test_down_failure_or_exception_still_releases_in_finally(self):
        for result in (0, OSError("sensitive-input-error")):
            with self.subTest(error_type=type(result).__name__):
                api = FakeWin32()
                api.user32.SendInput.side_effect = [result, 1]
                with patch.object(cloud, "_get_win32", return_value=api):
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                self.assertEqual(api.button_flags(), [cloud._LEFT_DOWN, cloud._LEFT_UP])
                self.assertEqual(api.user32.SetThreadDpiAwarenessContext.call_count, 2)
        self.assertNotIn("sensitive-input-error", str(self.log.mock_calls))

    def test_release_failure_is_reported_without_retry_and_restores_dpi(self):
        for result in (0, OSError("release-error")):
            with self.subTest(error_type=type(result).__name__):
                api = FakeWin32()
                api.user32.SendInput.side_effect = [1, result]
                with patch.object(cloud, "_get_win32", return_value=api):
                    self.assertFalse(cloud.click_window(api.HWND, (10, 20)))
                self.assertEqual(api.button_flags(), [cloud._LEFT_DOWN, cloud._LEFT_UP])
                self.assertEqual(api.user32.SetThreadDpiAwarenessContext.call_count, 2)

    def test_missing_dpi_context_fails_closed(self):
        self.api.user32.SetThreadDpiAwarenessContext.return_value = 0
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.api.user32.SetCursorPos.assert_not_called()
        self.assert_no_input()
        self.api.user32.SetThreadDpiAwarenessContext.assert_called_once()

    def test_dpi_restore_failure_is_reported_after_releasing_input(self):
        self.api.user32.SetThreadDpiAwarenessContext.side_effect = [123, 0]
        self.assertFalse(cloud.click_window(self.api.HWND, (10, 20)))
        self.assertEqual(self.api.button_flags(), [cloud._LEFT_DOWN, cloud._LEFT_UP])
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
        self.log.debug.assert_not_called()

    def test_worker_errors_have_no_stdout_stderr_or_logger_leak(self):
        stdout, stderr = SimpleNamespace(buffer=io.BytesIO()), io.StringIO()
        with patch.object(cloud.sys, "stdout", stdout), patch.object(
            cloud.sys, "stderr", stderr
        ), patch.object(cloud, "_capture_native", side_effect=RuntimeError("secret")):
            result = cloud._worker_main(["--capture", str(self.api.HWND)])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.buffer.getvalue(), b"")
        self.assertEqual(stderr.getvalue(), "")
        self.log.debug.assert_not_called()

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
        self.log.debug.assert_not_called()

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
