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
_GW_OWNER = 4
_GA_ROOT = 2
_CLIENT_ERROR_CODES = frozenset({
    "invalid_executable", "wrong_executable", "executable_missing",
    "windows_required", "process_query_failed", "process_identity_unknown", "window_query_failed",
    "invalid_timeout", "stopped", "ambiguous_window", "client_start_timeout",
    "instance_requires_mxu", "client_exited", "game_root_timeout",
    "game_root_not_unique", "game_root_pid_changed", "game_root_identity_changed",
    "game_root_not_new", "invalid_handoff_timeout", "invalid_stability_samples",
})
_last_process_query = None


class ClientError(RuntimeError):
    """只携带可公开的错误码，不携带安装路径或账号信息。"""


def _safe_reason(exc):
    # 只允许本站定义的错误码；即使 ClientError 被传入外部异常正文也不回显。
    if isinstance(exc, ClientError):
        if len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in _CLIENT_ERROR_CODES:
            return exc.args[0]
        return "client_error"
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "process_wait_timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "file_not_found"
    if isinstance(exc, OSError):
        return "os_error"
    return "unexpected_error"


def _log_failure(stage, exc, started):
    reason = _safe_reason(exc)
    cancelled = reason in ("stopped", "interrupted")
    log = logger.warning if cancelled else logger.error
    log(
        "云客户端操作终止 | stage=%s | result=%s | reason=%s | elapsed=%.3fs",
        stage, "cancelled" if cancelled else "failed", reason,
        time.monotonic() - started,
    )


@dataclass(frozen=True)
class ClientWindow:
    hwnd: int
    pid: int
    size: tuple[int, int]
    executable: str | None = None
    title: str = CLIENT_TITLE
    class_name: str = ""
    owner: int = 0
    root_hwnd: int | None = None
    created_at: int | None = None


@dataclass(frozen=True)
class WindowSnapshot:
    """客户端进程内某个顶层窗口的脱敏身份快照。"""

    hwnd: int
    pid: int
    executable: str | None
    title: str
    class_name: str
    visible: bool
    owner: int
    root_hwnd: int | None
    client_size: tuple[int, int]
    rect: tuple[int, int, int, int]
    created_at: int | None = None

    @property
    def is_root(self):
        # GetAncestor(GA_ROOT) 失败时 root_hwnd 为 None；绝不能回退为自身。
        return self.root_hwnd is not None and self.hwnd == self.root_hwnd and self.owner == 0

    @property
    def is_valid_size(self):
        width, height = self.client_size
        return width > 1 and height > 1

    @property
    def has_identity(self):
        return (
            self.hwnd > 0
            and self.pid > 0
            and bool(self.executable)
            and self.created_at is not None
            and self.created_at > 0
        )


@dataclass(frozen=True)
class _ProcessIdentity:
    path: str
    created_at: int


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
    k.GetProcessTimes.argtypes = [
        w.HANDLE,
        pointer(w.FILETIME),
        pointer(w.FILETIME),
        pointer(w.FILETIME),
        pointer(w.FILETIME),
    ]
    k.GetProcessTimes.restype = w.BOOL
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    u.GetWindowThreadProcessId.argtypes = [w.HWND, pointer(w.DWORD)]
    u.GetWindowThreadProcessId.restype = w.DWORD
    u.IsWindow.argtypes = [w.HWND]
    u.IsWindow.restype = w.BOOL
    u.IsWindowVisible.argtypes = [w.HWND]
    u.IsWindowVisible.restype = w.BOOL
    u.GetWindow.argtypes = [w.HWND, w.UINT]
    u.GetWindow.restype = w.HWND
    u.GetAncestor.argtypes = [w.HWND, w.UINT]
    u.GetAncestor.restype = w.HWND
    u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindowRect.argtypes = [w.HWND, pointer(w.RECT)]
    u.GetWindowRect.restype = w.BOOL
    u.GetClientRect.argtypes = [w.HWND, pointer(w.RECT)]
    u.GetClientRect.restype = w.BOOL
    u.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, pointer(ctypes.c_size_t)]
    u.SendMessageTimeoutW.restype = w.LPARAM
    return u, k


