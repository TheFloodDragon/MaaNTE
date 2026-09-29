"""原生客户区截图与后台单击；阻塞的 PrintWindow 仅在独立进程执行，点击用 PostMessage 投递。"""

from __future__ import annotations

import ctypes
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from functools import lru_cache
import logging
import math
from pathlib import Path
import struct
import subprocess
import sys
from threading import Lock
import time

import numpy as np

# 与 utils.logger 的实例相同，但不触发 utils/__init__.py 或工作进程的日志初始化。
logger = logging.getLogger("maante")

__all__ = ["capture_window", "click_window"]

_MAX_SIDE = 8192
_MAX_PIXELS = 64_000_000
_HEADER = struct.Struct("<II")
_MAX_FRAME_BYTES = _HEADER.size + _MAX_PIXELS * 3
_MAX_HWND = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
_GA_ROOT = 2
_DPI_PER_MONITOR_V2 = -4
_PW_CLIENTONLY = 0x1
_PW_RENDERFULLCONTENT = 0x2
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_MK_LBUTTON = 0x0001
_CAPTURE_LOG_LIMIT = 16
_CAPTURE_WAIT_SLICE = 0.1
_CAPTURE_REAP_TIMEOUT = 1.0
_capture_failures = OrderedDict()
_capture_log_lock = Lock()
_CHECK_REASONS = frozenset({
    "invalid_timeout", "frame_payload_invalid", "frame_length_mismatch",
    "dimensions_out_of_bounds", "client_origin_invalid", "dpi_context_unavailable",
    "dpi_restore_failed", "window_invalid", "window_hidden", "window_minimized",
    "client_bounds_query_failed", "window_disabled", "target_not_root",
    "target_outside_client", "move_post_failed", "button_down_failed",
    "button_release_failed", "native_call_failed",
})


class _Rect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("left", "top", "right", "bottom")]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapInfoHeader), ("colors", ctypes.c_uint32 * 1)]



class _Win32:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        handle = ctypes.c_void_p
        integer = ctypes.c_int32
        uint = ctypes.c_uint32
        pointer = ctypes.POINTER
        for name, args, result in (
            ("IsWindow", [handle], integer),
            ("IsWindowVisible", [handle], integer),
            ("IsWindowEnabled", [handle], integer),
            ("IsIconic", [handle], integer),
            ("GetAncestor", [handle, uint], handle),
            ("GetClientRect", [handle, pointer(_Rect)], integer),
            ("PostMessageW", [handle, uint, ctypes.c_size_t, ctypes.c_ssize_t], integer),
            ("SetThreadDpiAwarenessContext", [handle], handle),
            ("GetDC", [handle], handle),
            ("ReleaseDC", [handle, handle], integer),
            ("PrintWindow", [handle, handle, uint], integer),
        ):
            function = getattr(self.user32, name)
            function.argtypes, function.restype = args, result
        for name, args, result in (
            ("CreateCompatibleDC", [handle], handle),
            ("CreateCompatibleBitmap", [handle, integer, integer], handle),
            ("SelectObject", [handle, handle], handle),
            ("PatBlt", [handle, integer, integer, integer, integer, uint], integer),
            (
                "GetDIBits",
                [handle, handle, uint, uint, handle, pointer(_BitmapInfo), uint],
                integer,
            ),
            ("DeleteDC", [handle], integer),
            ("DeleteObject", [handle], integer),
        ):
            function = getattr(self.gdi32, name)
            function.argtypes, function.restype = args, result


@lru_cache(maxsize=1)
def _get_win32():
    if sys.platform != "win32":
        raise OSError
    return _Win32()


def _safe_failure(exc, fallback):
    # 只读取预定义检查码，不调用 str/repr，也不回显外部异常类型名。
    reason = fallback
    if len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in _CHECK_REASONS:
        reason = exc.args[0]
    elif isinstance(exc, subprocess.TimeoutExpired):
        reason = "worker_timeout"
    elif isinstance(exc, MemoryError):
        reason = "memory_unavailable"
    elif isinstance(exc, PermissionError):
        reason = "permission_denied"
    for error_class in (
        subprocess.TimeoutExpired, ChildProcessError, MemoryError, OverflowError,
        ValueError, PermissionError, FileNotFoundError, OSError, Exception,
    ):
        if isinstance(exc, error_class):
            return reason, error_class.__name__
    return reason, "Exception"


