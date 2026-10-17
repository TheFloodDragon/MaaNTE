"""云异环网页端启动器：专用浏览器 profile + 本机 CDP，供 MXU pretask 与 Agent 共用。

只使用独立的 cloud-web-profile，不触碰日常浏览器配置，也不读取 cookie 或凭据。
远程调试只监听 127.0.0.1 的随机端口；DevToolsActivePort 中的 GUID 必须与
/json/version 一致，且监听进程必须是浏览器本身，避免连到陈旧或他人的端口。
就绪只表示唯一的“异环云游戏”应用窗口与云页面已出现；登录、排队、串流由
Agent 任务（CloudGameWebStart）处理。本模块只依赖标准库，可在依赖安装前运行。
"""

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger("maante")
ROOT = Path(__file__).resolve().parents[1]
CLOUD_URL = "https://yh.wanmei.com/cloud/"
WINDOW_CLASS = "Chrome_WidgetWin_1"
WINDOW_TITLE = "异环云游戏"
PROFILE_NAME = "cloud-web-profile"
BROWSER_NAMES = ("msedge.exe", "chrome.exe")
_PROFILE_PATTERN = re.compile(r"cloud-web-profile(?:-[A-Za-z0-9_-]+)?", re.I)
_PORT_FILE = "DevToolsActivePort"
_LOCK_NAME = ".maante-web-launch.lock"
_MAX_HTTP = 256 * 1024
_ERROR_CODES = frozenset({
    "windows_required", "invalid_arguments", "invalid_timeout", "invalid_browser",
    "browser_missing", "invalid_profile", "profile_busy", "profile_lock_failed",
    "profile_in_use", "cdp_unverified", "stopped", "browser_exited",
    "browser_start_timeout", "window_query_failed", "ambiguous_window",
    "window_missing", "page_missing", "cdp_failed", "cdp_timeout", "script_failed",
    "frame_unavailable",
})
_HINTS = {
    "profile_in_use": "专用浏览器正以非调试模式运行，请关闭“异环云游戏”窗口后重试",
    "browser_missing": "未找到 Edge 或 Chrome，请安装浏览器，或用 --browser 指定 msedge.exe/chrome.exe",
    "cdp_unverified": "专用浏览器调试端口未通过校验，请关闭“异环云游戏”窗口后重试",
    "window_missing": "专用浏览器已运行但未找到“异环云游戏”窗口，请关闭该浏览器后重试",
    "page_missing": "专用浏览器未打开云异环页面，请关闭该浏览器后重试",
    "browser_start_timeout": "浏览器启动超时，请检查网络后重试",
    "profile_busy": "另一个云异环启动流程正在进行，请稍后再试",
}
_api = None


class WebClientError(RuntimeError):
    """只携带固定错误码，不携带本机路径、命令行或账号信息。"""


@dataclass(frozen=True)
class Endpoint:
    """已校验的本机 CDP 端点：监听进程即浏览器主进程。"""

    port: int
    pid: int
    exe: str
    browser_path: str


@dataclass(frozen=True)
class BrowserState:
    endpoint: Endpoint
    hwnd: int
    client_size: tuple[int, int]
    launched: bool


def safe_reason(exc):
    if isinstance(exc, WebClientError):
        if len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in _ERROR_CODES:
            return exc.args[0]
        return "web_client_error"
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "process_wait_timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, OSError):
        return "os_error"
    return "unexpected_error"


def _log_failure(stage, exc, started):
    reason = safe_reason(exc)
    log = logger.warning if reason in ("stopped", "interrupted") else logger.error
    log("网页云启动终止 | stage=%s | reason=%s | elapsed=%.3fs", stage, reason, time.monotonic() - started)
    if reason in _HINTS:
        logger.warning("%s | reason=%s", _HINTS[reason], reason)


def _normalize(path):
    return os.path.normcase(os.path.normpath(str(path)))


def default_profile():
    """开发与发行目录均放在 <根目录>/cache 下，不进入配置备份或调试日志打包。"""
    return ROOT / "cache" / PROFILE_NAME


