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

# 界面选项的载体节点。每个选项覆盖各自独立的节点，Python 侧再把它们的
# ``attach`` 合并成参数字典。
#
# 为什么不让选项直接改 ``CloudGameLaunchMain`` 的 ``custom_action_param``：
# GUI 合并多个 option 的 pipeline_override 时，``custom_action_param``
# 是**整体替换**而不是逐键合并。三个选项都改这一个字段时只有最后一个能活下来，
# 表现为用户填的启动器路径被忽略、「等待进入游戏」开关关不掉。
#
# 改成每个选项写各自的节点就不存在互相覆盖——字段路径本来就不同。
# 这套机制与 ``AutoCombat_*`` / ``DungeonFarm_*`` 相同。
OPTION_NODES = (
    "CloudGameLaunch_PathOption",
    "CloudGameLaunch_WaitInGameOption",
    "CloudGameLaunch_ReadyTimeoutOption",
)


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


def _collect_option_params(context) -> dict:
    """把界面选项载体节点的 ``attach`` 合并成参数字典。

    空值（``None`` / 空字符串）一律跳过：它们表示「这个选项没有给出有效值」。
    启动器路径留空正是常态（表示走自动探测），必须回落到
    ``custom_action_param`` 的默认值而不是拿着 ``None`` 去启动进程。

    读不到节点不算错误：脚本直接调用、或某个 Client 不下发选项时都会这样。
    """
    merged: dict = {}
    failed: list[str] = []
    for node in OPTION_NODES:
        try:
            data = context.get_node_data(node)
        except Exception:
            failed.append(node)
            continue
        attach = data.get("attach") if isinstance(data, dict) else None
        if not isinstance(attach, dict):
            continue
        for key, value in attach.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            merged[key] = value
    if failed and len(failed) < len(OPTION_NODES):
        logger.warning("%s 这些选项载体节点读不到: %s", _LOG_PREFIX, failed)
    return merged


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
        # 界面选项走载体节点，pipeline / 内联调用走 custom_action_param。
        # 选项优先：它代表用户在界面上的显式选择。
        inline = _parse_params(argv)
        from_options = _collect_option_params(context)
        if from_options:
            logger.info(
                "%s 界面选项: %s", _LOG_PREFIX, sorted(from_options)
            )
        params = {**inline, **from_options}

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