def _capture_failed(hwnd, stage, reason, error_type, started, returncode=None, timeout=None):
    # 仅在父进程调用：按窗口保存至多 16 条纯诊断状态，不保存截图或异常对象。
    # 耗时不参与签名；连续相同故障静默，诊断变化和恢复各记录一次。
    key = hwnd if type(hwnd) is int and _valid_hwnd(hwnd) else None
    returncode = returncode if type(returncode) is int else None
    signature = (stage, reason, error_type, returncode, timeout)
    with _capture_log_lock:
        previous = _capture_failures.pop(key, None)
        count = previous[1] + 1 if previous is not None else 1
        first_failure = previous[2] if previous is not None else started
        _capture_failures[key] = (signature, count, first_failure)
        if len(_capture_failures) > _CAPTURE_LOG_LIMIT:
            _capture_failures.popitem(last=False)
    if previous is None or previous[0] != signature:
        logger.debug(
            "云窗口截图失败 | stage=%s | reason=%s | returncode=%s | timeout_s=%s | failures=%d | elapsed=%.3fs | error=%s",
            stage, reason, returncode if returncode is not None else "unknown",
            timeout if timeout is not None else "unknown", count,
            time.monotonic() - started, error_type,
        )


def _capture_recovered(hwnd, image, started):
    key = hwnd if type(hwnd) is int and _valid_hwnd(hwnd) else None
    with _capture_log_lock:
        previous = _capture_failures.pop(key, None)
    if previous is not None:
        now = time.monotonic()
        logger.debug(
            "云窗口截图已恢复 | stage=capture.ready | failures=%d | client=%dx%d | outage=%.3fs | elapsed=%.3fs",
            previous[1], image.shape[1], image.shape[0], now - previous[2], now - started,
        )


def _click_failed(stage, reason, error_type, started):
    logger.debug(
        "云窗口点击未完成 | stage=%s | reason=%s | elapsed=%.3fs | error=%s",
        stage, reason, time.monotonic() - started, error_type,
    )


def _valid_hwnd(hwnd) -> bool:
    return (
        isinstance(hwnd, int)
        and not isinstance(hwnd, bool)
        and 0 < hwnd <= _MAX_HWND
    )


def _valid_point(point) -> bool:
    return (
        isinstance(point, tuple)
        and len(point) == 2
        and all(
            isinstance(value, int) and not isinstance(value, bool) for value in point
        )
        and all(0 <= value < _MAX_SIDE for value in point)
    )


def _require(value, reason="native_call_failed") -> None:
    if not value:
        raise OSError(reason)


def _check_size(width: int, height: int) -> None:
    if not (
        0 < width <= _MAX_SIDE
        and 0 < height <= _MAX_SIDE
        and width * height <= _MAX_PIXELS
    ):
        raise ValueError("dimensions_out_of_bounds")


@contextmanager
def _thread_dpi(api):
    previous = api.user32.SetThreadDpiAwarenessContext(_DPI_PER_MONITOR_V2)
    _require(previous, "dpi_context_unavailable")
    try:
        yield
    finally:
        _require(api.user32.SetThreadDpiAwarenessContext(previous), "dpi_restore_failed")


def _check_window(api, hwnd) -> None:
    _require(api.user32.IsWindow(hwnd), "window_invalid")
    _require(api.user32.IsWindowVisible(hwnd), "window_hidden")
    _require(not api.user32.IsIconic(hwnd), "window_minimized")


def _client_size(api, hwnd) -> tuple[int, int]:
    rect = _Rect()
    _require(api.user32.GetClientRect(hwnd, ctypes.byref(rect)), "client_bounds_query_failed")
    if rect.left != 0 or rect.top != 0:
        raise ValueError("client_origin_invalid")
    width, height = rect.right, rect.bottom
    _check_size(width, height)
    return width, height


def _decode_frame(payload: bytes) -> np.ndarray:
    if (
        not isinstance(payload, bytes)
        or not _HEADER.size <= len(payload) <= _MAX_FRAME_BYTES
    ):
        raise ValueError("frame_payload_invalid")
    width, height = _HEADER.unpack_from(payload)
    _check_size(width, height)
    if len(payload) != _HEADER.size + width * height * 3:
        raise ValueError("frame_length_mismatch")
    return (
        np.frombuffer(payload, dtype=np.uint8, offset=_HEADER.size)
        .reshape(height, width, 3)
        .copy()
    )


def _cleanup_capture_worker(process, completed):
    """只回收本次截图进程；清理失败不得覆盖取帧/停止原因。"""
    if not completed:
        # 截止后的这段时间只用于回收，不能继续取帧或启动另一个 worker。
        # 即使 kill 失败也尝试 wait，避免已退出进程未被回收。
        for stage, operation in (
            ("capture.cleanup.kill", process.kill),
            ("capture.cleanup.wait", lambda: process.wait(timeout=_CAPTURE_REAP_TIMEOUT)),
        ):
            try:
                operation()
            except BaseException as exc:
                reason, error_type = _safe_failure(exc, "worker_cleanup_failed")
                logger.debug(
                    "截图工作进程清理未完成 | stage=%s | reason=%s | error=%s",
                    stage, reason, error_type,
                )
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except BaseException as exc:
                reason, error_type = _safe_failure(exc, "worker_pipe_close_failed")
                logger.debug(
                    "截图工作进程管道关闭未完成 | stage=capture.cleanup.close | reason=%s | error=%s",
                    reason, error_type,
                )


