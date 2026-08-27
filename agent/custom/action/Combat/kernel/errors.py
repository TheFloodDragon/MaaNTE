"""战斗内核异常。

从 ``pinkpaw_core3`` 原样提取，语义保持不变：

- ``AbortException``：流程判定失败，需要中止当前逻辑并走恢复分支。
- ``TaskerStoppedException``：MAA tasker 请求停止，必须立即放弃所有按键。
"""

from __future__ import annotations


class AbortException(Exception):
    """流程主动中止。"""


class TaskerStoppedException(Exception):
    """MAA tasker 停止任务。"""
