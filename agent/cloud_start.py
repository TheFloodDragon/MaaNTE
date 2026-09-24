"""在控制器枚举前启动云异环；本地窗口就绪不等于账号登录或进入大世界。

此入口不依赖 MaaFramework/AgentServer。可随后打开 MXU，由云启动 Pipeline
完成登录等待、排队与游戏场景检查。不修改 MXU 的接口协议或其他控制器。
"""

import argparse
import ctypes
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from ctypes import wintypes as w
from dataclasses import dataclass

logger = logging.getLogger("maante")
CLIENT_NAME = "NTECloudGame.exe"
CLIENT_TITLE = "云·异环"
MAIN_CLASS = re.compile(r"Qt\d+QWindow(?:Icon|OwnDC)?", re.I)


class ClientError(RuntimeError):
    """只携带可公开的错误码，不携带安装路径或账号信息。"""


@dataclass(frozen=True)
class ClientWindow:
    hwnd: int
    pid: int
    size: tuple[int, int]


def validate_executable(value, expected_name=None):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ClientError("invalid_executable")
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() != ".exe":
        raise ClientError("invalid_executable")
    if expected_name and path.name.lower() != expected_name.lower():
        raise ClientError("wrong_executable")
    if not path.is_file():
        raise ClientError("executable_missing")
    return path.resolve()


def _apis():
    if sys.platform != "win32":
        raise ClientError("windows_required")
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, pointer(w.DWORD)]
    k.QueryFullProcessImageNameW.restype = w.BOOL
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    u.GetWindowThreadProcessId.argtypes = [w.HWND, pointer(w.DWORD)]
    u.GetWindowThreadProcessId.restype = w.DWORD
    u.IsWindowVisible.argtypes = [w.HWND]
    u.IsWindowVisible.restype = w.BOOL
    u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetClientRect.argtypes = [w.HWND, pointer(w.RECT)]
    u.GetClientRect.restype = w.BOOL
    u.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, pointer(ctypes.c_size_t)]
    u.SendMessageTimeoutW.restype = w.LPARAM
    return u, k


def _process_path(kernel, pid):
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        text = ctypes.create_unicode_buffer(32768)
        size = w.DWORD(len(text))
        if kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)):
            return Path(text.value)
        return None
    finally:
        kernel.CloseHandle(handle)


def client_pids(executable=None):
    """按可执行文件身份查进程；已启动但窗口尚未出现时不重复拉起。"""
    _, kernel = _apis()

    class Entry(ctypes.Structure):
        _fields_ = [
            ("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
            ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
            ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
            ("exe", w.WCHAR * 260),
        ]

    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype = w.BOOL
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.restype = w.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise ClientError("process_query_failed")
    result = set()
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.exe.lower() == CLIENT_NAME.lower():
                path = _process_path(kernel, entry.pid)
                if path is None:
                    # 受保护/提升权限的实例无法查询路径：保守地视为已在运行，避免重复拉起。
                    logger.debug("cloud_start client process path unavailable; treating as running")
                    result.add(entry.pid)
                elif executable is None or os.path.normcase(str(path)) == os.path.normcase(str(executable)):
                    result.add(entry.pid)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return result


def client_windows(executable=None):
    user, _ = _apis()
    pids = client_pids(executable)
    result = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows.restype = w.BOOL

    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        pid = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        title, name, rect = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(160), w.RECT()
        user.GetWindowTextW(hwnd, title, len(title))
        user.GetClassNameW(hwnd, name, len(name))
        if title.value != CLIENT_TITLE or not MAIN_CLASS.fullmatch(name.value):
            return True
        if user.GetClientRect(hwnd, ctypes.byref(rect)) and rect.right > 0 and rect.bottom > 0:
            result.append(ClientWindow(int(hwnd), pid.value, (rect.right, rect.bottom)))
        return True

    if not user.EnumWindows(callback_type(visit), 0):
        raise ClientError("window_query_failed")
    return result


def window_responding(hwnd):
    user, _ = _apis()
    reply = ctypes.c_size_t()
    return bool(user.SendMessageTimeoutW(hwnd, 0, 0, 0, 2, 250, ctypes.byref(reply)))


def desktop_interactive():
    """会话断开或锁屏时窗口不渲染、不接收输入；此时不能把黑帧或无响应当成客户端故障。"""
    if sys.platform != "win32":
        return False
    user, _ = _apis()
    user.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    user.OpenInputDesktop.restype = w.HANDLE
    user.CloseDesktop.argtypes = [w.HANDLE]
    user.CloseDesktop.restype = w.BOOL
    handle = user.OpenInputDesktop(0, False, 0x80000000)  # GENERIC_READ
    if not handle:
        return False
    user.CloseDesktop(handle)
    return True


def prepare_client_window(hwnd, width=1280, height=720):
    """把 Qt 主窗口客户区调成目标尺寸并完整移入工作区；不添加标题栏、不改进程 DPI。

    PrintWindow/FramePool 只能取到窗口位于屏幕内的部分，超出屏幕的区域一律是黑色，
    因此工作区放不下整个窗口时返回 False，由调用方提示用户，而不是让识别静默失败。
    """
    user, _ = _apis()
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user.GetWindowRect.restype = w.BOOL
    user.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
    user.SetWindowPos.restype = w.BOOL
    user.IsIconic.argtypes = [w.HWND]
    user.IsIconic.restype = w.BOOL
    user.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
    user.ShowWindow.restype = w.BOOL
    user.SystemParametersInfoW.argtypes = [w.UINT, w.UINT, ctypes.c_void_p, w.UINT]
    user.SystemParametersInfoW.restype = w.BOOL
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous:
        return False
    try:
        if user.IsIconic(hwnd):
            user.ShowWindow(hwnd, 9)
        client, outer, work = w.RECT(), w.RECT(), w.RECT()
        if not user.GetClientRect(hwnd, ctypes.byref(client)) or not user.GetWindowRect(hwnd, ctypes.byref(outer)):
            return False
        if not user.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0):  # SPI_GETWORKAREA
            return False
        outer_width = width + (outer.right - outer.left) - client.right
        outer_height = height + (outer.bottom - outer.top) - client.bottom
        work_width, work_height = work.right - work.left, work.bottom - work.top
        if outer_width > work_width or outer_height > work_height:
            logger.warning("cloud_start work area is smaller than the client window")
            return False
        left = work.left + (work_width - outer_width) // 2
        top = work.top + (work_height - outer_height) // 2
        if (client.right, client.bottom) == (width, height) and (outer.left, outer.top) == (left, top):
            return True
        if not user.SetWindowPos(hwnd, None, left, top, outer_width, outer_height, 0x14):  # NOZORDER|NOACTIVATE
            return False
        if not user.GetClientRect(hwnd, ctypes.byref(client)) or not user.GetWindowRect(hwnd, ctypes.byref(outer)):
            return False
        return (client.right, client.bottom) == (width, height) and outer.left >= work.left and outer.top >= work.top \
            and outer.right <= work.right and outer.bottom <= work.bottom
    finally:
        user.SetThreadDpiAwarenessContext(previous)