def _normalize_process_path(value):
    if value is None:
        return None
    try:
        text = os.fspath(value)
    except TypeError:
        return None
    if not isinstance(text, str) or not text:
        return None
    # 只在身份比较时规范化；绝不把路径写入日志。
    return os.path.normcase(os.path.realpath(os.path.abspath(text)))


def _filetime_value(value):
    return int(value.dwLowDateTime) | (int(value.dwHighDateTime) << 32)


def _process_identity(kernel, pid):
    """用同一个受限句柄读取进程路径和创建时间；任一失败都返回 unknown。"""
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        text = ctypes.create_unicode_buffer(32768)
        size = w.DWORD(len(text))
        created = w.FILETIME()
        exited = w.FILETIME()
        kernel_time = w.FILETIME()
        user_time = w.FILETIME()
        if not kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)):
            return None
        if not kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        path = _normalize_process_path(text.value)
        created_at = _filetime_value(created)
        if not path or created_at <= 0:
            return None
        return _ProcessIdentity(path=path, created_at=created_at)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    finally:
        kernel.CloseHandle(handle)


def _process_path(kernel, pid):
    identity = _process_identity(kernel, pid)
    return Path(identity.path) if identity is not None else None


def _enumerated_client_identities(kernel, executable=None):
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
    result = {}
    unavailable = 0
    expected_path = _normalize_process_path(executable)
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.exe.lower() == CLIENT_NAME.lower():
                identity = _process_identity(kernel, entry.pid)
                if identity is None:
                    # 身份未知时 fail closed，不能把 PID 当作可复用的客户端身份。
                    unavailable += 1
                elif expected_path is None or identity.path == expected_path:
                    result[entry.pid] = identity
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return result, unavailable


def client_pids(executable=None):
    """按可执行文件路径和创建时间识别进程；身份未知时不返回 PID。"""
    global _last_process_query
    _, kernel = _apis()
    identities, unavailable = _enumerated_client_identities(kernel, executable)
    if unavailable and not identities:
        raise ClientError("process_identity_unknown")
    result = set(identities)
    state = (len(result), unavailable)
    if state != _last_process_query:
        if unavailable or (_last_process_query is not None and _last_process_query[1]):
            logger.debug(
                "云客户端进程身份检查变化 | stage=start.process_query | reason=%s | candidates=%d | inaccessible=%d",
                "identity_unavailable" if unavailable else "identity_query_recovered",
                len(result), unavailable,
            )
        _last_process_query = state
    return result


def _window_process_identities(kernel, pids):
    return {pid: _process_identity(kernel, pid) for pid in pids}


def window_snapshots(executable=None, *, include_hidden=False):
    """枚举云客户端窗口并记录进程创建时间；任一身份未知的窗口直接忽略。"""
    user, kernel = _apis()
    pids = client_pids(executable)
    process_identities = _window_process_identities(kernel, pids)

    result = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows.restype = w.BOOL

    def visit(hwnd, _):
        if not user.IsWindow(hwnd):
            return True
        visible = bool(user.IsWindowVisible(hwnd))
        if not include_hidden and not visible:
            return True
        pid = w.DWORD()
        if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value not in pids:
            return True
        identity = process_identities.get(pid.value)
        if identity is None:
            return True
        title = ctypes.create_unicode_buffer(256)
        class_name = ctypes.create_unicode_buffer(160)
        window_rect, client_rect = w.RECT(), w.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(window_rect)):
            return True
        if not user.GetClientRect(hwnd, ctypes.byref(client_rect)):
            return True
        hwnd_value = int(hwnd)
        owner = int(user.GetWindow(hwnd, _GW_OWNER) or 0)
        root_value = int(user.GetAncestor(hwnd, _GA_ROOT) or 0)
        result.append(
            WindowSnapshot(
                hwnd=hwnd_value,
                pid=pid.value,
                executable=identity.path,
                title=(user.GetWindowTextW(hwnd, title, len(title)) and title.value) or "",
                class_name=(user.GetClassNameW(hwnd, class_name, len(class_name)) and class_name.value) or "",
                visible=visible,
                owner=owner,
                root_hwnd=root_value or None,
                client_size=(client_rect.right - client_rect.left, client_rect.bottom - client_rect.top),
                rect=(window_rect.left, window_rect.top, window_rect.right, window_rect.bottom),
                created_at=identity.created_at,
            )
        )
        return True

    if not user.EnumWindows(callback_type(visit), 0):
        raise ClientError("window_query_failed")
    return result


