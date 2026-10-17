"""网页云异环阶段 0 开发探针（不进入发行包）。

launch 使用独立 Edge app/profile；用户手动登录，不读取凭据、不代填验证码或购买。
probe 只读测量 #gameVideo，截图矩阵默认零输入、不保存截图。输入必须显式指定
--allow-input、单个 --input-method 和 --click X,Y / --key KEY（二选一），且只执行一次。
坐标为客户区物理像素；未知 CSS 偏移不猜测。原生调用成功不代表游戏响应成功。
probe 成功、失败或中断后均关闭本次 launch 的浏览器，包括 CDP 端口。
原生控制器在有截止时间的独立 worker 中运行，worker 用 os._exit 避免析构卡死。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

try:
    import numpy as np
except ImportError:
    np = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://yh.wanmei.com/cloud/"
DEFAULT_PROFILE = ROOT / ".narrafork" / "edge-cloud-profile"
DEFAULT_REPORT_ROOT = ROOT / ".narrafork" / "cloud-web-probe"
DEFAULT_STATE = DEFAULT_REPORT_ROOT / "state.json"
WINDOW_CLASS = "Chrome_WidgetWin_1"
CAPTURE_METHODS = ("PrintWindow", "FramePool", "DXGI_DesktopDup_Window")
MOUSE_METHODS = ("SendMessageWithWindowPos", "PostMessage", "Seize")
KEY_METHODS = ("PostMessage", "Seize")
STATES = ("foreground", "background", "occluded", "minimized")
KEY_NAMES = {"ESC": 0x1B, "ENTER": 0x0D, "SPACE": 0x20, "F13": 0x7C}
MAX_RESPONSE = 256 * 1024
MAX_FRAME_PIXELS = 4096 * 2160
WORKER_TIMEOUT = 20.0
ERROR_CODES = frozenset("""
windows_required edge_missing invalid_url invalid_profile profile_in_use
browser_launch_failed browser_exited window_missing window_not_unique
window_query_failed controller_connect_failed screencap_failed operation_timeout
process_mismatch empty_frame invalid_geometry cdp_unavailable cdp_target_missing
cdp_protocol_error input_requires_allow_flag input_failed input_unsafe
state_missing invalid_arguments desktop_unavailable worker_failed report_write_failed
interrupted timeout connection_failed file_not_found permission_denied unexpected_error
""".split())


class ProbeError(RuntimeError):
    """异常只携带固定错误码。"""


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "cloud-web-probe: invalid_arguments; use --help\n")


def fail(code):
    raise ProbeError(code if code in ERROR_CODES else "unexpected_error")


def safe_reason(exc):
    if isinstance(exc, ProbeError) and len(exc.args) == 1:
        code = exc.args[0]
        if isinstance(code, str) and code in ERROR_CODES:
            return code
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(exc, (ConnectionError, urllib.error.URLError)):
        return "connection_failed"
    if isinstance(exc, FileNotFoundError):
        return "file_not_found"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    return "unexpected_error"


def parse_size(value):
    match = re.fullmatch(r"([1-9]\d{2,3})x([1-9]\d{2,3})", value)
    if not match:
        raise argparse.ArgumentTypeError("invalid_size")
    width, height = map(int, match.groups())
    if not 320 <= width <= 4096 or not 240 <= height <= 2160:
        raise argparse.ArgumentTypeError("invalid_size")
    return width, height


def parse_point(value):
    match = re.fullmatch(r"(\d{1,4}),(\d{1,4})", value)
    if not match:
        raise argparse.ArgumentTypeError("invalid_point")
    return tuple(map(int, match.groups()))


def parse_key(value):
    text = value.upper()
    if text in KEY_NAMES:
        return KEY_NAMES[text]
    try:
        key = int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid_key") from exc
    if not 1 <= key <= 255:
        raise argparse.ArgumentTypeError("invalid_key")
    return key


def positive_timeout(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid_timeout") from exc
    if not math.isfinite(number) or not 1 <= number <= 120:
        raise argparse.ArgumentTypeError("invalid_timeout")
    return number


def csv_arg(allowed):
    def parse(value):
        parts = tuple(dict.fromkeys(value.split(",")))
        if not parts or any(part not in allowed for part in parts):
            raise argparse.ArgumentTypeError("invalid_choice")
        return parts
    return parse


def validate_url(value):
    if value != DEFAULT_URL:
        fail("invalid_url")
    return value


def url_arg(value):
    if value != DEFAULT_URL:
        raise argparse.ArgumentTypeError("invalid_url")
    return value


def artifact_path(value):
    path = Path(value).expanduser().resolve()
    root = (ROOT / ".narrafork").resolve()
    if root not in path.parents:
        fail("invalid_arguments")
    return path


def validate_profile(value):
    """仅用仓库忽略目录下明确命名的专用 profile，禁止日常浏览器目录。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        fail("invalid_profile")
    path = path.resolve()
    if path.parent != (ROOT / ".narrafork").resolve() or not re.fullmatch(
        r"edge-cloud-profile(?:-[A-Za-z0-9_-]+)?", path.name
    ):
        fail("invalid_profile")
    return path


def find_edge(value=None):
    candidates = [Path(value)] if value else [
        Path(os.environ[name]) / "Microsoft/Edge/Application/msedge.exe"
        for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
        if os.environ.get(name)
    ]
    for path in candidates:
        if path.is_absolute() and path.name.lower() == "msedge.exe" and path.is_file():
            return path.resolve()
    fail("edge_missing")