def _terminate_owned(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    except OSError:
        logger.warning("cloud_start cleanup failed")


def start_client(executable, timeout=90, stopped=lambda: False):
    if sys.platform != "win32":
        raise ClientError("windows_required")
    path = validate_executable(executable, CLIENT_NAME)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ClientError("invalid_timeout")
    process = None
    deadline = time.monotonic() + timeout
    try:
        if stopped():
            raise ClientError("stopped")
        if not client_pids(path):
            process = subprocess.Popen([str(path)], cwd=str(path.parent), shell=False)
            logger.info("cloud_start client launched")
        else:
            logger.info("cloud_start reuse existing client")
        relaunch_logged = False
        while time.monotonic() < deadline:
            if stopped():
                raise ClientError("stopped")
            windows = client_windows(path)
            if len(windows) > 1:
                raise ClientError("ambiguous_window")
            if windows and window_responding(windows[0].hwnd):
                logger.info("cloud_start local client ready; authentication not yet verified")
                return windows[0], process
            if process is not None and process.poll() is not None and not relaunch_logged:
                # 实机流程：客户端先运行 NTECloudUpdate.exe 自更新，再由更新器重新拉起主程序。
                # 启动进程退出不等于失败，只在截止时间内继续等待新的主窗口。
                logger.info("cloud_start launcher exited; waiting for updater relaunch")
                relaunch_logged = True
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise ClientError("client_start_timeout")
    except BaseException:
        _terminate_owned(process)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True, help="NTECloudGame.exe 的完整路径")
    parser.add_argument("--mxu", help="可选：客户端就绪后打开的 MXU.exe 完整路径")
    parser.add_argument("--instance", help="MXU 中已配置云启动任务的实例名")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args(argv)
    process = None
    try:
        if args.instance and not args.mxu:
            raise ClientError("instance_requires_mxu")
        mxu = validate_executable(args.mxu) if args.mxu else None
        _, process = start_client(args.client, args.timeout)
        if not desktop_interactive():
            logger.warning("cloud_start desktop session is disconnected or locked; the client cannot render or receive input until it is reconnected")
        if mxu:
            command = [str(mxu)]
            if args.instance:
                command += ["--autostart", "--instance", args.instance]
            subprocess.Popen(command, cwd=str(mxu.parent), shell=False)
        return 0
    except KeyboardInterrupt:
        _terminate_owned(process)
        logger.warning("cloud_start stopped")
        return 130
    except (ClientError, OSError) as exc:
        _terminate_owned(process)
        logger.error("cloud_start failed: %s", str(exc) if isinstance(exc, ClientError) else type(exc).__name__)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