def _window_identity(item):
    if not isinstance(item, (ClientWindow, WindowSnapshot)):
        return None
    if item.hwnd <= 0 or item.pid <= 0:
        return None
    created_at = item.created_at
    if isinstance(created_at, bool) or not isinstance(created_at, int) or created_at <= 0:
        return None
    executable = _normalize_process_path(item.executable)
    if executable is None:
        return None
    return item.hwnd, item.pid, created_at, executable


def _owner_chain_is_safe(item, by_hwnd):
    """检查 owner 链，避免损坏的多层 owner/cycle 被当成游戏根。"""
    current = item
    visited = {item.hwnd}
    while current.owner:
        if current.owner in visited:
            return False
        visited.add(current.owner)
        current = by_hwnd.get(current.owner)
        if current is None:
            return False
    return True


def _is_game_root_snapshot(item, snapshots=None):
    if not isinstance(item, WindowSnapshot):
        return False
    if not item.has_identity or not item.visible or not item.is_root or not item.is_valid_size:
        return False
    if item.title != CLIENT_TITLE or MAIN_CLASS.fullmatch(item.class_name) is None:
        return False
    if item.rect[2] <= item.rect[0] or item.rect[3] <= item.rect[1]:
        return False
    if snapshots is not None:
        by_hwnd = {candidate.hwnd: candidate for candidate in snapshots}
        if not _owner_chain_is_safe(item, by_hwnd):
            return False
    return True


def _root_window_snapshots(snapshots):
    """保留可作为游戏 root 的顶层窗口，排除 owned/tool/popup 窗口。"""
    snapshots = tuple(snapshots)
    return [item for item in snapshots if _is_game_root_snapshot(item, snapshots)]


def client_windows(executable=None):
    return [
        ClientWindow(
            hwnd=item.hwnd,
            pid=item.pid,
            size=item.client_size,
            executable=item.executable,
            title=item.title,
            class_name=item.class_name,
            owner=item.owner,
            root_hwnd=item.root_hwnd,
            created_at=item.created_at,
        )
        for item in _root_window_snapshots(window_snapshots(executable))
    ]


def _window_identity_signature(item):
    return (
        item.hwnd,
        item.pid,
        item.created_at,
        _normalize_process_path(item.executable),
        item.title,
        item.class_name,
        item.owner,
        item.root_hwnd,
        item.client_size,
    )


def _game_root_candidate_details(launcher, snapshots):
    expected = _window_identity(launcher)
    if expected is None:
        return None, "identity_changed"
    snapshots = tuple(snapshots)
    roots = _root_window_snapshots(snapshots)
    old_windows = [item for item in snapshots if item.hwnd == launcher.hwnd]
    old_visible_root = any(
        item.visible
        and (item.root_hwnd is None or (item.owner == 0 and item.root_hwnd == item.hwnd))
        for item in old_windows
    )
    matching = [
        item for item in roots
        if item.hwnd != launcher.hwnd and _window_identity(item)[1:] == expected[1:]
    ]
    if old_visible_root:
        return None, "not_unique" if matching else "not_new"
    if len(matching) > 1:
        return None, "not_unique"
    if matching:
        return matching[0], None

    other_roots = [item for item in roots if item.hwnd != launcher.hwnd]
    if other_roots and any(item.pid != expected[1] for item in other_roots):
        return None, "pid_changed"
    if other_roots:
        return None, "identity_changed"
    return None, "waiting"


def game_root_candidate(launcher, snapshots):
    """从一次只读窗口快照中选择唯一、同进程身份的新游戏根窗口。"""
    candidate, _ = _game_root_candidate_details(launcher, snapshots)
    return candidate