def build_edge_command(edge, profile, *, url=DEFAULT_URL, size=(1280, 760),
                       layout="app", cdp=False, keep_rendering=True):
    validate_url(url)
    profile = validate_profile(profile)
    if layout not in ("app", "kiosk"):
        fail("invalid_arguments")
    width, height = parse_size("%dx%d" % tuple(size))
    # Edge 的兼容层可能让启动进程退出后另起浏览器；禁止该重启以保留
    # Popen 句柄对应的进程身份，避免误报 profile_in_use 并遗留无主窗口。
    command = [str(edge), "--user-data-dir=%s" % profile, "--no-first-run",
               "--no-default-browser-check", "--disable-sync", "--disable-background-mode",
               "--edge-skip-compat-layer-relaunch",
               "--window-size=%d,%d" % (width, height)]
    if keep_rendering:
        command += ["--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows"]
    if cdp:
        command += ["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"]
    command += (["--kiosk", "--edge-kiosk-type=fullscreen", url] if layout == "kiosk"
                else ["--app=%s" % url])
    return command


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    rect: tuple
    client_size: tuple
    client_origin: tuple
    dpi: int


_WIN32 = None
_WORKER_KEEPALIVE = []  # worker 结束前持有原生对象，防止返回/异常展开时触发阻塞析构。


