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

from .fishpro.config import load_custom_action_params, load_fish_pro_config
from .fishpro.executor import ActionExecutor, ControllerKeyAdapter
from .fishpro.learning import ResidualPolicy
from .fishpro.runtime import FishProSession

# 学习产物与调试帧输出目录，相对项目根定位，避免污染 assets。
_ARTIFACT_SUBDIR = ("debug", "fishpro")

# 界面选项的载体节点。每个选项覆盖各自独立的节点，Python 侧再把它们的
# ``attach`` 合并进 ``custom_action_param``。
#
# 为什么不让选项直接改 ``FishNewGamingPro`` 的 ``custom_action_param``：
# GUI 合并多个 option 的 pipeline_override 时，``custom_action_param``
# 是**整体替换**而不是逐键合并。「学习模式」与「调试输出」两个子选项都改这一个
# 字段时只有最后一个能活下来，并且会连同 pipeline 里写死的 ``roi_px`` 与各项
# 超时一起冲掉——控条 ROI 静默回落到代码默认值，问题极难定位。
#
# 改成每个选项写各自的节点就不存在互相覆盖——字段路径本来就不同。
_OPTION_NODES = (
    "FishNewGamingPro_LearningOption",
    "FishNewGamingPro_DebugOption",
)


def _collect_option_params(context) -> dict:
    """把界面选项载体节点的 ``attach`` 合并成参数字典。

    空值一律跳过：表示「这个选项没有给出有效值」，应回落到
    ``custom_action_param``。读不到节点不算错误（脚本直接调用时如此）。
    """
    merged: dict = {}
    for node in _OPTION_NODES:
        try:
            data = context.get_node_data(node)
        except Exception:
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
    return merged


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
        # 界面选项走载体节点，pipeline / 内联调用走 custom_action_param。
        # 选项优先：它代表用户在界面上的显式选择。合并在解析之前完成，
        # 这样 roi_px 等只在 pipeline 里写死的参数不会被选项冲掉。
        inline = load_custom_action_params(argv.custom_action_param)
        from_options = _collect_option_params(context)
        config = load_fish_pro_config({**inline, **from_options})
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
