"""共用的进入世界识别与等待预算，不导入 AgentServer，供 Win32 和网页 CDP 使用。

识别资源由 Pipeline 定义，调用方提供截图及输入。窗口/串流存在不等于进入世界；
同一画面只操作一次，随后等待可识别的状态变化，不重启游戏或重新排队。
"""

import math
import time
from dataclasses import dataclass, field

from utils.logger import logger
from utils.maafocus import PrintT

LOGIN_TIMEOUT_NODE = "EnterGameLoginTimeoutConfig"
ACCOUNT_NODE = "EnterGameAccountLogin"
READY_NODE = "CloudWebWorldReady"
WORLD_NODES = (
    ACCOUNT_NODE,
    "CloudWebWorldAnnouncement",
    "CloudWebWorldMonthlyPass",
    "CloudWebWorldClickBlank",
    "CloudWebWorldCloseButton",
    "CloudWebWorldLoading",
    "CloudWebWorldLoginEnter",
    READY_NODE,
    "CloudWebWorldClickEnter",  # 保留无退出图标的完整进入文字兜底，放在其他识别之后。
)
_LOGIN_SUCCESS_STATES = {
    READY_NODE,
    "CloudWebWorldLoginEnter",
    "CloudWebWorldClickEnter",
    "CloudWebWorldAnnouncement",
    "CloudWebWorldMonthlyPass",
    "CloudWebWorldLoading",
}


class EntryError(RuntimeError):
    """只携带固定错误码和翻译参数，不记录账号或识别原文。"""

    def __init__(self, key, reason="entry_failed", *values):
        super().__init__(key)
        self.key, self.reason, self.values = key, reason, values


def login_timeout_seconds(context, fallback_seconds=600):
    """所有控制器共用一个分钟配置；缺失时兼容旧版独立节点/单节点调用。"""
    node = context.get_node_data(LOGIN_TIMEOUT_NODE)
    if node is None:
        node = context.get_node_data("CloudWebLoginTimeoutConfig")
    if node is None:
        return float(fallback_seconds)
    attach = node.get("attach") if isinstance(node, dict) else None
    value = attach.get("minutes") if isinstance(attach, dict) else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not float(value).is_integer()
        or not 1 <= value <= 120
    ):
        raise EntryError("enter_game.invalid_config", "invalid_login_timeout")
    return float(value) * 60


def _action_state(name):
    # 同一进入按钮有带退出图标和无图标两个识别分支；图标抖动不代表新状态。
    return "CloudWebWorldClickEnter" if name == "CloudWebWorldLoginEnter" else name


@dataclass
class WorldProgress:
    login_timeout: float
    world_timeout: float = 300
    transition_timeout: float = 90
    world_deadline: float = field(init=False)
    login_deadline: float | None = None
    login_started: float | None = None
    pending_node: str | None = None
    pending_deadline: float | None = None
    stable: int = 0
    last_state: str | None = None
    login_prompted: bool = False

    def __post_init__(self):
        self.world_deadline = time.monotonic() + self.world_timeout

    def deadline(self):
        if self.login_started is not None:
            return self.login_deadline
        return min(self.world_deadline, self.pending_deadline or math.inf)

    def check(self, context):
        if context.tasker.stopping:
            raise EntryError("enter_game.stopped", "stopping")
        now = time.monotonic()
        if self.login_started is not None:
            if now >= self.login_deadline:
                raise EntryError(
                    "enter_game.login_timeout",
                    "login_deadline",
                    round(self.login_timeout / 60),
                )
        elif now >= self.world_deadline:
            raise EntryError("enter_game.world_timeout", "world_deadline")
        if self.pending_deadline is not None and now >= self.pending_deadline:
            raise EntryError("enter_game.transition_failed", "unchanged_action_state")

    def observe(self, context, name):
        now = time.monotonic()
        if name != self.last_state:
            logger.info("进入游戏画面 | state=%s", name)
            self.last_state = name
        if name == ACCOUNT_NODE:
            if self.login_deadline is None:
                self.login_deadline = now + self.login_timeout
            if self.login_started is None:
                self.login_started = now
            self.pending_node = self.pending_deadline = None
            if not self.login_prompted:
                PrintT(
                    context, "enter_game.login_required", round(self.login_timeout / 60)
                )
                self.login_prompted = True
        elif self.login_started is not None and name in _LOGIN_SUCCESS_STATES:
            # 账号等待不侵占世界加载预算；同一次运行再次出现登录提示也不重置期限。
            self.world_deadline += now - self.login_started
            self.login_started = None
        if name != "unknown" and _action_state(name) != _action_state(
            self.pending_node
        ):
            self.pending_node = self.pending_deadline = None
        self.check(context)