def _win32():
    global _WIN32
    if _WIN32 is not None:
        return _WIN32
    if sys.platform != "win32":
        fail("windows_required")
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    specs = {
        "EnumWindows": ([callback, w.LPARAM], w.BOOL),
        "IsWindow": ([w.HWND], w.BOOL),
        "IsWindowVisible": ([w.HWND], w.BOOL),
        "IsIconic": ([w.HWND], w.BOOL),
        "GetWindow": ([w.HWND, w.UINT], w.HWND),
        "GetAncestor": ([w.HWND, w.UINT], w.HWND),
        "GetWindowThreadProcessId": ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        "GetClassNameW": ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
        "GetClientRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
        "ClientToScreen": ([w.HWND, ctypes.POINTER(w.POINT)], w.BOOL),
        "ShowWindow": ([w.HWND, ctypes.c_int], w.BOOL),
        "SetForegroundWindow": ([w.HWND], w.BOOL),
        "GetForegroundWindow": ([], w.HWND),
        "WindowFromPoint": ([w.POINT], w.HWND),
        "PostMessageW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
        "GetDpiForWindow": ([w.HWND], w.UINT),
        "SetThreadDpiAwarenessContext": ([w.HANDLE], w.HANDLE),
        "OpenInputDesktop": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "SwitchDesktop": ([w.HANDLE], w.BOOL),
        "CloseDesktop": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in specs.items():
        fn = getattr(user, name)
        fn.argtypes, fn.restype = args, result
    specs = {
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "GetProcessTimes": ([w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        "QueryFullProcessImageNameW": ([w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
    }
    for name, (args, result) in specs.items():
        fn = getattr(kernel, name)
        fn.argtypes, fn.restype = args, result
    _WIN32 = SimpleNamespace(user=user, kernel=kernel, callback_type=callback)
    return _WIN32


def physical_coordinates():
    if not _win32().user.SetThreadDpiAwarenessContext(w.HANDLE(-4)):
        fail("invalid_geometry")


def desktop_interactive():
    user = _win32().user
    desktop = user.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desktop:
        return False
    try:
        return bool(user.SwitchDesktop(desktop))
    finally:
        user.CloseDesktop(desktop)


def normalized_exe(path):
    return os.path.normcase(os.path.realpath(str(path)))


def process_identity(handle):
    kernel = _win32().kernel
    created, exited, system, user = (w.FILETIME() for _ in range(4))
    buffer, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
    if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(system), ctypes.byref(user)):
        fail("process_mismatch")
    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        fail("process_mismatch")
    return {"created": (created.dwHighDateTime << 32) | created.dwLowDateTime,
            "exe": normalized_exe(buffer.value)}


def validate_state(state):
    if not isinstance(state, dict) or state.get("owner") != "cloud-web-probe" or state.get("schema") != 2:
        fail("process_mismatch")
    for key in ("pid", "created", "hwnd"):
        if type(state.get(key)) is not int or state[key] <= 0:
            fail("process_mismatch")
    if not isinstance(state.get("exe"), str) or normalized_exe(state["exe"]) != state["exe"]:
        fail("process_mismatch")
    if Path(state["exe"]).name.lower() != "msedge.exe":
        fail("process_mismatch")
    if not isinstance(state.get("token"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["token"]):
        fail("process_mismatch")
    profile = validate_profile(state.get("profile", ""))
    try:
        with (profile / ".probe-lock").open(encoding="ascii") as stream:
            token = stream.read(33)
    except OSError as exc:
        raise ProbeError("process_mismatch") from exc
    if token != state["token"]:
        fail("process_mismatch")
    return state


def window_info(hwnd, pid):
    user = _win32().user
    actual_pid = w.DWORD()
    name = ctypes.create_unicode_buffer(160)
    if not user.IsWindow(hwnd) or not user.GetWindowThreadProcessId(hwnd, ctypes.byref(actual_pid)):
        fail("window_missing")
    user.GetClassNameW(hwnd, name, len(name))
    if actual_pid.value != pid or name.value != WINDOW_CLASS or user.GetWindow(hwnd, 4):
        fail("process_mismatch")
    rect, client, origin = w.RECT(), w.RECT(), w.POINT(0, 0)
    if not (user.GetWindowRect(hwnd, ctypes.byref(rect))
            and user.GetClientRect(hwnd, ctypes.byref(client))
            and user.ClientToScreen(hwnd, ctypes.byref(origin))):
        fail("window_query_failed")
    return WindowInfo(hwnd, pid, (rect.left, rect.top, rect.right, rect.bottom),
                      (client.right - client.left, client.bottom - client.top),
                      (origin.x, origin.y), int(user.GetDpiForWindow(hwnd)))


@contextmanager
def owned_process(state, *, terminate=False):
    """查询、等待和终止使用同一个句柄，绝不通过 PID 重新打开待杀进程。"""
    validate_state(state)
    kernel = _win32().kernel
    handle = kernel.OpenProcess(0x1000 | 0x00100000 | (1 if terminate else 0), False, state["pid"])
    if not handle:
        fail("process_mismatch")
    try:
        if process_identity(handle) != {"created": state["created"], "exe": state["exe"]}:
            fail("process_mismatch")
        if kernel.WaitForSingleObject(handle, 0) != 0x102:
            fail("process_mismatch")
        window_info(state["hwnd"], state["pid"])
        yield handle
    finally:
        kernel.CloseHandle(handle)


def bound_window(state):
    physical_coordinates()
    with owned_process(state):
        return window_info(state["hwnd"], state["pid"])


def select_window(windows, *, pid):
    candidates = [window for window in windows if window.pid == pid]
    if len(candidates) != 1:
        fail("window_not_unique" if candidates else "window_missing")
    return candidates[0]


def enumerate_windows(pid):
    api, windows = _win32(), []
    def visit(hwnd, _):
        try:
            window = window_info(int(hwnd), pid)
            if api.user.IsWindowVisible(hwnd):
                windows.append(window)
        except ProbeError:
            pass
        return True
    if not api.user.EnumWindows(api.callback_type(visit), 0):
        fail("window_query_failed")
    return windows


def release_profile(state):
    lock = validate_profile(state["profile"]) / ".probe-lock"
    if lock.read_text(encoding="ascii") == state["token"]:
        lock.unlink()


def close_browser(state, timeout=4.0):
    api = _win32()
    with owned_process(state, terminate=True) as handle:
        # 发送前再次核对保存 HWND；句柄持有期间 PID 无法被其他进程复用。
        window_info(state["hwnd"], state["pid"])
        api.user.PostMessageW(state["hwnd"], 0x0010, 0, 0)
        status = api.kernel.WaitForSingleObject(handle, int(timeout * 1000))
        if status == 0x102:
            if process_identity(handle) != {"created": state["created"], "exe": state["exe"]}:
                fail("process_mismatch")
            if not api.kernel.TerminateProcess(handle, 1):
                return False
            status = api.kernel.WaitForSingleObject(handle, 3000)
        if status != 0:
            return False
    release_profile(state)
    return True


def reap_child(process):
    """只回收 Popen 自己持有的句柄，不用 PID 或标题寻找替代进程。"""
    if process.poll() is None:
        process.kill()
    process.wait(timeout=3)


def launch_browser(edge, profile, *, url, size, layout, cdp, keep_rendering, timeout=30):
    physical_coordinates()
    if not desktop_interactive():
        fail("desktop_unavailable")
    command = build_edge_command(edge, profile, url=url, size=size, layout=layout,
                                 cdp=cdp, keep_rendering=keep_rendering)
    profile.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    lock = profile / ".probe-lock"
    try:
        with lock.open("x", encoding="ascii") as stream:
            stream.write(token)
    except FileExistsError as exc:
        raise ProbeError("profile_in_use") from exc
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True)
        # Windows Popen 保留创建时的进程句柄，避免启动后查询 PID 的复用竞态。
        identity = process_identity(process._handle)
        if identity["exe"] != normalized_exe(edge):
            fail("process_mismatch")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                fail("profile_in_use" if process.returncode == 0 else "browser_exited")
            windows = enumerate_windows(process.pid)
            if len(windows) == 1:
                window = select_window(windows, pid=process.pid)
                state = {"schema": 2, "owner": "cloud-web-probe", "pid": process.pid,
                         "hwnd": window.hwnd, **identity, "profile": str(profile),
                         "token": token, "cdp": bool(cdp), "layout": layout}
                bound_window(state)
                return process, state
            time.sleep(0.1)
        fail("window_missing")
    except BaseException:
        reaped = process is None
        if process is not None:
            try:
                reap_child(process)
                reaped = True
            except BaseException:
                pass
        if reaped and lock.read_text(encoding="ascii") == token:
            lock.unlink()
        raise


# CDP 的唯一表达式：只读取固定字段，不注入监听器、不改变样式、焦点或页面事件。
LAYOUT_SCRIPT = r"""
(() => {
  if (location.href !== 'https://yh.wanmei.com/cloud/') return {target: false};
  const video = document.querySelector('video#gameVideo, #gameVideo video');
  const rect = video ? video.getBoundingClientRect() : null;
  const style = video ? getComputedStyle(video) : null;
  const clear = video && [0.1, 0.5, 0.9].every(x => [0.1, 0.5, 0.9].every(y =>
    document.elementFromPoint(rect.x + rect.width*x, rect.y + rect.height*y) === video));
  return {target: true, dpr: devicePixelRatio, inner: [innerWidth, innerHeight],
    video: video ? {rect: [rect.x, rect.y, rect.width, rect.height],
      width: video.videoWidth, height: video.videoHeight, readyState: video.readyState,
      paused: video.paused, currentTime: video.currentTime, clear: Boolean(clear),
      simple: style.transform === 'none' && style.objectPosition === '50% 50%' &&
        ['borderLeftWidth','borderTopWidth','borderRightWidth','borderBottomWidth',
         'paddingLeft','paddingTop','paddingRight','paddingBottom'].every(k => parseFloat(style[k]) === 0),
      visible: style.visibility === 'visible' && style.display !== 'none' && style.opacity === '1',
      objectFit: style.objectFit} : null};
})()
"""


def websocket_endpoint(value, port=None):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "ws" and parsed.hostname == "127.0.0.1"
                 and not parsed.username and not parsed.password and not parsed.query
                 and not parsed.fragment and parsed.port is not None
                 and (port is None or parsed.port == port)
                 and re.fullmatch(r"/devtools/page/[A-Za-z0-9-]+", parsed.path))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        fail("cdp_target_missing")
    return value


def parse_target_list(payload, port=None):
    if not isinstance(payload, (str, bytes)) or len(payload) > MAX_RESPONSE:
        fail("cdp_target_missing")
    try:
        targets = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise ProbeError("cdp_target_missing") from exc
    if not isinstance(targets, list):
        fail("cdp_target_missing")
    candidates = [target for target in targets if isinstance(target, dict)
                  and target.get("type") == "page" and target.get("url") == DEFAULT_URL]
    if len(candidates) != 1:
        fail("cdp_target_missing")
    return websocket_endpoint(candidates[0].get("webSocketDebuggerUrl"), port)


def read_devtools_port(profile):
    try:
        with (validate_profile(profile) / "DevToolsActivePort").open(encoding="ascii") as stream:
            value = stream.read(256)
        lines = value.splitlines()
        port = int(lines[0])
        if len(value) >= 256 or len(lines) != 2 or not 1 <= port <= 65535:
            fail("cdp_unavailable")
        if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", lines[1]):
            fail("cdp_unavailable")
        return port
    except (OSError, ValueError, IndexError) as exc:
        raise ProbeError("cdp_unavailable") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        fail("cdp_unavailable")


def _proxy_free_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


class CdpSession:
    def __init__(self, websocket_url, timeout=3.0):
        self.url = websocket_endpoint(websocket_url)
        self.timeout, self._ws = timeout, None
        self._next_id = 0

    def connect(self):
        try:
            from websockets.sync.client import ClientConnection, connect
            class NoRedirectConnection(ClientConnection):
                def handshake(self, *args, **kwargs):
                    try:
                        return super().handshake(*args, **kwargs)
                    except Exception:
                        # RuntimeError 不属于该库跟随重定向时捕获的 InvalidStatus。
                        raise RuntimeError("cdp_unavailable") from None
            self._ws = connect(self.url, proxy=None, open_timeout=self.timeout,
                               close_timeout=1, max_size=MAX_RESPONSE, max_queue=4,
                               create_connection=NoRedirectConnection)
        except Exception as exc:
            raise ProbeError("cdp_unavailable") from exc
        return self

    def layout(self):
        if self._ws is None:
            fail("cdp_unavailable")
        self._next_id += 1
        self._ws.send(json.dumps({"id": self._next_id, "method": "Runtime.evaluate",
                                 "params": {"expression": LAYOUT_SCRIPT, "returnByValue": True,
                                            "userGesture": False, "awaitPromise": False}}))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = self._ws.recv(timeout=max(0.01, deadline - time.monotonic()))
            if len(raw) > MAX_RESPONSE:
                fail("cdp_protocol_error")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("id") != self._next_id:
                continue
            result = value.get("result", {})
            if "error" in value or "exceptionDetails" in result:
                fail("cdp_protocol_error")
            return sanitize_layout(result.get("result", {}).get("value"))
        fail("cdp_protocol_error")

    def close(self):
        if self._ws is not None:
            self._ws.close()
            self._ws = None


def verify_cdp_listener(port, pid):
    """拒绝陈旧 DevToolsActivePort 指向其他进程或监听所有网卡的端口。"""
    if sys.platform != "win32":
        fail("windows_required")
    library = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_table = library.GetExtendedTcpTable
    get_table.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL,
                          w.ULONG, ctypes.c_int, w.ULONG]
    get_table.restype = w.DWORD
    size = w.DWORD()
    if get_table(None, ctypes.byref(size), False, 2, 3, 0) != 122 or not 4 <= size.value <= MAX_RESPONSE:
        fail("cdp_unavailable")
    buffer = ctypes.create_string_buffer(size.value)
    if get_table(buffer, ctypes.byref(size), False, 2, 3, 0) != 0:
        fail("cdp_unavailable")
    class Row(ctypes.Structure):
        _fields_ = [(name, w.DWORD) for name in ("state", "address", "port", "remote", "remote_port", "pid")]
    count = w.DWORD.from_buffer(buffer).value
    if 4 + count * ctypes.sizeof(Row) > size.value:
        fail("cdp_unavailable")
    matching = []
    for index in range(count):
        row = Row.from_buffer(buffer, 4 + index * ctypes.sizeof(Row))
        if socket.ntohs(row.port & 0xFFFF) == port:
            matching.append(row.pid == pid and row.address == 0x0100007F)
    if not matching or not all(matching):
        fail("cdp_unavailable")


def connect_cdp(profile, pid):
    port = read_devtools_port(profile)
    verify_cdp_listener(port, pid)
    endpoint = "http://127.0.0.1:%d/json/list" % port
    with _proxy_free_opener().open(endpoint, timeout=3) as response:
        if response.geturl() != endpoint:
            fail("cdp_unavailable")
        payload = response.read(MAX_RESPONSE + 1)
    return CdpSession(parse_target_list(payload, port)).connect()


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1000000


def sanitize_layout(value):
    """白名单复制，页面提供的任意字符串/URL/标题均不会进入报告。"""
    if not isinstance(value, dict) or value.get("target") is not True:
        fail("cdp_target_missing")
    dpr, inner = value.get("dpr"), value.get("inner")
    if not finite_number(dpr) or not 0 < dpr <= 8 or not isinstance(inner, list) or len(inner) != 2:
        fail("cdp_protocol_error")
    if not all(finite_number(v) and v > 0 for v in inner):
        fail("cdp_protocol_error")
    clean = {"target": True, "dpr": dpr, "inner": list(inner), "video": None}
    video = value.get("video")
    if video is None:
        return clean
    if not isinstance(video, dict):
        fail("cdp_protocol_error")
    rect = video.get("rect")
    if not isinstance(rect, list) or len(rect) != 4 or not all(map(finite_number, rect)):
        fail("cdp_protocol_error")
    result = {"rect": list(rect)}
    for key in ("width", "height", "readyState", "currentTime"):
        if not finite_number(video.get(key)) or video[key] < 0:
            fail("cdp_protocol_error")
        result[key] = video[key]
    for key in ("paused", "clear", "simple", "visible"):
        if type(video.get(key)) is not bool:
            fail("cdp_protocol_error")
        result[key] = video[key]
    fit = video.get("objectFit")
    result["objectFit"] = fit if fit in ("contain", "cover", "fill", "none", "scale-down") else "unknown"
    clean["video"] = result
    return clean


def viewport_offset(client_size, inner, dpr):
    """只有 CSS 视口物理尺寸与客户区一致时才确认原点；不猜标题栏高度。"""
    if not all(finite_number(v) and v > 0 for v in (*client_size, *inner, dpr)):
        return None
    if any(abs(client_size[i] - inner[i] * dpr) > 1 for i in (0, 1)):
        return None
    return 0, 0


def video_metrics(layout, client_size):
    unknown = {"mapping": "unknown", "content_rect_px": None, "covers_client": None,
               "video_ready": False, "is_16_9": None}
    if not layout or not layout.get("video"):
        return unknown
    video = layout["video"]
    dpr = layout["dpr"]
    if viewport_offset(client_size, layout["inner"], dpr) is None or not video["simple"]:
        return unknown
    x, y, width, height = video["rect"]
    sw, sh = video["width"], video["height"]
    if min(width, height, sw, sh) <= 0:
        return unknown
    fit = video["objectFit"]
    if fit in ("contain", "scale-down"):
        scale = min(width / sw, height / sh)
        if fit == "scale-down":
            scale = min(scale, 1)
        cw, ch = sw * scale, sh * scale
    elif fit in ("cover", "fill"):
        cw, ch = width, height
    else:
        return unknown
    left, top = (x + (width - cw) / 2) * dpr, (y + (height - ch) / 2) * dpr
    rect = [int(round(v)) for v in (left, top, cw * dpr, ch * dpr)]
    if rect[0] < 0 or rect[1] < 0 or rect[0] + rect[2] > client_size[0] or rect[1] + rect[3] > client_size[1]:
        return unknown
    return {"mapping": "client_physical", "content_rect_px": rect,
            "covers_client": rect == [0, 0, *client_size],
            "is_16_9": abs(cw / ch - 16 / 9) < 0.01,
            "content_aspect": round(cw / ch, 4), "stream_aspect": round(sw / sh, 4),
            "video_ready": video["readyState"] >= 2 and not video["paused"]
            and video["visible"] and video["clear"], "stream_size": [sw, sh]}


def _crop(frame, roi=None):
    if np is None:
        fail("empty_frame")
    array = np.asarray(frame)
    if array.ndim != 3 or array.shape[2] < 3 or not array.size or array.shape[0] * array.shape[1] > MAX_FRAME_PIXELS:
        fail("empty_frame")
    if array.dtype != np.uint8:
        fail("empty_frame")
    if roi is None:
        return array[:, :, :3]
    x, y, width, height = roi
    if any(type(v) is not int for v in roi) or min(x, y) < 0 or min(width, height) <= 0:
        fail("invalid_geometry")
    if x + width > array.shape[1] or y + height > array.shape[0]:
        fail("invalid_geometry")
    return array[y:y + height, x:x + width, :3]


def frame_stats(frame, roi=None):
    view = _crop(frame, roi)
    return {"shape": list(view.shape),
            "spatial_std": round(float(view.astype(np.float32).std(axis=(0, 1)).max()), 3),
            "nonblack_ratio": round(float((view.max(axis=2) > 16).mean()), 6)}


def frame_diff(first, second, roi=None):
    a, b = _crop(first, roi), _crop(second, roi)
    if a.shape != b.shape:
        return {"compatible": False, "changed_ratio": 0}
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return {"compatible": True, "changed_ratio": round(float((delta.max(axis=2) >= 8).mean()), 6)}


def capture_verdict(stats, diffs, *, video_ready=False, state_achieved=False):
    if not video_ready or not state_achieved or len(stats) < 3 or len(diffs) < 2:
        return "inconclusive"
    if any(item["nonblack_ratio"] < 0.02 or item["spatial_std"] < 1.5 for item in stats):
        return "inconclusive"
    if all(item["compatible"] and item["changed_ratio"] >= 0.001 for item in diffs):
        return "pass"
    return "inconclusive"


def observe_state(window):
    user = _win32().user
    minimized = bool(user.IsIconic(window.hwnd))
    visible = bool(user.IsWindowVisible(window.hwnd))
    foreground = int(user.GetForegroundWindow() or 0) == window.hwnd
    hits = []
    if visible and not minimized and min(window.client_size) > 0:
        for rx in (0.1, 0.5, 0.9):
            for ry in (0.1, 0.5, 0.9):
                point = w.POINT(window.client_origin[0] + int(window.client_size[0] * rx),
                                window.client_origin[1] + int(window.client_size[1] * ry))
                hit = user.WindowFromPoint(point)
                root = int(user.GetAncestor(hit, 2) or 0) if hit else 0
                hits.append(root == window.hwnd)
    return {"foreground": foreground, "minimized": minimized, "visible": visible,
            "sampled_uncovered": bool(hits) and all(hits),
            "sampled_occluded": bool(hits) and not any(hits)}


def state_achieved(requested, actual):
    if requested == "minimized":
        return actual["minimized"]
    if not actual["visible"] or actual["minimized"]:
        return False
    if requested == "foreground":
        return actual["foreground"] and actual["sampled_uncovered"]
    if requested == "background":
        return not actual["foreground"] and actual["sampled_uncovered"]
    return actual["sampled_occluded"]


@contextmanager
def window_state(state, requested):
    """只切换前台/最小化；后台及遮挡由真人摆放，未达成绝不冒充成功。"""
    window = bound_window(state)
    user = _win32().user
    was_minimized = bool(user.IsIconic(window.hwnd))
    try:
        if requested == "foreground":
            user.ShowWindow(window.hwnd, 9)
            user.SetForegroundWindow(window.hwnd)
        elif requested == "minimized":
            user.ShowWindow(window.hwnd, 6)
        time.sleep(0.2)
        yield
    finally:
        # 窗口可能在操作中销毁或被复用；恢复前必须重新核验身份。
        window = bound_window(state)
        if bool(user.IsIconic(window.hwnd)) != was_minimized:
            user.ShowWindow(window.hwnd, 6 if was_minimized else 9)


def load_framework():
    try:
        from maa.controller import Win32Controller
        from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
        from maa.tasker import Tasker
        from maa.define import LoggingLevelEnum
        # 空日志目录禁用文件日志；也显式禁用错误截图和调试图。
        if not all((Tasker.set_log_dir(""), Tasker.set_stdout_level(LoggingLevelEnum.Off),
                    Tasker.set_save_on_error(False), Tasker.set_save_draw(False),
                    Tasker.set_debug_mode(False))):
            fail("controller_connect_failed")
    except Exception as exc:
        raise ProbeError("controller_connect_failed") from exc
    return SimpleNamespace(Win32Controller=Win32Controller, Input=MaaWin32InputMethodEnum,
                           Screencap=MaaWin32ScreencapMethodEnum)


def wait_job(job, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = job.status
        if status.done:
            return job.wait()
        time.sleep(0.02)
    fail("operation_timeout")


def input_guard(state, expected, method, click=None):
    if not desktop_interactive():
        fail("desktop_unavailable")
    window = bound_window(state)
    if window != expected or min(window.client_size) <= 0 or _win32().user.IsIconic(window.hwnd):
        fail("input_unsafe")
    if click is not None:
        x, y = click
        if not (0 <= x < window.client_size[0] and 0 <= y < window.client_size[1]):
            fail("input_unsafe")
    if method == "Seize":
        user = _win32().user
        if int(user.GetForegroundWindow() or 0) != window.hwnd:
            fail("input_unsafe")
        if click is not None:
            point = w.POINT(window.client_origin[0] + click[0], window.client_origin[1] + click[1])
            hit = user.WindowFromPoint(point)
            if not hit or int(user.GetAncestor(hit, 2) or 0) != window.hwnd:
                fail("input_unsafe")
    return window


def native_worker(request):
    """仅 worker 调用 MaaFw；父进程不会持有原生控制器或其析构函数。"""
    state, operation = request["state"], request["operation"]
    window = bound_window(state)
    api = load_framework()
    if time.monotonic() >= request["expires"]:
        fail("operation_timeout")
    method = request["method"]
    is_input = operation == "input"
    if operation not in ("capture", "input"):
        fail("invalid_arguments")
    click, key = request.get("click"), request.get("key")
    if is_input:
        if request.get("allow_input") is not True or (click is None) == (key is None):
            fail("input_requires_allow_flag")
        if method not in (MOUSE_METHODS if click is not None else KEY_METHODS):
            fail("invalid_arguments")
        input_guard(state, window, method, click)
    elif method not in CAPTURE_METHODS:
        fail("invalid_arguments")
    controller = api.Win32Controller(
        window.hwnd, screencap_method=getattr(api.Screencap, "PrintWindow" if is_input else method),
        mouse_method=getattr(api.Input, method) if is_input and click is not None else api.Input.Null,
        keyboard_method=getattr(api.Input, method) if is_input and key is not None else api.Input.Null)
    _WORKER_KEEPALIVE.append(controller)
    if not controller.set_screenshot_use_raw_size(True):
        fail("invalid_geometry")
    if not wait_job(controller.post_connection()).succeeded:
        fail("controller_connect_failed")
    if is_input:
        # 连接或截图过期后决不能再派发输入。截图仅建立 MaaFw 原始坐标换算。
        if not wait_job(controller.post_screencap()).succeeded:
            fail("screencap_failed")
        frame = _crop(controller.cached_image)
        if tuple(frame.shape[:2][::-1]) != window.client_size:
            fail("invalid_geometry")
        input_guard(state, window, method, click)
        if time.monotonic() >= request["expires"] - 1:
            fail("operation_timeout")
        job = controller.post_click(*click) if click is not None else controller.post_click_key(key)
        ok = bool(wait_job(job).succeeded)
        return {"method": method, "native_success": ok, "effect": "unverified",
                "coordinate_unit": "client_physical_pixels", "executions": 1}
    cdp = None
    try:
        if state.get("cdp"):
            cdp = connect_cdp(state["profile"], state["pid"])
        layout_before = cdp.layout() if cdp else None
        metrics = video_metrics(layout_before, window.client_size)
        roi = metrics["content_rect_px"]
        stats, diffs, observations, previous = [], [], [], None
        for _ in range(3):
            current = bound_window(state)
            if current != window:
                fail("invalid_geometry")
            observations.append(observe_state(current))
            if not wait_job(controller.post_screencap()).succeeded:
                fail("screencap_failed")
            frame = _crop(controller.cached_image).copy()
            current = bound_window(state)
            if current != window:
                fail("invalid_geometry")
            observations.append(observe_state(current))
            if tuple(frame.shape[:2][::-1]) != window.client_size:
                roi, metrics["mapping"], metrics["video_ready"] = None, "unknown", False
                metrics["content_rect_px"], metrics["covers_client"] = None, None
            stats.append(frame_stats(frame, roi))
            if previous is not None:
                diffs.append(frame_diff(previous, frame, roi))
            previous = frame
            time.sleep(0.2)
        layout_after = cdp.layout() if cdp else None
        advanced = False
        if layout_before and layout_after and layout_before["video"] and layout_after["video"]:
            before, after = layout_before["video"], layout_after["video"]
            advanced = (after["currentTime"] > before["currentTime"]
                        and video_metrics(layout_after, window.client_size) == metrics
                        and after["rect"] == before["rect"]
                        and layout_before["inner"] == layout_after["inner"]
                        and layout_before["dpr"] == layout_after["dpr"])
        achieved = all(state_achieved(request["requested_state"], item) for item in observations)
        return {"actual": observations, "state_achieved": achieved, "video": metrics,
                "layout": layout_before, "media_time_advanced": advanced,
                "frames": stats, "differences": diffs,
                "verdict": capture_verdict(stats, diffs, video_ready=metrics["video_ready"] and advanced,
                                           state_achieved=achieved)}
    finally:
        if cdp:
            cdp.close()


def run_worker(request, timeout=WORKER_TIMEOUT):
    payload = dict(request, expires=time.monotonic() + timeout - 2)
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "_worker"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, close_fds=True)
    try:
        output, _ = process.communicate(json.dumps(payload).encode("utf-8"), timeout=timeout)
        if process.returncode != 0 or len(output) > MAX_RESPONSE:
            fail("worker_failed")
        value = json.loads(output)
        if not isinstance(value, dict):
            fail("worker_failed")
        if "error" in value:
            fail(value["error"] if value["error"] in ERROR_CODES else "worker_failed")
        return value
    except BaseException:
        reap_child(process)
        raise


def worker_entry():
    # 原生库 stdout/stderr 均静默；唯一输出为我们白名单构造的 JSON。
    output_fd = os.dup(1)
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    try:
        data = sys.stdin.buffer.read(65537)
        if len(data) > 65536:
            fail("invalid_arguments")
        result = native_worker(json.loads(data))
    except BaseException as exc:
        result = {"error": safe_reason(exc)}
    data = json.dumps(result, ensure_ascii=True, allow_nan=False).encode("utf-8")
    try:
        os.write(output_fd, data)
    finally:
        # 不走 Python/MaaFw 析构，阻塞的后台原生任务也随进程终结。
        os._exit(0)


def capture_matrix(state, *, methods, states):
    result = {}
    for requested in states:
        result[requested] = {}
        for method in methods:
            try:
                with window_state(state, requested):
                    result[requested][method] = run_worker({"state": state, "operation": "capture",
                                                          "method": method, "requested_state": requested})
            except Exception as exc:
                result[requested][method] = {"verdict": "inconclusive", "reason": safe_reason(exc),
                                             "state_achieved": False}
    return result


def validate_input_args(args):
    requested = args.click is not None or args.key is not None or args.input_method is not None
    if not args.allow_input:
        if requested:
            fail("input_requires_allow_flag")
        return False
    if (args.click is None) == (args.key is None) or not args.input_method:
        fail("invalid_arguments")
    if args.input_method not in (MOUSE_METHODS if args.click is not None else KEY_METHODS):
        fail("invalid_arguments")
    return True


def write_json(path, value):
    path = artifact_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_state(path):
    try:
        with artifact_path(path).open(encoding="utf-8") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            fail("state_missing")
        state = json.loads(data)
    except (OSError, ValueError) as exc:
        raise ProbeError("state_missing") from exc
    return validate_state(state)


def retire_state(path, state):
    """只删除与刚刚关闭的所有者 token 一致的状态文件。"""
    try:
        path = artifact_path(path)
        with path.open(encoding="utf-8") as stream:
            saved = json.loads(stream.read(65536))
        if isinstance(saved, dict) and saved.get("token") == state["token"]:
            path.unlink()
    except (OSError, ValueError):
        pass


def save_report(args, report):
    try:
        path = artifact_path(args.output) / uuid.uuid4().hex / "report.json"
        write_json(path, report)
        return {"report": str(path)}
    except BaseException:
        # 磁盘满/输出目录不可写时仍在 stdout 返回脱敏报告，不丢失失败与收尾信息。
        report["errors"].append("report_write_failed")
        return {"report": None, "inline_report": report}


def run_launch(args):
    process, state = None, None
    report = {"schema": 2, "command": "launch", "errors": [], "closed": None}
    try:
        artifact_path(args.state)
        artifact_path(args.output)
        if args.state.exists():
            fail("profile_in_use")
        process, state = launch_browser(find_edge(args.edge), validate_profile(args.profile),
                                        url=args.url, size=args.size, layout=args.layout, cdp=args.cdp,
                                        keep_rendering=args.keep_rendering, timeout=args.wait)
        write_json(args.state, state)
        report["cdp"] = state["cdp"]
        report["manual_login_required"] = True
        output = save_report(args, report)
        if report["errors"]:
            fail("report_write_failed")
        return 0, output
    except BaseException as exc:
        report["errors"].append(safe_reason(exc))
        if process is not None:
            try:
                if state is not None:
                    report["closed"] = close_browser(state)
                if not report["closed"]:
                    reap_child(process)
                    report["closed"] = True
            except BaseException:
                try:
                    reap_child(process)
                    report["closed"] = True
                except BaseException:
                    report["closed"] = False
            if report["closed"] and state:
                retire_state(args.state, state)
                try:
                    release_profile(state)
                except OSError:
                    pass
        return (130 if isinstance(exc, KeyboardInterrupt) else 1), save_report(args, report)


def run_probe(args):
    report = {"schema": 2, "command": "probe", "errors": [], "closed": False,
              "capture": {}, "input": {"enabled": False, "executions": 0},
              "coordinate_unit": "client_physical_pixels", "gate": "inconclusive"}
    state, code = None, 0
    try:
        state = _load_state(args.state)
        bound_window(state)
        allow = validate_input_args(args)
        report["capture"] = capture_matrix(state, methods=args.methods, states=args.states)
        if allow:
            report["input"] = run_worker({"state": state, "operation": "input", "allow_input": True,
                                          "method": args.input_method, "click": args.click, "key": args.key})
        values = [item for methods in report["capture"].values() for item in methods.values()]
        if any(item.get("verdict") == "pass" for item in values):
            report["gate"] = "capture_available"
        # 未取得视频证据不是“通过”，但不是工具异常；阶段 1 仍须人工批准。
    except BaseException as exc:
        report["errors"].append(safe_reason(exc))
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        if state is not None:
            try:
                report["closed"] = close_browser(state)
                if report["closed"]:
                    retire_state(args.state, state)
                if not report["closed"]:
                    report["errors"].append("process_mismatch")
                    code = code or 1
            except BaseException as exc:
                report["errors"].append(safe_reason(exc))
                code = code or 1
        output = save_report(args, report)
    return code or (1 if report["errors"] else 0), output


def run_close(args):
    state = _load_state(args.state)
    closed = close_browser(state)
    if closed:
        retire_state(args.state, state)
    return (0 if closed else 1), {"closed": closed}


def build_parser():
    parser = SafeParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    launch = commands.add_parser("launch")
    launch.add_argument("--edge")
    launch.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    launch.add_argument("--url", type=url_arg, default=DEFAULT_URL)
    launch.add_argument("--size", type=parse_size, default="1280x760")
    launch.add_argument("--layout", choices=("app", "kiosk"), default="app")
    launch.add_argument("--cdp", action="store_true")
    launch.add_argument("--no-keep-rendering", dest="keep_rendering", action="store_false")
    launch.add_argument("--wait", type=positive_timeout, default=30.0)
    launch.add_argument("--state", type=Path, default=DEFAULT_STATE)
    launch.add_argument("--output", type=Path, default=DEFAULT_REPORT_ROOT)
    probe = commands.add_parser("probe")
    probe.add_argument("--state", type=Path, default=DEFAULT_STATE)
    probe.add_argument("--output", type=Path, default=DEFAULT_REPORT_ROOT)
    probe.add_argument("--methods", type=csv_arg(CAPTURE_METHODS), default=",".join(CAPTURE_METHODS))
    probe.add_argument("--states", type=csv_arg(STATES), default=",".join(STATES),
                       help="后台/遮挡需真人预先摆放；未实际达成的状态记 inconclusive")
    probe.add_argument("--allow-input", action="store_true")
    action = probe.add_mutually_exclusive_group()
    action.add_argument("--click", type=parse_point, help="客户区物理像素 X,Y")
    action.add_argument("--key", type=parse_key)
    probe.add_argument("--input-method", choices=MOUSE_METHODS)
    close = commands.add_parser("close")
    close.add_argument("--state", type=Path, default=DEFAULT_STATE)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["_worker"]:
        worker_entry()
        return 1
    try:
        args = build_parser().parse_args(argv)
        handler = {"launch": run_launch, "probe": run_probe, "close": run_close}[args.command]
        code, output = handler(args)
        print(json.dumps(output, ensure_ascii=True, allow_nan=False))
        return code
    except SystemExit as exc:
        return int(exc.code)
    except BaseException as exc:
        print(json.dumps({"error": safe_reason(exc)}))
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
