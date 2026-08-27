"""云异环客户端安装位置探测。

按优先级解析云客户端启动器路径：

1. 用户显式指定（``custom_action_param`` 的 ``launcher_path``）——最高优先级，
   便于自定义安装位置或绿色版；
2. Windows 注册表卸载表（``Uninstall`` 键的 ``InstallLocation`` /
   ``DisplayIcon``）——本机实测可命中；
3. 常见安装目录逐盘扫描——注册表被清理时兜底。

安全约束：本模块只负责"找到路径"，不负责启动。返回的候选一律经过
:func:`is_trusted_launcher` 校验——文件必须真实存在、后缀为 ``.exe``、
且文件名在已知启动器白名单内。这样即使注册表被污染或用户填错，也不会
把任意可执行文件交给下游启动。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# 已知的云异环启动器文件名（小写比较）。
# NTECloudLauncher 是安装根目录的启动器，NTECloudGame 是实际客户端主程序。
TRUSTED_LAUNCHER_NAMES = (
    "ntecloudlauncher.exe",
    "ntecloudgame.exe",
)

# 注册表卸载表中云异环的显示名特征（小写子串匹配）。
# 本机实测 DisplayName 为「云异环」。
REGISTRY_NAME_HINTS = ("云异环",)

# 注册表卸载表根键。同时查 64 位与 32 位视图。
_UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)

# 常见安装目录名（相对各盘根）。注册表不可用时兜底。
_COMMON_DIR_NAMES = (
    "NevernessToEvernessCloudGame",
    "Neverness To Everness Cloud Game",
)

# 安装根目录下可能存放主程序的子目录。
_SUBDIR_CANDIDATES = ("", "NTECloud")


@dataclass(frozen=True)
class LauncherCandidate:
    """一个通过校验的启动器候选。"""

    path: Path
    source: str

    @property
    def display(self) -> str:
        return f"{self.path} (来源: {self.source})"


def is_trusted_launcher(path: str | os.PathLike[str] | None) -> bool:
    """校验路径是否为可信的云异环启动器。

    要求同时满足：真实存在的文件、``.exe`` 后缀、文件名在白名单内。
    白名单是防止把任意可执行文件当作启动器拉起的关键防线。
    """
    if not path:
        return False
    try:
        resolved = Path(path).expanduser()
    except (OSError, ValueError, RuntimeError):
        return False
    try:
        if not resolved.is_file():
            return False
    except OSError:
        return False
    if resolved.suffix.lower() != ".exe":
        return False
    return resolved.name.lower() in TRUSTED_LAUNCHER_NAMES


def _iter_registry_locations():
    """从注册表卸载表读出云异环的安装位置与启动器路径。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import winreg
    except ImportError:
        return

    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        for root_path in _UNINSTALL_KEYS:
            try:
                root = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    root_path,
                    0,
                    winreg.KEY_READ | view,
                )
            except OSError:
                continue
            try:
                index = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(
                            root, sub_name, 0, winreg.KEY_READ | view
                        ) as sub:
                            display = _read_reg_value(sub, "DisplayName")
                            if not display:
                                continue
                            lowered = display.lower()
                            if not any(
                                hint.lower() in lowered
                                for hint in REGISTRY_NAME_HINTS
                            ):
                                continue
                            yield (
                                _read_reg_value(sub, "InstallLocation"),
                                _read_reg_value(sub, "DisplayIcon"),
                            )
                    except OSError:
                        continue
            finally:
                try:
                    winreg.CloseKey(root)
                except OSError:
                    pass


def _read_reg_value(key, name: str) -> str:
    """读注册表字符串值，失败返回空串。"""
    try:
        import winreg

        value, _kind = winreg.QueryValueEx(key, name)
    except (OSError, ImportError, ValueError):
        return ""
    return str(value).strip() if value else ""


def _normalize_icon_path(raw: str) -> str:
    """DisplayIcon 可能带 ``,0`` 图标索引或引号，取出纯路径。"""
    text = raw.strip().strip('"')
    if not text:
        return ""
    # 仅当逗号后是纯数字索引时才裁剪，避免误伤含逗号的目录名。
    head, sep, tail = text.rpartition(",")
    if sep and head and tail.strip().lstrip("-").isdigit():
        return head.strip().strip('"')
    return text


def _candidates_from_dir(directory: str | os.PathLike[str], source: str):
    """在安装根目录及其已知子目录下查找白名单启动器。"""
    try:
        base = Path(directory).expanduser()
    except (OSError, ValueError, RuntimeError):
        return
    for sub in _SUBDIR_CANDIDATES:
        target_dir = base / sub if sub else base
        for name in TRUSTED_LAUNCHER_NAMES:
            candidate = target_dir / name
            if is_trusted_launcher(candidate):
                yield LauncherCandidate(path=candidate, source=source)


def _iter_common_paths():
    """扫描常见安装目录。仅在注册表未命中时调用。"""
    if not sys.platform.startswith("win"):
        return
    drives = []
    for letter in "CDEFGH":
        drive = Path(f"{letter}:/")
        try:
            if drive.exists():
                drives.append(drive)
        except OSError:
            continue
    for drive in drives:
        for dir_name in _COMMON_DIR_NAMES:
            yield drive / dir_name


def discover_launchers(user_path: str | None = None) -> list[LauncherCandidate]:
    """按优先级返回所有通过校验的启动器候选，去重且保持顺序。"""
    found: list[LauncherCandidate] = []
    seen: set[str] = set()

    def push(candidate: LauncherCandidate) -> None:
        key = str(candidate.path).lower()
        if key not in seen:
            seen.add(key)
            found.append(candidate)

    # 1. 用户显式指定优先。
    if user_path:
        expanded = os.path.expandvars(str(user_path).strip())
        if is_trusted_launcher(expanded):
            push(LauncherCandidate(path=Path(expanded), source="用户指定"))
        else:
            # 用户可能填了安装目录而非 exe，尝试在目录下找。
            for candidate in _candidates_from_dir(expanded, "用户指定目录"):
                push(candidate)

    # 2. 注册表。
    for install_location, display_icon in _iter_registry_locations():
        icon_path = _normalize_icon_path(display_icon)
        if is_trusted_launcher(icon_path):
            push(LauncherCandidate(path=Path(icon_path), source="注册表"))
        if install_location:
            for candidate in _candidates_from_dir(install_location, "注册表"):
                push(candidate)

    # 3. 常见路径兜底。
    for directory in _iter_common_paths():
        for candidate in _candidates_from_dir(directory, "常见安装路径"):
            push(candidate)

    return found


def resolve_launcher(user_path: str | None = None) -> LauncherCandidate | None:
    """返回首个可信启动器；全部失败返回 ``None``。"""
    candidates = discover_launchers(user_path)
    return candidates[0] if candidates else None


def describe_failure(user_path: str | None = None) -> str:
    """探测失败时给出可操作的说明，而不是只报"没找到"。"""
    if user_path:
        return (
            f"指定的云异环启动器无效: {user_path!r}。"
            f"请填写 {' 或 '.join(TRUSTED_LAUNCHER_NAMES)} 的完整路径，"
            "或填写云异环安装目录。"
        )
    return (
        "未能自动找到云异环客户端。"
        "请在任务选项中填写启动器完整路径（如 "
        r"E:\NevernessToEvernessCloudGame\NTECloudLauncher.exe）。"
    )


__all__ = [
    "TRUSTED_LAUNCHER_NAMES",
    "LauncherCandidate",
    "is_trusted_launcher",
    "discover_launchers",
    "resolve_launcher",
    "describe_failure",
]