def validate_bound_window(bound, snapshots=None, *, executable=None):
    """校验已绑定窗口仍是同一 PID/创建时间/路径的可见 Qt 根窗口。"""
    expected = _window_identity(bound)
    if expected is None:
        return None
    expected_path = _normalize_process_path(executable) if executable is not None else None
    if expected_path is not None and expected[3] != expected_path:
        return None
    if snapshots is None:
        snapshots = window_snapshots(executable, include_hidden=True)
    matches = [item for item in snapshots if isinstance(item, WindowSnapshot) and item.hwnd == expected[0]]
    if len(matches) != 1:
        return None
    item = matches[0]
    if _window_identity(item) != expected:
        return None
    if not _is_game_root_snapshot(item, tuple(snapshots)):
        return None
    return item


def bound_window(bound, snapshots=None, *, executable=None):
    """validate_bound_window 的简短公共别名，保持绑定校验只读。"""
    return validate_bound_window(bound, snapshots, executable=executable)


def wait_for_game_root(
    launcher,
    executable=None,
    *,
    timeout=30.0,
    stable_samples=2,
    poll_interval=0.1,
    stopped=lambda: False,
):
    """有界只读观察，等待同一客户端身份中的唯一稳定新游戏根窗口。"""
    if _window_identity(launcher) is None:
        raise ClientError("game_root_identity_changed")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise ClientError("invalid_handoff_timeout")
    if isinstance(stable_samples, bool) or not isinstance(stable_samples, int) or not 2 <= stable_samples <= 10:
        raise ClientError("invalid_stability_samples")
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, (int, float))
        or not math.isfinite(poll_interval)
        or not 0 < poll_interval <= 5
    ):
        raise ClientError("invalid_handoff_timeout")

    deadline = time.monotonic() + timeout
    previous_signature = None
    stable = 0
    last_state = None
    while True:
        if time.monotonic() >= deadline:
            break
        if stopped():
            raise ClientError("stopped")
        pids = client_pids(executable)
        if launcher.pid not in pids:
            raise ClientError("game_root_pid_changed")
        snapshots = window_snapshots(executable, include_hidden=True)
        if time.monotonic() >= deadline:
            break
        candidate, reason = _game_root_candidate_details(launcher, snapshots)
        if reason == "identity_changed":
            raise ClientError("game_root_identity_changed")
        if reason == "pid_changed":
            raise ClientError("game_root_pid_changed")
        if reason == "not_unique":
            raise ClientError("game_root_not_unique")
        if candidate is not None:
            if stopped():
                raise ClientError("stopped")
            if time.monotonic() >= deadline:
                break
            signature = _window_identity_signature(candidate)
            stable = stable + 1 if signature == previous_signature else 1
            previous_signature = signature
            state = (candidate.hwnd, candidate.pid, candidate.client_size, stable)
            if state != last_state:
                logger.debug(
                    "检测到云客户端游戏窗口候选 | stage=handoff.window | hwnd_known=true | pid=%d | client=%dx%d | stable=%d/%d",
                    candidate.pid, candidate.client_size[0], candidate.client_size[1], stable, stable_samples,
                )
                last_state = state
            if stable >= stable_samples:
                if stopped():
                    raise ClientError("stopped")
                if time.monotonic() >= deadline:
                    break
                logger.info(
                    "云客户端游戏窗口已稳定 | stage=handoff.window | pid=%d | client=%dx%d | samples=%d",
                    candidate.pid, candidate.client_size[0], candidate.client_size[1], stable,
                )
                return candidate
        else:
            stable = 0
            previous_signature = None
            state = (reason, len(_root_window_snapshots(snapshots)))
            if state != last_state:
                logger.debug(
                    "等待云客户端游戏窗口交接 | stage=handoff.window | roots=%d | new_root=%s",
                    state[1], reason not in ("not_new", "waiting"),
                )
                last_state = state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    raise ClientError("game_root_timeout")


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


