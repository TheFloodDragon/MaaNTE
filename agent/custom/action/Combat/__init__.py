"""战斗支持模块。

分层（见 docs/zh_cn/develop/combat-engine.md）：

- ``kernel``   —— 底层机制：等待、键鼠、队伍 UI 识别、切人确认状态机。
                 由 ``pinkpaw`` 路线与战斗引擎共用。
- ``identity`` —— 角色身份识别：侧栏头像匹配 -> 当前是谁。
- ``script``   —— 编排层：JSON 规则表的解析、条件求值与决策，纯逻辑无 IO。
- ``runtime``  —— 会话生命周期：进战、tick 循环、退出、安全释放。

``kernel`` 之外的层次随后续阶段落地。
"""