def validate_profile(value=None):
    """只接受独立命名的绝对目录，拒绝任何指向浏览器日常 User Data 的路径。"""
    if value is None:
        value = default_profile()
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise WebClientError("invalid_profile")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WebClientError("invalid_profile")
    resolved = path.resolve()
    for candidate in (path, resolved):
        if not _PROFILE_PATTERN.fullmatch(candidate.name) or any(
            part.casefold() == "user data" for part in candidate.parts
        ):
            raise WebClientError("invalid_profile")
    if resolved.exists() and not resolved.is_dir():
        raise WebClientError("invalid_profile")
    return resolved


def _browser_candidates():
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            yield Path(base) / "Microsoft/Edge/Application/msedge.exe"
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            yield Path(base) / "Google/Chrome/Application/chrome.exe"


def find_browser(value=None):
    """优先使用参数或 MAANTE_CLOUD_BROWSER，否则按 Edge → Chrome 查找；不下载浏览器。"""
    value = value or os.environ.get("MAANTE_CLOUD_BROWSER") or None
    if value is not None:
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            raise WebClientError("invalid_browser")
        path = Path(value)
        if not path.is_absolute() or path.name.lower() not in BROWSER_NAMES:
            raise WebClientError("invalid_browser")
        if not path.is_file():
            raise WebClientError("browser_missing")
        return path.resolve()
    for path in _browser_candidates():
        if path.is_absolute() and path.is_file():
            return path.resolve()
    raise WebClientError("browser_missing")


def build_browser_command(browser, profile):
    """应用模式 + 本机随机调试端口；不添加自动化标记，避免触发网页人机校验。"""
    return [
        str(browser), "--user-data-dir=%s" % profile,
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--disable-background-mode", "--edge-skip-compat-layer-relaunch",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows", "--lang=zh-CN",
        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
        "--window-size=1280,760", "--app=%s" % CLOUD_URL,
    ]


def _apis():
    global _api
    if sys.platform != "win32":
        raise WebClientError("windows_required")
    if _api is not None:
        return _api
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    pointer = ctypes.POINTER
    for lib, name, args, result in (
        (user, "EnumWindows", [callback, w.LPARAM], w.BOOL),
        (user, "IsWindow", [w.HWND], w.BOOL),
        (user, "IsWindowVisible", [w.HWND], w.BOOL),
        (user, "IsIconic", [w.HWND], w.BOOL),
        (user, "GetWindow", [w.HWND, w.UINT], w.HWND),
        (user, "GetAncestor", [w.HWND, w.UINT], w.HWND),
        (user, "GetWindowThreadProcessId", [w.HWND, pointer(w.DWORD)], w.DWORD),
        (user, "GetWindowTextW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        (user, "GetClassNameW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        (user, "GetClientRect", [w.HWND, pointer(w.RECT)], w.BOOL),
        (user, "SendMessageTimeoutW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT,
                                       pointer(ctypes.c_size_t)], w.LPARAM),
        (kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        (kernel, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, pointer(w.DWORD)], w.BOOL),
        (kernel, "CloseHandle", [w.HANDLE], w.BOOL),
        (kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        (iphlp, "GetExtendedTcpTable", [ctypes.c_void_p, pointer(w.DWORD), w.BOOL, w.ULONG,
                                        ctypes.c_int, w.ULONG], w.DWORD),
    ):
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result
    _api = SimpleNamespace(user=user, kernel=kernel, iphlp=iphlp, callback=callback)
    return _api


def process_exe(pid):
    """只读查询进程映像路径；查询失败返回 None，调用方必须按未知处理。"""
    kernel = _apis().kernel
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        text, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)):
            return None
        return _normalize(text.value) if Path(text.value).is_absolute() else None
    finally:
        kernel.CloseHandle(handle)


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, w.DWORD) for name in ("state", "address", "port", "remote", "remote_port", "pid")]


def listener_pids(port):
    """返回监听指定端口的 (地址, PID)；只看 IPv4 LISTEN 行（TCP_TABLE_OWNER_PID_LISTENER）。"""
    get_table = _apis().iphlp.GetExtendedTcpTable
    size = w.DWORD()
    if get_table(None, ctypes.byref(size), False, 2, 3, 0) != 122 or not 4 <= size.value <= 4 * 1024 * 1024:
        raise WebClientError("cdp_unverified")
    buffer = ctypes.create_string_buffer(size.value)
    if get_table(buffer, ctypes.byref(size), False, 2, 3, 0) != 0:
        raise WebClientError("cdp_unverified")
    count = w.DWORD.from_buffer(buffer).value
    if 4 + count * ctypes.sizeof(_TcpRow) > size.value:
        raise WebClientError("cdp_unverified")
    rows = []
    for index in range(count):
        row = _TcpRow.from_buffer(buffer, 4 + index * ctypes.sizeof(_TcpRow))
        if socket.ntohs(row.port & 0xFFFF) == port:
            rows.append((row.address, row.pid))
    return rows


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise WebClientError("cdp_unverified")