def prepare_client_window(hwnd, width=1280, height=720, *, fit_to_work_area=False):
    """把 Qt 主窗口客户区调成 1280×720 并尽量移入工作区；不添加标题栏、不改进程 DPI。

    工作区放不下完整窗口时：``fit_to_work_area`` 为假则返回 ``work_area_too_small``；
    为真则保持 1280×720 客户区、左上对齐、允许右/下部分离屏——``PrintWindow``
    带 ``PW_RENDERFULLCONTENT`` 能取到离屏窗口的完整内容，缩小物理窗口反而会在
    截图回放缩放时模糊识别模板。
    """
    started = time.monotonic()
    stage = "window.api"
    logger.debug("开始准备云客户端窗口 | stage=window.prepare")

    def failed(reason):
        logger.warning(
            "云客户端窗口准备未完成 | stage=%s | reason=%s | elapsed=%.3fs",
            stage, reason, time.monotonic() - started,
        )
        return False

    try:
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
        stage = "window.dpi"
        previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            return failed("dpi_context_unavailable")
        try:
            stage = "window.restore"
            if user.IsIconic(hwnd):
                logger.debug("还原最小化的云客户端窗口 | stage=window.restore")
                user.ShowWindow(hwnd, 9)
            stage = "window.bounds"
            client, outer, work = w.RECT(), w.RECT(), w.RECT()
            if not user.GetClientRect(hwnd, ctypes.byref(client)):
                return failed("client_bounds_query_failed")
            if not user.GetWindowRect(hwnd, ctypes.byref(outer)):
                return failed("window_bounds_query_failed")
            if not user.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0):  # SPI_GETWORKAREA
                return failed("work_area_query_failed")
            outer_width = width + (outer.right - outer.left) - client.right
            outer_height = height + (outer.bottom - outer.top) - client.bottom
            work_width, work_height = work.right - work.left, work.bottom - work.top
            logger.debug(
                "云客户端窗口尺寸检查 | stage=window.bounds | client=%dx%d | required=%dx%d | work=%dx%d",
                client.right, client.bottom, outer_width, outer_height, work_width, work_height,
            )
            offscreen = False
            if outer_width > work_width or outer_height > work_height:
                if not fit_to_work_area:
                    return failed("work_area_too_small")
                offscreen = True
            physical_width, physical_height = width, height
            if offscreen:
                # 保持完整 1280×720 客户区、左上对齐，允许右/下离屏；
                # PrintWindow(PW_RENDERFULLCONTENT) 对离屏窗口仍能取到完整内容。
                left, top = work.left, work.top
                logger.info(
                    "工作区放不下完整窗口，保持 1280×720 并允许部分离屏 | stage=window.resize | required=%dx%d | work=%dx%d",
                    outer_width, outer_height, work_width, work_height,
                )
            else:
                left = work.left + (work_width - outer_width) // 2
                top = work.top + (work_height - outer_height) // 2
            if (client.right, client.bottom) == (physical_width, physical_height) and (outer.left, outer.top) == (left, top):
                ready, reason = True, "already_ready"
            else:
                stage = "window.resize"
                logger.info(
                    "调整云客户端窗口尺寸与工作区位置 | stage=window.resize | target=%dx%d | offscreen=%s",
                    physical_width, physical_height, offscreen,
                )
                if not user.SetWindowPos(hwnd, None, left, top, outer_width, outer_height, 0x14):  # NOZORDER|NOACTIVATE
                    return failed("window_position_failed")
                stage = "window.verify"
                if not user.GetClientRect(hwnd, ctypes.byref(client)):
                    return failed("client_bounds_query_failed")
                if not user.GetWindowRect(hwnd, ctypes.byref(outer)):
                    return failed("window_bounds_query_failed")
                size_ok = (client.right, client.bottom) == (physical_width, physical_height)
                if offscreen:
                    # 离屏窗口只要求尺寸正确且左上角已就位，不要求右/下边界落在工作区内。
                    ready = size_ok and outer.left == left and outer.top == top
                    reason = "resized_offscreen" if ready else "postcheck_mismatch"
                else:
                    ready = size_ok and outer.left >= work.left and outer.top >= work.top \
                        and outer.right <= work.right and outer.bottom <= work.bottom
                    reason = "resized" if ready else "postcheck_mismatch"
        finally:
            previous_stage = stage
            stage = "window.restore_dpi"
            user.SetThreadDpiAwarenessContext(previous)
            stage = previous_stage
        log = logger.info if ready else logger.warning
        log(
            "云客户端窗口准备结束 | stage=window.ready | result=%s | reason=%s | client=%dx%d | elapsed=%.3fs",
            "ready" if ready else "not_ready", reason, client.right, client.bottom,
            time.monotonic() - started,
        )
        return ready
    except BaseException as exc:
        _log_failure(stage, exc, started)
        raise


