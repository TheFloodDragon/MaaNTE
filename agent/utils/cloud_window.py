"""原生客户区截图与受保护的单击；阻塞的 PrintWindow 仅在独立进程执行。"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from functools import lru_cache
import logging
import math
from pathlib import Path
import struct
import subprocess
import sys

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
_LEFT_DOWN = 0x0002
_LEFT_UP = 0x0004
_VK_LBUTTON = 0x01


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


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


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    # MOUSEINPUT 是 INPUT 联合体的最大成员，保留其原生对齐即可。
    _fields_ = [("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", ctypes.c_uint32), ("data", _InputUnion)]


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
            ("GetForegroundWindow", [], handle),
            ("GetClientRect", [handle, pointer(_Rect)], integer),
            ("ClientToScreen", [handle, pointer(_Point)], integer),
            ("WindowFromPoint", [_Point], handle),
            ("GetSystemMetrics", [integer], integer),
            ("SetCursorPos", [integer, integer], integer),
            ("GetCursorPos", [pointer(_Point)], integer),
            ("GetAsyncKeyState", [integer], ctypes.c_int16),
            ("SendInput", [uint, pointer(_Input), integer], uint),
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


def _debug_failure(stage: str, error_type: str) -> None:
    # 不记录句柄、坐标、截图、命令行、异常正文或子进程输出。
    logger.debug("窗口辅助失败 | stage=%s | error=%s", stage, error_type)


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


def _require(value) -> None:
    if not value:
        raise OSError


def _check_size(width: int, height: int) -> None:
    if not (
        0 < width <= _MAX_SIDE
        and 0 < height <= _MAX_SIDE
        and width * height <= _MAX_PIXELS
    ):
        raise ValueError


@contextmanager
def _thread_dpi(api):
    previous = api.user32.SetThreadDpiAwarenessContext(_DPI_PER_MONITOR_V2)
    _require(previous)
    try:
        yield
    finally:
        _require(api.user32.SetThreadDpiAwarenessContext(previous))


def _check_window(api, hwnd) -> None:
    _require(api.user32.IsWindow(hwnd))
    _require(api.user32.IsWindowVisible(hwnd))
    _require(not api.user32.IsIconic(hwnd))


def _client_size(api, hwnd) -> tuple[int, int]:
    rect = _Rect()
    _require(api.user32.GetClientRect(hwnd, ctypes.byref(rect)))
    if rect.left != 0 or rect.top != 0:
        raise ValueError
    width, height = rect.right, rect.bottom
    _check_size(width, height)
    return width, height


def _decode_frame(payload: bytes) -> np.ndarray:
    if (
        not isinstance(payload, bytes)
        or not _HEADER.size <= len(payload) <= _MAX_FRAME_BYTES
    ):
        raise ValueError
    width, height = _HEADER.unpack_from(payload)
    _check_size(width, height)
    if len(payload) != _HEADER.size + width * height * 3:
        raise ValueError
    return (
        np.frombuffer(payload, dtype=np.uint8, offset=_HEADER.size)
        .reshape(height, width, 3)
        .copy()
    )


def capture_window(hwnd, timeout: float = 3.0) -> np.ndarray | None:
    """返回原生客户区尺寸的 BGR 图像；失败或超时返回 None，不缩放、不改进程 DPI。"""
    if sys.platform != "win32":
        _debug_failure("capture.platform", "UnsupportedPlatform")
        return None
    if not _valid_hwnd(hwnd):
        _debug_failure("capture.arguments", "ValueError")
        return None
    stage = "capture.arguments"
    try:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
        stage = "capture.worker"
        # run 在 TimeoutExpired 时 kill + communicate/wait，回收进程和管道。
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-I",
                str(Path(__file__).resolve()),
                "--capture",
                str(hwnd),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            shell=False,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise ChildProcessError
        stage = "capture.decode"
        return _decode_frame(result.stdout)
    except Exception as exc:
        _debug_failure(stage, type(exc).__name__)
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


def _click_target(api, hwnd, point) -> tuple[int, int, int, int]:
    _check_window(api, hwnd)
    _require(api.user32.IsWindowEnabled(hwnd))
    _require(api.user32.GetAncestor(hwnd, _GA_ROOT) == hwnd)
    foreground = api.user32.GetForegroundWindow()
    _require(foreground and api.user32.GetAncestor(foreground, _GA_ROOT) == hwnd)
    width, height = _client_size(api, hwnd)
    if point[0] >= width or point[1] >= height:
        raise ValueError
    screen = _Point(*point)
    _require(api.user32.ClientToScreen(hwnd, ctypes.byref(screen)))
    left, top, screen_width, screen_height = (
        api.user32.GetSystemMetrics(index) for index in (76, 77, 78, 79)
    )
    _require(
        screen_width > 0
        and screen_height > 0
        and left <= screen.x < left + screen_width
        and top <= screen.y < top + screen_height
    )
    hit = api.user32.WindowFromPoint(screen)
    _require(hit and api.user32.GetAncestor(hit, _GA_ROOT) == hwnd)
    # 不接管用户已经按住的左键，避免 finally 释放用户的输入。
    _require(not api.user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000)
    return screen.x, screen.y, width, height


def _send_button(api, flags: int) -> bool:
    event = _Input()
    event.mi.dwFlags = flags
    return api.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) == 1


def click_window(hwnd, point: tuple[int, int]) -> bool:
    """按原生客户区坐标单击；不抢焦点，遮挡、窗口移动或光标不符时拒绝发送。"""
    if sys.platform != "win32":
        _debug_failure("click.platform", "UnsupportedPlatform")
        return False
    if not _valid_hwnd(hwnd) or not _valid_point(point):
        _debug_failure("click.arguments", "ValueError")
        return False
    stage = "click.api"
    try:
        api = _get_win32()
        stage = "click.dpi"
        with _thread_dpi(api):
            stage = "click.validate"
            target = _click_target(api, hwnd, point)
            stage = "click.cursor"
            _require(api.user32.SetCursorPos(*target[:2]))
            stage = "click.revalidate"
            if _click_target(api, hwnd, point) != target:
                raise ValueError
            cursor = _Point()
            _require(api.user32.GetCursorPos(ctypes.byref(cursor)))
            if (cursor.x, cursor.y) != target[:2]:
                raise ValueError
            stage = "click.input"
            try:
                pressed = _send_button(api, _LEFT_DOWN)
            finally:
                # 按下调用即使异常也可能已经送达，必须在 finally 尝试释放一次。
                released = _send_button(api, _LEFT_UP)
            _require(pressed and released)
        return True
    except Exception as exc:
        _debug_failure(stage, type(exc).__name__)
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
