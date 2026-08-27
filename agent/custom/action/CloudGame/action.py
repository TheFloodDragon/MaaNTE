"""云异环启动任务的 CustomAction 入口。

职责边界（与用户确认过的方案一致）：

- **凭据**：完全不碰。登录由云客户端自己完成（保持登录状态 / 自动登录），
  本任务不读取、不存储、不传递账号密码。
- **进程启动**：只启动通过白名单校验的云异环可执行文件，路径来自自动探测
  或用户显式填写。不使用 ``shell``。
- **调度**：不在此实现。定时由前端（MFAAvalonia / MXU）的调度器负责，
  本任务只作为"每日流程的第一个任务"被调用。

流程：解析参数 → 启动客户端并等窗口 → 等待进入游戏 → 成功/失败。
"""

from __future__ import annotations

import json

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils.logger import logger

from .launcher import (
    DEFAULT_POLL_INTERVAL as LAUNCH_POLL_INTERVAL,
    DEFAULT_WINDOW_TIMEOUT,
    launch_cloud_game,
)
from .ready import (
    DEFAULT_CONFIRM_HITS,
    DEFAULT_POLL_INTERVAL as READY_POLL_INTERVAL,
    DEFAULT_READY_TIMEOUT,
    wait_until_in_game,
)

_LOG_PREFIX = "[CloudGame]"


def _parse_params(argv: CustomAction.RunArg) -> dict:
    """解析 ``custom_action_param``；非法输入退化为空配置。"""
    value = getattr(argv, "custom_action_param", None)
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except Exception as exc:
        logger.warning("%s custom_action_param 解析失败: %s", _LOG_PREFIX, exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_float(value, default: float, minimum: float, maximum: float) -> float:
    """解析浮点参数并钳制到合法区间；非法值回退默认。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN
        return default
    return max(minimum, min(maximum, parsed))


def _parse_int(value, default: int, minimum: int, maximum: int) -> int:
    """解析整数参数并钳制到合法区间。"""
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@AgentServer.custom_action("CloudGameLaunch")
class CloudGameLaunch(CustomAction):
    """启动云异环并等待进入游戏。"""

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        params = _parse_params(argv)

        launcher_path = params.get("launcher_path") or ""
        window_timeout = _parse_float(
            params.get("window_timeout"), DEFAULT_WINDOW_TIMEOUT, 5.0, 600.0
        )
        ready_timeout = _parse_float(
            params.get("ready_timeout"), DEFAULT_READY_TIMEOUT, 10.0, 1800.0
        )
        confirm_hits = _parse_int(
            params.get("confirm_hits"), DEFAULT_CONFIRM_HITS, 1, 10
        )
        wait_in_game = params.get("wait_in_game", True)
        if isinstance(wait_in_game, str):
            wait_in_game = wait_in_game.strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }

        def should_stop() -> bool:
            try:
                stopping = context.tasker.stopping
                return bool(stopping() if callable(stopping) else stopping)
            except Exception:
                return False

        def log(message: str) -> None:
            logger.info("%s %s", _LOG_PREFIX, message)

        # 1. 启动客户端并等窗口出现。
        launch = launch_cloud_game(
            user_path=launcher_path,
            window_timeout=window_timeout,
            poll_interval=LAUNCH_POLL_INTERVAL,
            should_stop=should_stop,
            logger=log,
        )

        if launch.hwnd is None:
            logger.error("%s %s", _LOG_PREFIX, launch.message)
            return CustomAction.RunResult(success=False)

        if launch.already_running:
            log(launch.message)

        if not wait_in_game:
            log("已按配置跳过「等待进入游戏」")
            return CustomAction.RunResult(success=True)

        # 2. 等待真正进入游戏（依赖已验证的游戏内公共节点）。
        ready = wait_until_in_game(
            context,
            ready_timeout=ready_timeout,
            poll_interval=READY_POLL_INTERVAL,
            confirm_hits=confirm_hits,
            should_stop=should_stop,
            logger=log,
        )

        if not ready.ready:
            logger.error("%s %s", _LOG_PREFIX, ready.message)
            return CustomAction.RunResult(success=False)

        return CustomAction.RunResult(success=True)


__all__ = ["CloudGameLaunch"]
