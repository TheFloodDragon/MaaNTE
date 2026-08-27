"""FishPro 控条实现。

分层结构：

- ``config``：参数定义与 ``custom_action_param`` 解析；
- ``vision``：ROI 裁剪、绿条与光标识别、候选打分；
- ``state``：平滑、速度估计、预测与派生观测量；
- ``policy``：规则控制层（强度曲线、静默滞回、恢复与降档）；
- ``learning``：在线残差策略与样本/模型持久化；
- ``executor``：动作到 A/D 按键的状态机；
- ``runtime``：单次控条会话主循环。
"""

from .config import FishProConfig, load_fish_pro_config
from .executor import ActionExecutor, ControllerKeyAdapter
from .learning import ResidualPolicy
from .runtime import FishProSession, SessionResult

__all__ = [
    "ActionExecutor",
    "ControllerKeyAdapter",
    "FishProConfig",
    "FishProSession",
    "ResidualPolicy",
    "SessionResult",
    "load_fish_pro_config",
]
