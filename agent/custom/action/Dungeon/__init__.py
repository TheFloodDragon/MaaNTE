"""刷本任务：传送 -> 选副本 -> 进入 -> 战斗 -> 结算 -> 领奖 -> 循环。

分层与 ``Combat`` 保持一致：

- ``config``  配置解析与校验（纯逻辑）
- ``runner``  循环编排（纯逻辑，依赖注入，可离线仿真）
- ``steps``   步骤 -> MAA 调用（薄桥接层）
- ``action``  MAA 自定义动作入口（参数解析与装配）

所有屏幕坐标、OCR 文本、模板路径都来自用户配置，本包不内置任何未经实机
标定的识别参数。
"""

from .action import DungeonFarm

__all__ = ["DungeonFarm"]
