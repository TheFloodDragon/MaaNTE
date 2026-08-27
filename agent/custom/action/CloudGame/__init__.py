"""云异环客户端启动支持。

分层：

- ``locator``  —— 安装位置探测（注册表 / 常见路径 / 用户指定），带白名单校验
- ``launcher`` —— 进程启动与窗口就绪等待，可注入时钟与 spawn，便于离线测试
- ``action``   —— MAA CustomAction 入口，串联上面两层并等待进入游戏

安全边界：只启动云异环自身的可执行文件，不读取/不存储任何账号凭据。
"""

from .locator import (
    TRUSTED_LAUNCHER_NAMES,
    LauncherCandidate,
    describe_failure,
    discover_launchers,
    is_trusted_launcher,
    resolve_launcher,
)
from .launcher import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WINDOW_TIMEOUT,
    LaunchResult,
    launch_cloud_game,
    resolve_executable,
)
from .action import CloudGameLaunch

__all__ = [
    "TRUSTED_LAUNCHER_NAMES",
    "LauncherCandidate",
    "describe_failure",
    "discover_launchers",
    "is_trusted_launcher",
    "resolve_launcher",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_WINDOW_TIMEOUT",
    "LaunchResult",
    "launch_cloud_game",
    "resolve_executable",
    "CloudGameLaunch",
]