def http_json(port, path, timeout=2.0):
    """直连本机端口，禁用系统代理与重定向，并限制响应大小。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    url = "http://127.0.0.1:%d%s" % (port, path)
    with opener.open(url, timeout=timeout) as response:
        payload = response.read(_MAX_HTTP + 1)
    if len(payload) > _MAX_HTTP:
        raise WebClientError("cdp_unverified")
    return json.loads(payload)


def read_port_file(profile):
    """返回 (port, browser_path)；文件缺失返回 None，格式异常抛 ValueError。"""
    try:
        raw = (profile / _PORT_FILE).read_text(encoding="ascii", errors="strict")
    except FileNotFoundError:
        return None
    lines = raw.splitlines()
    if len(raw) > 256 or len(lines) != 2:
        raise ValueError("port_file")
    port = int(lines[0])
    if not 1 <= port <= 65535 or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", lines[1]):
        raise ValueError("port_file")
    return port, lines[1]


def discover(profile):
    """校验专用 profile 当前的 CDP 端点，返回 (Endpoint|None, reason)。

    reason：ok / no_port_file / stale / invalid_port_file（三者可重新启动）、
    not_loopback / ambiguous_listener / not_browser / guid_mismatch（端口疑似被
    其他程序占用，禁止启动或连接）。
    """
    try:
        parsed = read_port_file(profile)
    except (OSError, ValueError, UnicodeError):
        return None, "invalid_port_file"
    if parsed is None:
        return None, "no_port_file"
    port, browser_path = parsed
    rows = listener_pids(port)
    if not rows:
        return None, "stale"
    if any(address != 0x0100007F for address, _ in rows):  # 只允许 127.0.0.1
        return None, "not_loopback"
    pids = {pid for _, pid in rows}
    if len(pids) != 1:
        return None, "ambiguous_listener"
    pid = pids.pop()
    exe = process_exe(pid)
    if exe is None or Path(exe).name.lower() not in BROWSER_NAMES:
        return None, "not_browser"
    try:
        version = http_json(port, "/json/version")
        socket_url = urlsplit(version.get("webSocketDebuggerUrl", "")) if isinstance(version, dict) else None
    except (OSError, ValueError, WebClientError):
        return None, "guid_mismatch"
    if (socket_url is None or socket_url.scheme != "ws" or socket_url.hostname != "127.0.0.1"
            or socket_url.port != port or socket_url.path != browser_path):
        return None, "guid_mismatch"
    return Endpoint(port=port, pid=pid, exe=exe, browser_path=browser_path), "ok"


def is_cloud_url(url):
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        return False
    return parts.scheme == "https" and parts.hostname == "yh.wanmei.com" and parts.path.startswith("/cloud")


def page_targets(endpoint, cloud_only=True):
    """列出 page 类型目标的 id 与 webSocketDebuggerUrl；不读取页面标题或内容。"""
    targets = http_json(endpoint.port, "/json/list")
    if not isinstance(targets, list):
        raise WebClientError("cdp_unverified")
    result = []
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        if cloud_only and not is_cloud_url(target.get("url", "")):
            continue
        socket_url = target.get("webSocketDebuggerUrl", "")
        parts = urlsplit(socket_url)
        if parts.scheme != "ws" or parts.hostname != "127.0.0.1" or parts.port != endpoint.port \
                or not re.fullmatch(r"/devtools/page/[A-Za-z0-9-]+", parts.path):
            continue
        result.append({"id": str(target.get("id", "")), "ws": socket_url, "cloud": is_cloud_url(target.get("url", ""))})
    return result


def _safe_targets(endpoint):
    try:
        return page_targets(endpoint)
    except (OSError, ValueError, WebClientError):
        return []


def browser_windows(pid):
    """枚举浏览器进程中可见、无 owner、标题恰为“异环云游戏”的根窗口（含最小化）。"""
    api = _apis()
    found, errors = [], []

    @api.callback
    def visit(hwnd, _unused):
        try:
            owner = w.DWORD()
            user = api.user
            if not user.IsWindowVisible(hwnd) or not user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)):
                return True
            if owner.value != pid or user.GetWindow(hwnd, 4) or user.GetAncestor(hwnd, 2) != hwnd:
                return True
            name, title = ctypes.create_unicode_buffer(128), ctypes.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, name, len(name))
            user.GetWindowTextW(hwnd, title, len(title))
            if name.value != WINDOW_CLASS or title.value.strip() != WINDOW_TITLE:
                return True
            rect = w.RECT()
            user.GetClientRect(hwnd, ctypes.byref(rect))
            found.append((int(hwnd), (rect.right - rect.left, rect.bottom - rect.top), bool(user.IsIconic(hwnd))))
        except Exception as exc:  # ctypes 回调内不能向外传播异常
            errors.append(exc)
        return True

    if not api.user.EnumWindows(visit, 0) or errors:
        raise WebClientError("window_query_failed")
    return found


def window_responding(hwnd):
    result = ctypes.c_size_t()
    # WM_NULL + SMTO_ABORTIFHUNG：只探测响应，不发送任何用户输入。
    return bool(_apis().user.SendMessageTimeoutW(hwnd, 0, 0, 0, 2, 200, ctypes.byref(result)))


@contextmanager
def _profile_guard(profile):
    """零共享文件句柄串行化 pretask 与 Agent 的并发启动；进程退出时系统自动释放。"""
    profile.mkdir(parents=True, exist_ok=True)
    kernel = _apis().kernel
    handle = kernel.CreateFileW(str(profile / _LOCK_NAME), 0xC0000000, 0, None, 4, 0x80, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise WebClientError("profile_busy" if ctypes.get_last_error() in (32, 33) else "profile_lock_failed")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _wait_guard(profile, deadline, stopped):
    """另一个启动流程持锁时等待其完成，而不是并发启动第二个浏览器。"""
    while True:
        try:
            with _profile_guard(profile):
                yield
            return
        except WebClientError as exc:
            if exc.args[0] != "profile_busy" or time.monotonic() >= deadline:
                raise
        if stopped():
            raise WebClientError("stopped")
        time.sleep(0.2)


def _validate_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 < value <= 600:
        raise WebClientError("invalid_timeout")
    return float(value)


def _ready_window(endpoint):
    """返回 (BrowserState 所需窗口, 缺失原因)；多个窗口直接拒绝，不猜测。"""
    windows = browser_windows(endpoint.pid)
    if len(windows) > 1:
        raise WebClientError("ambiguous_window")
    if not windows:
        return None, "window_missing"
    hwnd, size, _iconic = windows[0]
    if not window_responding(hwnd):
        return None, "window_missing"
    if not _safe_targets(endpoint):
        return None, "page_missing"
    return (hwnd, size), None


def start_browser(browser=None, profile=None, timeout=90, stopped=lambda: False):
    """复用或启动专用浏览器，直到云页面与应用窗口就绪；返回 BrowserState。

    只在本次启动且从未出现 CDP 端点时清理自己拉起的进程；已就绪或复用的浏览器
    在任何失败路径都保留，避免打断用户正在进行的登录或排队。
    """
    started, stage, process, endpoint = time.monotonic(), "start.validate", None, None
    try:
        if sys.platform != "win32":
            raise WebClientError("windows_required")
        deadline = started + _validate_timeout(timeout)
        profile = validate_profile(profile)
        stage = "start.lock"
        with _wait_guard(profile, deadline, stopped):
            stage = "start.discover"
            endpoint, reason = discover(profile)
            if endpoint is not None:
                logger.info("复用已运行的专用浏览器 | stage=start.reuse | pid=%d | port=%d", endpoint.pid, endpoint.port)
            elif reason in ("no_port_file", "stale", "invalid_port_file"):
                path = find_browser(browser)
                if reason != "no_port_file":
                    logger.debug("清理陈旧的调试端口记录 | stage=start.discover | reason=%s", reason)
                    (profile / _PORT_FILE).unlink(missing_ok=True)
                stage = "start.launch"
                process = subprocess.Popen(
                    build_browser_command(path, profile), cwd=str(path.parent), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, shell=False,
                )
                logger.info("正在启动 %s 浏览器 | stage=start.launch | pid=%d", Path(path).stem, process.pid)
            else:
                logger.debug("调试端口校验未通过 | stage=start.discover | reason=%s", reason)
                raise WebClientError("cdp_unverified")
            exited_at, last_wait = None, None
            while time.monotonic() < deadline:
                if stopped():
                    raise WebClientError("stopped")
                if endpoint is None:
                    stage = "start.cdp"
                    code = process.poll()
                    if code is not None and exited_at is None:
                        exited_at = time.monotonic()
                        logger.debug("启动进程已退出，等待浏览器接管 | stage=start.cdp | returncode=%d", code)
                    endpoint, reason = discover(profile)
                    if endpoint is None:
                        if reason not in ("no_port_file", "stale", "invalid_port_file"):
                            raise WebClientError("cdp_unverified")
                        if exited_at is not None and time.monotonic() - exited_at > 5:
                            # 退出码 0 表示交给了已在运行、但未开调试端口的同 profile 浏览器。
                            raise WebClientError("profile_in_use" if code == 0 else "browser_exited")
                        time.sleep(0.1)
                        continue
                    logger.info("浏览器调试端口已就绪 | stage=start.cdp | port=%d | elapsed=%.3fs",
                                endpoint.port, time.monotonic() - started)
                stage = "start.window"
                window, missing = _ready_window(endpoint)
                if window is not None:
                    state = BrowserState(endpoint, window[0], window[1], process is not None)
                    logger.info("云异环网页窗口已就绪 | stage=start.ready | client=%dx%d | launched=%s | elapsed=%.3fs",
                                state.client_size[0], state.client_size[1], state.launched, time.monotonic() - started)
                    return state
                if missing != last_wait:
                    logger.debug("等待云异环网页窗口 | stage=start.window | reason=%s", missing)
                    last_wait = missing
                time.sleep(0.2)
            raise WebClientError(last_wait or "browser_start_timeout")
    except BaseException as exc:
        _log_failure(stage, exc, started)
        if process is not None and endpoint is None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
                logger.info("已清理本次启动但未就绪的浏览器 | stage=cleanup.done")
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                logger.warning("浏览器清理未完成 | stage=cleanup.failed | reason=%s", safe_reason(cleanup_error))
        raise


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise WebClientError("invalid_arguments")


def build_parser():
    parser = _SafeParser(description="打开云异环网页并等待窗口就绪（MXU pretask / 命令行）", allow_abbrev=False)
    parser.add_argument("--browser", help="可选：msedge.exe 或 chrome.exe 的绝对路径")
    parser.add_argument("--profile", help="可选：cloud-web-profile[-后缀] 专用目录（绝对路径）")
    parser.add_argument("--timeout", type=float, default=90, help="等待秒数（默认 90，最大 600）")
    parser.add_argument("options", nargs="?", help="MXU pretask 追加的选项 JSON（当前不使用）")
    return parser


def _setup_cli_logging():
    """独立运行时输出到标准流；pythonw 无标准流时改写 debug/cloud_web_start.log。"""
    if logging.getLogger().handlers or logger.handlers:
        return
    stream = sys.stdout if sys.stdout is not None else None
    if stream is not None:
        handler = logging.StreamHandler(stream)
    else:
        (ROOT / "debug").mkdir(exist_ok=True)
        handler = logging.FileHandler(ROOT / "debug" / "cloud_web_start.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def main(argv=None):
    started, stage = time.monotonic(), "cli.arguments"
    try:
        args = build_parser().parse_args(argv)
        stage = "cli.browser"
        logger.info("开始打开云异环网页 | stage=cli.begin")
        state = start_browser(args.browser, args.profile, args.timeout)
        logger.info("云异环网页启动入口完成，浏览器保持运行 | stage=cli.ready | launched=%s | elapsed=%.3fs",
                    state.launched, time.monotonic() - started)
        return 0
    except KeyboardInterrupt:
        return 130
    except WebClientError as exc:
        if stage == "cli.arguments":
            _log_failure(stage, exc, started)
            return 2
        return 1
    except Exception as exc:
        _log_failure(stage, exc, started)
        return 1


if __name__ == "__main__":
    _setup_cli_logging()
    raise SystemExit(main())
