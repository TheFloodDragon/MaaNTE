"""云异环客户端进程启动与窗口就绪等待。

安全边界（重要）：
- 只启动**云异环自身**的可执行文件。候选来源限定为 ``locator`` 的自动探测
  结果，或用户在任务选项里显式填写的路径。
- 用户填写的路径同样要过 ``locator.validate_user_path`` 校验：必须存在、
  必须是 ``.exe``、文件名必须落在已知的云异环启动器白名单内。
  这样即使配置被误填或被篡改，也不会变成"任意进程启动器"。
- 不传 ``shell=True``，参数以列表形式传递，避免命令注入。
- 不读取、不存储、不传递任何账号凭据（登录由云客户端自己完成）。

本模块只负责"把客户端拉起来，并确认窗口出现"。登录点击与排队等待由
Pipeline 节点负责，因为那些依赖界面识别。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .locator import (
    TRUSTED_LAUNCHER_NAMES,
    LauncherCandidate,
    describe_failure,
    resolve_launcher,
)

try:
    from utils.win32_process import find_windows_by_process, get_pids_by_name
except ImportError:  # pragma: no cover - 非 Windows 或独立测试
    find_windows_by_process = None
    get_pids_by_name = None


# 云异环窗口类名（与 interface.json 的 CloudGame-Front 保持一致：class_regex "Qt.*"）。
#
# 必须是**编译好的正则对象**，不能写成字符串 "Qt"：
# ``utils.win32_process._match_class_name`` 对字符串做的是精确相等比较，
# 只有非字符串的 pattern 才走 ``re.search``。写成 "Qt" 时永远匹配不到真实
# 类名（实机为 ``Qt51517QWindowOwnDC``），窗口明明已经出现却一直找不到，
# 表现为 CloudGameLaunch 必然卡到「等待窗口超时（120s）」。
CLOUD_WINDOW_CLASS = (re.compile(r"^Qt"),)

DEFAULT_WINDOW_TIMEOUT = 120.0
DEFAULT_POLL_INTERVAL = 1.0
MIN_WINDOW_SIDE = 200


@dataclass
class LaunchResult:
    """启动结果。"""

    started: bool
    already_running: bool
    exe_path: Path | None
    source: str
    hwnd: int | None
    message: str


def restore_if_minimized(hwnd: int | None, logger=None) -> bool:
    """窗口最小化时恢复它；返回是否执行了恢复。

    这是一个实测出来的静默盲区：窗口最小化时 ``post_screencap().wait()``
    **仍然返回成功**，但截到的画面是全白的（实测 mean=255 / std=0），
    而且尺寸会变成异常值（实测 3317x720 而非 1280x720）。
    识别拿着白图必然全部不命中，表现为「任务一直等到超时」，
    日志里却看不出任何异常。

    三月七工具箱在云游戏截图前也做同样的事
    （``_ensure_window_not_minimized_for_frame_capture``），原因一致：
    最小化后画面停止渲染。
    """
    if not hwnd or sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        if not user32.IsIconic(int(hwnd)):
            return False
        _SW_RESTORE = 9
        user32.ShowWindow(int(hwnd), _SW_RESTORE)
        if logger is not None:
            logger("云异环窗口处于最小化，已恢复（最小化时截图为全白，识别必然失败）")
        time.sleep(1.0)
        return True
    except Exception as exc:
        if logger is not None:
            logger(f"恢复云异环窗口失败: {exc}")
        return False


def _running_pids(names: tuple[str, ...]) -> list[int]:
    """返回云异环相关进程的 PID 列表。"""
    if get_pids_by_name is None:
        return []
    pids: list[int] = []
    for name in names:
        try:
            pids.extend(get_pids_by_name(name) or [])
        except Exception:
            continue
    return pids


def _find_cloud_window(names: tuple[str, ...]):
    """在云异环进程里找一个尺寸合理的可见窗口。"""
    if find_windows_by_process is None:
        return None
    for name in names:
        try:
            windows = find_windows_by_process(
                name,
                hwnd_class=CLOUD_WINDOW_CLASS,
                require_title=True,
            )
        except Exception:
            continue
        for item in windows or []:
            size = item.get("client_size") or (0, 0)
            if size[0] >= MIN_WINDOW_SIDE and size[1] >= MIN_WINDOW_SIDE:
                return item
    return None


def resolve_executable(user_path: str | None) -> tuple[Path | None, str, str]:
    """决定要启动哪个可执行文件。

    返回 ``(路径, 来源说明, 失败原因)``。

    分工明确，且**不做静默回退**：

    - 用户填了路径 → 只认这个路径（可以是 exe，也可以是安装目录）。
      校验不通过就直接失败，绝不偷偷改用自动探测到的其他客户端。
      否则用户填错了却"看起来能跑"，反而更难排查。
    - 用户没填 → 走自动探测。

    无论走哪条路径，最终都由 ``locator`` 的白名单校验把关，
    因此这里不可能返回一个非云异环的可执行文件。
    """
    has_user_path = bool(user_path and str(user_path).strip())

    candidate: LauncherCandidate | None = resolve_launcher(user_path)

    if has_user_path:
        # 显式配置必须自己命中；命中的候选也必须确实来自用户输入。
        if candidate is None or not candidate.source.startswith("用户指定"):
            return None, "", describe_failure(user_path)
        return candidate.path, candidate.source, ""

    if candidate is None:
        return None, "", describe_failure(None)
    return candidate.path, candidate.source, ""


def launch_cloud_game(
    user_path: str | None = None,
    window_timeout: float = DEFAULT_WINDOW_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    should_stop=None,
    logger=None,
    clock=time.monotonic,
    sleeper=time.sleep,
    spawn=None,
    process_names: tuple[str, ...] | None = None,
) -> LaunchResult:
    """启动云异环并等待其窗口出现。

    已在运行时不重复启动，直接等窗口。``spawn`` 与时钟可注入，便于离线测试。
    """
    names = process_names or TRUSTED_LAUNCHER_NAMES

    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    # 已在运行：不重复拉起，避免出现多个客户端争抢串流。
    existing = _find_cloud_window(names)
    if existing is not None:
        return LaunchResult(
            started=False,
            already_running=True,
            exe_path=None,
            source="",
            hwnd=int(existing["hwnd"]),
            message="云异环窗口已存在，跳过启动",
        )

    already = bool(_running_pids(names))

    exe_path = None
    source = ""
    if not already:
        exe_path, source, reason = resolve_executable(user_path)
        if exe_path is None:
            return LaunchResult(
                started=False,
                already_running=False,
                exe_path=None,
                source=source,
                hwnd=None,
                message=reason,
            )
        log(f"启动云异环: {exe_path}（{source}）")
        try:
            if spawn is not None:
                spawn(exe_path)
            else:
                # 不用 shell，参数走列表，cwd 设为安装目录（客户端常依赖相对路径）
                subprocess.Popen(
                    [str(exe_path)],
                    cwd=str(exe_path.parent),
                    shell=False,
                )
        except Exception as exc:
            return LaunchResult(
                started=False,
                already_running=False,
                exe_path=exe_path,
                source=source,
                hwnd=None,
                message=f"启动失败: {exc}",
            )
    else:
        log("云异环进程已在运行，等待窗口出现")

    deadline = clock() + max(float(window_timeout), 0.0)
    while clock() < deadline:
        if should_stop is not None and should_stop():
            return LaunchResult(
                started=True,
                already_running=already,
                exe_path=exe_path,
                source=source,
                hwnd=None,
                message="等待窗口时任务被停止",
            )
        window = _find_cloud_window(names)
        if window is not None:
            size = window.get("client_size") or (0, 0)
            log(f"云异环窗口已出现: {size[0]}x{size[1]}")
            return LaunchResult(
                started=True,
                already_running=already,
                exe_path=exe_path,
                source=source,
                hwnd=int(window["hwnd"]),
                message="窗口已就绪",
            )
        sleeper(max(float(poll_interval), 0.05))

    return LaunchResult(
        started=True,
        already_running=already,
        exe_path=exe_path,
        source=source,
        hwnd=None,
        message=f"等待窗口超时（{window_timeout:.0f}s）",
    )


__all__ = [
    "CLOUD_WINDOW_CLASS",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_WINDOW_TIMEOUT",
    "LaunchResult",
    "launch_cloud_game",
    "resolve_executable",
    "restore_if_minimized",
]
