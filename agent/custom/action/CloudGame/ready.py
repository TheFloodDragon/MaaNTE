"""等待云异环从"窗口出现"走到"真正进入游戏"。

这一层刻意**不识别云客户端自身的界面**（登录页、排队队列、串流加载）。
原因：仓库里没有任何云客户端 UI 的模板与 ROI，而 ``coding-standards.md``
明确禁止在缺少界面信息的情况下凭空编写识别节点。

替代做法是只依赖**已验证的游戏内公共节点**：

- ``InWorld``     —— 大世界（Esc 菜单按钮 + 任务菜单按钮同时命中）
- ``InMiniWorld`` —— 小世界（粉爪、主线故事等）

只要其中之一命中，就说明登录、排队、串流、加载全部已经走完 —— 因为这些
画面里不可能出现游戏内的 HUD 按钮。这样既不猜界面，又能可靠回答
"是否已经可以开始跑任务了"。

代价是：**看不到中间过程**。超时时无法区分"卡在排队"还是"登录失败"，
只能报告"未能进入游戏"。要给出细分原因，必须补云客户端界面模板。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# 已验证的游戏内场景判定节点（Interface/Scene/Status.json 暴露的公共接口）。
IN_GAME_NODES = ("InWorld", "InMiniWorld")

DEFAULT_READY_TIMEOUT = 300.0
DEFAULT_POLL_INTERVAL = 2.0
# 连续命中多少次才认为稳定进入游戏，避免加载途中的单帧误命中。
DEFAULT_CONFIRM_HITS = 2


@dataclass
class ReadyResult:
    """等待结果。"""

    ready: bool
    node: str
    elapsed: float
    message: str


def _is_hit(result) -> bool:
    """判断识别是否命中。

    ``run_recognition`` 返回 ``RecognitionDetail``（只有 ``hit``），
    ``run_task`` 返回 ``TaskDetail``（看 ``status.succeeded``），两种都要吃。

    缺省必须是 ``False``：判错方向会让任务谎报「已进入游戏」，
    后续每日流程全部空跑。见 ``enter.py`` 里的同名函数。
    """
    if result is None:
        return False
    hit = getattr(result, "hit", None)
    if hit is not None:
        return bool(hit)
    status = getattr(result, "status", None)
    succeeded = getattr(status, "succeeded", None)
    if succeeded is not None:
        return bool(succeeded)
    if status is not None:
        return status == 0
    return False


def wait_until_in_game(
    context,
    ready_timeout: float = DEFAULT_READY_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    confirm_hits: int = DEFAULT_CONFIRM_HITS,
    should_stop=None,
    logger=None,
    clock=time.monotonic,
    sleeper=time.sleep,
    nodes: tuple[str, ...] | None = None,
) -> ReadyResult:
    """轮询游戏内公共节点，直到确认已进入游戏。

    ``context`` 需提供 ``tasker.controller`` 用于截图、``run_recognition``
    用于识别。时钟与休眠可注入，便于离线测试。
    """
    target_nodes = nodes or IN_GAME_NODES
    started = clock()
    deadline = started + max(float(ready_timeout), 0.0)
    needed = max(int(confirm_hits), 1)

    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    streak = 0
    hit_node = ""
    logged_wait = False

    while clock() < deadline:
        if should_stop is not None and should_stop():
            return ReadyResult(
                ready=False,
                node="",
                elapsed=clock() - started,
                message="等待进入游戏时任务被停止",
            )

        image = _screencap(context)
        if image is None:
            streak = 0
            sleeper(max(float(poll_interval), 0.05))
            continue

        matched = ""
        for node in target_nodes:
            try:
                if _is_hit(context.run_recognition(node, image)):
                    matched = node
                    break
            except Exception as exc:
                # 识别异常不能当作"已进入游戏"，否则会在加载页就放行。
                log(f"识别 {node} 异常: {exc}")

        if matched:
            if matched != hit_node:
                hit_node = matched
                streak = 0
            streak += 1
            if streak >= needed:
                elapsed = clock() - started
                log(f"已进入游戏（{matched}），耗时 {elapsed:.1f}s")
                return ReadyResult(
                    ready=True,
                    node=matched,
                    elapsed=elapsed,
                    message=f"已进入游戏（{matched}）",
                )
        else:
            streak = 0
            hit_node = ""
            if not logged_wait:
                logged_wait = True
                log("云异环窗口已就绪，正在等待登录/排队/加载完成")

        sleeper(max(float(poll_interval), 0.05))

    elapsed = clock() - started
    return ReadyResult(
        ready=False,
        node="",
        elapsed=elapsed,
        message=(
            f"等待进入游戏超时（{ready_timeout:.0f}s）。"
            "可能仍在排队、登录未完成，或云客户端未自动进入游戏；"
            "请确认云客户端已保存登录状态并可自动进入。"
        ),
    )


def _screencap(context):
    """通过控制器截取当前画面；失败返回 ``None``。"""
    controller = getattr(getattr(context, "tasker", None), "controller", None)
    if controller is None:
        return None
    try:
        return controller.post_screencap().wait().get()
    except Exception:
        return None


__all__ = [
    "DEFAULT_CONFIRM_HITS",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_READY_TIMEOUT",
    "IN_GAME_NODES",
    "ReadyResult",
    "wait_until_in_game",
]