def _terminate_owned(process):
    if process is None:
        return
    started = time.monotonic()
    stage = "cleanup.poll"
    try:
        returncode = process.poll()
        if returncode is not None:
            logger.debug(
                "本次启动进程无需清理 | stage=cleanup.poll | reason=already_exited | returncode=%d | elapsed=%.3fs",
                returncode, time.monotonic() - started,
            )
            return
        logger.info("回收本次启动的云客户端进程 | stage=cleanup.terminate")
        stage = "cleanup.terminate"
        method = "terminate"
        try:
            process.terminate()
            stage = "cleanup.wait"
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning(
                "云客户端进程未及时退出，执行强制清理 | stage=%s | reason=terminate_timeout | timeout=5s | elapsed=%.3fs",
                stage, time.monotonic() - started,
            )
            stage = "cleanup.kill"
            method = "kill"
            process.kill()
            stage = "cleanup.kill_wait"
            process.wait(timeout=5)
        except OSError as exc:
            logger.warning(
                "云客户端进程清理未完成 | stage=%s | reason=%s | elapsed=%.3fs",
                stage, _safe_reason(exc), time.monotonic() - started,
            )
            return
        logger.info(
            "本次启动进程清理完成 | stage=cleanup.done | method=%s | elapsed=%.3fs",
            method, time.monotonic() - started,
        )
    except BaseException as exc:
        logger.warning(
            "云客户端进程清理中断 | stage=%s | reason=%s | elapsed=%.3fs",
            stage, _safe_reason(exc), time.monotonic() - started,
        )
        raise


def _cleanup_after_failure(process):
    """异常处理路径专用：只清理自有进程，保留正在处理的原始异常。"""
    try:
        _terminate_owned(process)
    except BaseException as exc:
        # 独立调用 _terminate_owned 仍保留其异常语义；此处不让清理异常
        # （包括二次中断）替换原始启动/取消原因及 CLI 退出码。
        logger.debug(
            "清理异常不替换原始终止原因 | stage=cleanup.preserve_failure | reason=%s",
            _safe_reason(exc),
        )


