"""新一代钓鱼控条自定义动作。

只做参数解析、依赖装配、会话驱动与结果返回，控条算法全部位于
``fishpro`` 子包内，便于离线单独验证。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils.logger import logger
from utils.maafocus import PrintT

from .fishpro.config import load_fish_pro_config
from .fishpro.executor import ActionExecutor, ControllerKeyAdapter
from .fishpro.learning import ResidualPolicy
from .fishpro.runtime import FishProSession

# 学习产物与调试帧输出目录，相对项目根定位，避免污染 assets。
_ARTIFACT_SUBDIR = ("debug", "fishpro")


def _resolve_artifact_dir() -> Path:
    """定位 ``debug/fishpro/``，找不到项目根时退回当前工作目录。"""
    for base in (Path(__file__).resolve().parents[4], Path.cwd()):
        try:
            if base.exists():
                return base.joinpath(*_ARTIFACT_SUBDIR)
        except OSError:
            continue
    return Path.cwd().joinpath(*_ARTIFACT_SUBDIR)


@AgentServer.custom_action("auto_fish_pro")
class AutoFishPro(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        config = load_fish_pro_config(argv.custom_action_param)
        controller = context.tasker.controller
        artifact_dir = _resolve_artifact_dir()

        residual_policy: Optional[ResidualPolicy] = None
        if config.learning_enabled:
            residual_policy = ResidualPolicy(config, artifact_dir)
            load_info = residual_policy.load_artifacts(
                replay_history=config.learning_replay_history
            )
            logger.info(
                "FishPro 学习模式已开启: model=%s history=%d | %s",
                "已加载" if load_info["loaded"] else "未命中",
                load_info["history_count"],
                residual_policy.summary(),
            )
            PrintT(context, "autofish.pro_learning_on")

        def screencap() -> Optional[np.ndarray]:
            try:
                return controller.post_screencap().wait().get()
            except Exception as exc:
                logger.warning("FishPro 截图失败: %s", exc)
                return None

        def should_stop() -> bool:
            return bool(context.tasker.stopping)

        executor = ActionExecutor(ControllerKeyAdapter(controller), config)
        session = FishProSession(
            config=config,
            executor=executor,
            screencap=screencap,
            should_stop=should_stop,
            residual_policy=residual_policy,
            debug_dir=artifact_dir if config.debug_enabled else None,
        )

        result = session.run()
        if result.valid_frames > 0:
            logger.info(
                "FishPro 本次控条留框率: %.1f%%（%d/%d 帧）",
                result.inside_ratio * 100.0,
                result.inside_frames,
                result.valid_frames,
            )
        return CustomAction.RunResult(success=result.success)