def _capture_worker(command, options, timeout, deadline, stopped):
    """同一 worker 的有界等待；切片超时只继续等它，不重启截图。"""
    if stopped():
        return None
    if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired(command, timeout)
    process = subprocess.Popen(command, **options)
    completed = False
    try:
        while True:
            if stopped():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, _ = process.communicate(timeout=min(_CAPTURE_WAIT_SLICE, remaining))
            except subprocess.TimeoutExpired:
                continue
            if stopped():
                return None
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            # communicate 成功返回已完成 wait；无需再 kill 一个已经退出的进程。
            completed = True
            return subprocess.CompletedProcess(command, process.returncode, stdout)
    finally:
        _cleanup_capture_worker(process, completed)


def capture_window(
    hwnd, timeout: float = 3.0, *, stopped: Callable[[], bool] | None = None,
) -> np.ndarray | None:
    """返回原生 BGR 图像，失败/超时/停止返回 None，不缩放、不改进程 DPI。

    stopped 存在时，创建和等待单个 worker 共用 timeout 截止时间，等待阶段
    以不超过 0.1 秒的切片检查停止；超时/取消后最多额外等待 1 秒回收，不重试截图。
    调用方在 None 返回后复查停止状态，决定是否转为业务停止。
    """
    started = time.monotonic()
    if sys.platform != "win32":
        _capture_failed(hwnd, "capture.platform", "unsupported_platform", "UnsupportedPlatform", started)
        return None
    if not _valid_hwnd(hwnd):
        _capture_failed(hwnd, "capture.arguments", "invalid_handle", "ValueError", started)
        return None
    stage = "capture.arguments"
    reason = "invalid_timeout"
    returncode = validated_timeout = None
    try:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("invalid_timeout")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid_timeout")
        validated_timeout = timeout
        stage = "capture.worker"
        reason = "worker_execution_failed"
        command = [
            sys.executable,
            "-B",
            "-I",
            str(Path(__file__).resolve()),
            "--capture",
            str(hwnd),
        ]
        options = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if stopped is None:
            # 保留既有调用；run 在超时时 kill + communicate/wait 并关闭管道。
            result = subprocess.run(command, timeout=timeout, **options)
        else:
            result = _capture_worker(command, options, timeout, started + timeout, stopped)
            if result is None:
                return None
        returncode = result.returncode
        if returncode != 0:
            reason = (
                "worker_capture_failed" if returncode == 1 else
                "worker_arguments_rejected" if returncode == 2 else "worker_exit_nonzero"
            )
            raise ChildProcessError
        stage = "capture.decode"
        reason = "frame_decode_failed"
        image = _decode_frame(result.stdout)
        if stopped is not None:
            if stopped():
                return None
            if time.monotonic() >= started + timeout:
                raise subprocess.TimeoutExpired(command, timeout)
        _capture_recovered(hwnd, image, started)
        return image
    except Exception as exc:
        reason, error_type = _safe_failure(exc, reason)
        _capture_failed(hwnd, stage, reason, error_type, started, returncode, validated_timeout)
        return None


def _capture_native(hwnd) -> np.ndarray:
    """只供工作进程使用。24 位 top-down DIB 已是 BGR，行填充不写入协议。"""
    api = _get_win32()
    with _thread_dpi(api):
        _check_window(api, hwnd)
        width, height = _client_size(api, hwnd)
        window_dc = api.user32.GetDC(hwnd)
        _require(window_dc)
        memory_dc = bitmap = 0
        try:
            memory_dc = api.gdi32.CreateCompatibleDC(window_dc)
            _require(memory_dc)
            bitmap = api.gdi32.CreateCompatibleBitmap(window_dc, width, height)
            _require(bitmap)
            previous_bitmap = api.gdi32.SelectObject(memory_dc, bitmap)
            _require(previous_bitmap and previous_bitmap != _MAX_HWND)
            try:
                # 防止窗口没有绘制的区域含有未初始化的位图内容。
                _require(api.gdi32.PatBlt(memory_dc, 0, 0, width, height, 0x00000042))
                _require(
                    api.user32.PrintWindow(
                        hwnd, memory_dc, _PW_CLIENTONLY | _PW_RENDERFULLCONTENT
                    )
                )
                _check_window(api, hwnd)
                if _client_size(api, hwnd) != (width, height):
                    raise ValueError
            finally:
                restored = api.gdi32.SelectObject(memory_dc, previous_bitmap)
                _require(restored and restored != _MAX_HWND)

            # GetDIBits 要求 bitmap 不再被任何 DC 选中。
            stride = (width * 3 + 3) & ~3
            pixels = np.zeros((height, stride), dtype=np.uint8)
            info = _BitmapInfo()
            info.header.biSize = ctypes.sizeof(_BitmapInfoHeader)
            info.header.biWidth = width
            info.header.biHeight = -height
            info.header.biPlanes = 1
            info.header.biBitCount = 24
            info.header.biSizeImage = stride * height
            lines = api.gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                pixels.ctypes.data_as(ctypes.c_void_p),
                ctypes.byref(info),
                0,
            )
            _require(lines == height)
            return pixels[:, : width * 3].reshape(height, width, 3)
        finally:
            # 先销毁 DC：即使取消选择失败，也先解除其对 bitmap 的占用。
            try:
                if memory_dc:
                    _require(api.gdi32.DeleteDC(memory_dc))
            finally:
                try:
                    if bitmap:
                        _require(api.gdi32.DeleteObject(bitmap))
                finally:
                    _require(api.user32.ReleaseDC(hwnd, window_dc))


