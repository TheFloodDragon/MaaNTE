"""把云异环网页端启动动作注册到 AgentServer。

流程逻辑在 agent/cloud_web_flow.py。MXU 中实际由预任务 cloud_web_pretask.py 在连接
控制器之前、于进程内 Tasker 中执行；这里保留 Agent 侧注册，便于单节点调试 Pipeline，
同名动作与 Pipeline 的 custom_action 一一对应（等价于逐个使用 @AgentServer.custom_action）。
"""

from maa.agent.agent_server import AgentServer

from cloud_web_flow import (
    ACTIONS,
    CloudWebFail,
    CloudWebFinish,
    CloudWebLobby,
    CloudWebLogin,
    CloudWebQueue,
    CloudWebReset,
    CloudWebWorld,
    cleanup_cloud_web_session,
)

for _name, _action in ACTIONS.items():
    AgentServer.custom_action(_name)(_action)

__all__ = [
    "CloudWebReset",
    "CloudWebLogin",
    "CloudWebLobby",
    "CloudWebQueue",
    "CloudWebWorld",
    "CloudWebFinish",
    "CloudWebFail",
    "cleanup_cloud_web_session",
]