def _valid_frame(frame):
    if (
        getattr(frame, "ndim", 0) != 3
        or frame.shape[:2] != (720, 1280)
        or frame.shape[2] not in (3, 4)
    ):
        raise EntryError("enter_game.invalid_frame", "frame_shape")
    return frame


def _recognize(context, node, frame, check):
    detail = context.run_recognition(node, frame)
    check()  # 慢 OCR 的迟到结果不能触发输入或被接受为成功。
    if detail is None:
        raise EntryError("enter_game.recognition_failed", "recognition_unavailable")
    return detail


def _point(detail):
    box = detail.box
    if not isinstance(box, (list, tuple)):
        box = [getattr(box, key, None) for key in ("x", "y", "w", "h")]
    if (
        len(box) != 4
        or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in box
        )
        or min(box[2:]) <= 0
        or min(box[:2]) < 0
        or box[0] + box[2] > 1280
        or box[1] + box[3] > 720
        or len(getattr(detail, "filtered_results", []) or []) > 1
    ):
        raise EntryError("enter_game.input_rejected", "ambiguous_target")
    return box[0] + box[2] / 2, box[1] + box[3] / 2


def run_world_entry(context, capture, click, key, *, progress=None, before_step=None):
    """识别→复核输入目标→操作→新截图确认；只有两帧大世界才成功。

    capture 返回 None 表示串流暂时还没有视频内容，仍受当前预算约束。
    before_step 接收当前截止时间，便于原生云复用其窗口操作预算检查。
    """
    progress = progress or WorldProgress(login_timeout_seconds(context))
    PrintT(context, "enter_game.started")

    def check():
        progress.check(context)
        if before_step is not None:
            before_step(progress.deadline())
        progress.check(context)

    def pause(seconds=0.4):
        end = min(time.monotonic() + seconds, progress.deadline())
        while time.monotonic() < end:
            check()
            time.sleep(max(0.0, min(0.1, end - time.monotonic())))
        check()

    while True:
        check()
        frame = capture()
        check()
        if frame is None:
            progress.stable = 0
            pause()
            continue
        frame = _valid_frame(frame)
        hit = None
        for node in WORLD_NODES:
            detail = _recognize(context, node, frame, check)
            if detail.hit:
                hit = node, detail
                break
        name = hit[0] if hit else "unknown"
        progress.observe(context, name)
        check()
        if name == READY_NODE:
            progress.stable += 1
            if progress.stable >= 2:
                check()
                PrintT(context, "enter_game.world_ready")
                return True
            pause(0.6)
            continue
        progress.stable = 0
        if not hit or name == ACCOUNT_NODE or progress.login_started is not None:
            pause()
            continue
        attach = (context.get_node_data(name) or {}).get("attach", {})
        action = attach.get("web_action", "wait")
        if action not in ("click", "esc") or _action_state(name) == _action_state(
            progress.pending_node
        ):
            pause()
            continue
        # OCR 后重新取帧，并复核全部更高优先级状态，不能点击新弹窗后面的旧按钮。
        fresh = capture()
        check()
        if fresh is None:
            pause()
            continue
        fresh = _valid_frame(fresh)
        fresh_hit = None
        for candidate in WORLD_NODES:
            detail = _recognize(context, candidate, fresh, check)
            if detail.hit:
                fresh_hit = candidate, detail
                break
        if fresh_hit is None or fresh_hit[0] != name:
            if fresh_hit is not None:
                progress.observe(context, fresh_hit[0])
            pause()
            continue
        detail = fresh_hit[1]
        progress.pending_node = name
        progress.pending_deadline = time.monotonic() + progress.transition_timeout
        check()
        if action == "click":
            result = click(*_point(detail))
        else:
            result = key("Escape")
        check()
        if result is False:
            raise EntryError("enter_game.input_rejected", "input_failed")
        PrintT(context, "enter_game." + attach.get("report", "action"))
        pause()