def _click_target(api, hwnd, point) -> tuple[int, int]:
    _check_window(api, hwnd)
    _require(api.user32.IsWindowEnabled(hwnd), "window_disabled")
    _require(api.user32.GetAncestor(hwnd, _GA_ROOT) == hwnd, "target_not_root")
    width, height = _client_size(api, hwnd)
    if point[0] >= width or point[1] >= height:
        raise ValueError("target_outside_client")
    return width, height


def _post_mouse(api, hwnd, message: int, wparam: int, point) -> bool:
    # lParam 低 16 位为 x、高 16 位为 y，坐标已限定在客户区内且小于 _MAX_SIDE。
    lparam = (point[1] << 16) | point[0]
    return bool(api.user32.PostMessageW(hwnd, message, wparam, lparam))


def click_window(hwnd, point: tuple[int, int]) -> bool:
    """按原生客户区坐标向窗口投递一次左键单击（PostMessage）。

    不激活窗口、不移动真实鼠标，因此桌面断开或锁屏时同样可用，也不打扰用户操作。
    实机取证（2026-09-26，会话断开）：投递到 Qt 主窗口的 WM_LBUTTON* 被客户端接收并执行。
    窗口不可见、最小化、禁用、不是顶层窗口或坐标超出客户区时拒绝投递，不盲发。
    """
    started = time.monotonic()
    if sys.platform != "win32":
        _click_failed("click.platform", "unsupported_platform", "UnsupportedPlatform", started)
        return False
    if not _valid_hwnd(hwnd) or not _valid_point(point):
        _click_failed("click.arguments", "invalid_handle_or_point", "ValueError", started)
        return False
    logger.debug("开始云窗口后台点击 | stage=click.begin")
    stage = "click.api"
    reason = "native_api_unavailable"
    try:
        api = _get_win32()
        stage = "click.dpi"
        reason = "dpi_context_failed"
        with _thread_dpi(api):
            stage = "click.validate"
            reason = "target_validation_failed"
            _click_target(api, hwnd, point)
            stage = "click.input"
            reason = "button_input_failed"
            # 先投递一次移动，让客户端更新悬停位置，再发按下/抬起。
            _require(_post_mouse(api, hwnd, _WM_MOUSEMOVE, 0, point), "move_post_failed")
            try:
                pressed = _post_mouse(api, hwnd, _WM_LBUTTONDOWN, _MK_LBUTTON, point)
            finally:
                # 按下即使异常也可能已进入消息队列，必须在 finally 尝试抬起一次。
                released = _post_mouse(api, hwnd, _WM_LBUTTONUP, 0, point)
            _require(pressed, "button_down_failed")
            _require(released, "button_release_failed")
        logger.debug("云窗口后台点击完成 | stage=click.ready | elapsed=%.3fs", time.monotonic() - started)
        return True
    except Exception as exc:
        reason, error_type = _safe_failure(exc, reason)
        _click_failed(stage, reason, error_type, started)
        return False


def _worker_main(argv: list[str]) -> int:
    # 工作进程不初始化项目 logger，stdout 仅含尺寸和 BGR 字节；异常不回显。
    try:
        if sys.platform != "win32" or len(argv) != 2 or argv[0] != "--capture":
            return 2
        hwnd = int(argv[1], 10)
        if not _valid_hwnd(hwnd):
            return 2
        image = _capture_native(hwnd)
        height, width, channels = image.shape
        _check_size(width, height)
        if channels != 3 or image.dtype != np.uint8:
            raise ValueError
        stream = sys.stdout.buffer
        stream.write(_HEADER.pack(width, height))
        # 逐行输出，跳过 DIB 对齐填充，避免再分配整帧 bytes。
        for row in image:
            stream.write(memoryview(row).cast("B"))
        stream.flush()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