def start_client(executable, timeout=90, stopped=lambda: False):
    started = time.monotonic()
    stage = "start.platform"
    logger.info("开始准备本地云客户端 | stage=start.begin")
    try:
        if sys.platform != "win32":
            raise ClientError("windows_required")
        stage = "start.executable"
        path = validate_executable(executable, CLIENT_NAME)
        stage = "start.timeout"
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ClientError("invalid_timeout")
    except BaseException as exc:
        _log_failure(stage, exc, started)
        raise
    process = None
    deadline = time.monotonic() + timeout
    try:
        stage = "start.check_stop"
        if stopped():
            raise ClientError("stopped")
        stage = "start.process_query"
        pids = client_pids(path)
        if not pids:
            stage = "start.launch"
            process = subprocess.Popen([str(path)], cwd=str(path.parent), shell=False)
            logger.info(
                "已启动本地云客户端 | stage=start.launch | elapsed=%.3fs",
                time.monotonic() - started,
            )
        else:
            logger.info(
                "复用已运行的云客户端 | stage=start.reuse | processes=%d | elapsed=%.3fs",
                len(pids), time.monotonic() - started,
            )
        logger.info("等待本地云客户端窗口就绪 | stage=start.wait | timeout=%.1fs", timeout)
        relaunch_logged = False
        last_wait_state = None
        while time.monotonic() < deadline:
            stage = "start.wait"
            if stopped():
                raise ClientError("stopped")
            stage = "start.windows"
            windows = client_windows(path)
            if len(windows) > 1:
                logger.debug("检测到多个云客户端候选窗口 | stage=start.windows | candidates=%d", len(windows))
                raise ClientError("ambiguous_window")
            stage = "start.respond"
            if windows and window_responding(windows[0].hwnd):
                logger.info(
                    "本地云客户端窗口已就绪，尚未验证登录 | stage=start.ready | candidates=%d | client=%dx%d | elapsed=%.3fs",
                    len(windows), *windows[0].size, time.monotonic() - started,
                )
                return windows[0], process
            state = (len(windows), "window_unresponsive" if windows else "window_missing")
            if state != last_wait_state:
                log = logger.warning if windows else logger.debug
                log(
                    "本地云客户端窗口等待状态变化 | stage=start.wait | reason=%s | candidates=%d | elapsed=%.3fs",
                    state[1], state[0], time.monotonic() - started,
                )
                last_wait_state = state
            stage = "start.updater"
            returncode = process.poll() if process is not None else None
            if returncode is not None and not relaunch_logged:
                # 实机流程：客户端先运行 NTECloudUpdate.exe 自更新，再由更新器重新拉起主程序。
                # 启动进程退出不等于失败，只在截止时间内继续等待新的主窗口。
                logger.info(
                    "启动进程已退出，继续等待更新器重启主窗口 | stage=start.updater | returncode=%d | elapsed=%.3fs",
                    returncode, time.monotonic() - started,
                )
                relaunch_logged = True
            stage = "start.wait"
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        stage = "start.wait"
        raise ClientError("client_start_timeout")
    except BaseException as exc:
        _log_failure(stage, exc, started)
        _cleanup_after_failure(process)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True, help="NTECloudGame.exe 的完整路径")
    parser.add_argument("--mxu", help="可选：客户端就绪后打开的 MXU.exe 完整路径")
    parser.add_argument("--instance", help="MXU 中已配置云启动任务的实例名")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args(argv)
    process = None
    started = time.monotonic()
    stage = "cli.arguments"
    logger.info("云客户端启动入口开始 | stage=cli.begin")
    try:
        if args.instance and not args.mxu:
            raise ClientError("instance_requires_mxu")
        stage = "cli.mxu_validate"
        mxu = validate_executable(args.mxu) if args.mxu else None
        stage = "cli.client"
        _, process = start_client(args.client, args.timeout)
        stage = "cli.desktop"
        if not desktop_interactive():
            # 截图与点击均为后台方式，断开桌面不影响运行，仅作排查信息记录。
            logger.info(
                "桌面会话已断开或锁定，云客户端将以后台方式截图和点击 | stage=cli.desktop | desktop_interactive=false"
            )
        if mxu:
            stage = "cli.mxu_launch"
            logger.info(
                "准备启动 MXU | stage=cli.mxu_launch | mode=%s",
                "autostart" if args.instance else "manual",
            )
            command = [str(mxu)]
            if args.instance:
                command += ["--autostart", "--instance", args.instance]
            subprocess.Popen(command, cwd=str(mxu.parent), shell=False)
            logger.info("MXU 启动请求已完成 | stage=cli.mxu_ready | elapsed=%.3fs", time.monotonic() - started)
        logger.info("云客户端启动入口完成 | stage=cli.ready | elapsed=%.3fs", time.monotonic() - started)
        return 0
    except KeyboardInterrupt:
        _cleanup_after_failure(process)
        # start_client 已记录具体终止阶段，入口只补充 DEBUG 结果，避免重复告警。
        log = logger.debug if stage == "cli.client" else logger.warning
        log(
            "云客户端启动入口已取消 | stage=%s | reason=interrupted | elapsed=%.3fs",
            stage, time.monotonic() - started,
        )
        return 130
    except (ClientError, OSError) as exc:
        _cleanup_after_failure(process)
        log = logger.debug if stage == "cli.client" else logger.error
        log(
            "云客户端启动入口终止 | stage=%s | reason=%s | elapsed=%.3fs",
            stage, _safe_reason(exc), time.monotonic() - started,
        )
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
