"""云异环网页端启动流程（仿三月七小助手云星铁 enter_cloud_game）。

网页大厅阶段（登录、时长、节点、时长确认、排队、串流）全部走 CDP 读 DOM 与派发输入；
游戏内阶段用 Pipeline 识别节点处理 CDP 截取的 1280x720 帧，再按节点 attach.web_action
通过 CDP 点击/按键，直到稳定进入大世界。不购买时长、不勾选“不再提醒”、不代填登录信息。
"""

import math
import re
import time
from dataclasses import dataclass, field
from functools import wraps
from uuid import uuid4

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from cloud_web_start import WebClientError, safe_reason, start_browser
from utils.cloud_cdp import CloudSession, format_minutes, parse_queue
from utils.logger import logger
from utils.maafocus import PrintT
from utils.pienv import controller_name

__all__ = [
    "CloudWebReset", "CloudWebLogin", "CloudWebLobby", "CloudWebQueue",
    "CloudWebWorld", "CloudWebFinish", "CloudWebFail", "cleanup_cloud_web_session",
]

CONTROLLER = "CloudGame-Web"
MAX_ATTEMPTS = 3  # 与三月七小助手 start_game 的 MAX_RETRY 一致
PAGE_RETRIES = 3
WORLD_NODES = (
    "CloudWebWorldReady", "CloudWebWorldLoginEnter", "CloudWebWorldAnnouncement",
    "CloudWebWorldMonthlyPass", "CloudWebWorldClickBlank", "CloudWebWorldCloseButton", "CloudWebWorldLoading",
)
_CLOSE_KINDS = ("setting", "page-setting", "rule", "record", "gift", "mall", "jump-payment", "exitsetting")


@dataclass
class _Web:
    task_id: int
    auto_queue: bool
    login_timeout: float
    queue_timeout: float
    fullscreen: bool
    session: CloudSession | None = None
    attempts: int = 0
    queue_deadline: float | None = None
    last_queue: tuple = ()
    last_queue_log: float = -math.inf
    events: list = field(default_factory=list)
    phase_states: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    run_id: str = field(default_factory=lambda: uuid4().hex[:10])


_web: _Web | None = None


class _WebError(Exception):
    def __init__(self, key, reason=None, *args):
        super().__init__(key)
        self.key, self.reason, self.args_ = key, reason or key, args


def _phase(state, phase, status):
    if state.phase_states.get(phase) != status:
        logger.info("云异环网页状态变化 | phase=%s | from=%s | to=%s | run_id=%s",
                    phase, state.phase_states.get(phase, "initial"), status, state.run_id)
        state.phase_states[phase] = status


def _guard(run):
    @wraps(run)
    def wrapper(self, context, argv):
        started, action = time.monotonic(), type(self).__name__
        logger.info("云异环网页操作开始 | action=%s", action)
        success, reason = False, "returned_false"
        try:
            _check_stop(context)
            success = bool(run(self, context, argv))
            reason = "completed" if success else reason
        except _WebError as exc:
            reason = exc.reason
            if not context.tasker.stopping:
                PrintT(context, exc.key, *exc.args_)
        except WebClientError as exc:
            reason = safe_reason(exc)
            if not context.tasker.stopping:
                PrintT(context, "cloud_web.browser_failed", reason)
        except Exception as exc:  # 只记录类型，不输出异常正文或页面内容
            reason = "unexpected_" + type(exc).__name__
            if not context.tasker.stopping:
                PrintT(context, "cloud_web.failed")
        finally:
            (logger.info if success else logger.error)(
                "云异环网页操作结束 | action=%s | result=%s | reason=%s | elapsed=%.3fs",
                action, "succeeded" if success else "failed", reason, time.monotonic() - started)
            if not success:
                cleanup_cloud_web_session()
        return CustomAction.RunResult(success=success)

    return wrapper


def _check_stop(context):
    if context.tasker.stopping:
        raise _WebError("cloud_web.stopped", "task_stopping")


def _sleep(context, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _check_stop(context)
        time.sleep(min(0.1, max(0.0, end - time.monotonic())))


def _require():
    if _web is None or _web.session is None:
        raise _WebError("cloud_web.failed", "session_missing")
    return _web


def _attach(context, node, key, default):
    data = context.get_node_data(node) or {}
    value = (data.get("attach") or {}).get(key, default)
    return value


def _minutes(context, node, default, maximum):
    value = _attach(context, node, "minutes", default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise _WebError("cloud_web.invalid_config", "invalid_minutes") from None
    if isinstance(value, bool) or not math.isfinite(value) or not 1 <= value <= maximum:
        raise _WebError("cloud_web.invalid_config", "minutes_out_of_range")
    return value


def _state(context, web):
    _check_stop(context)
    state = web.session.state()
    if state.get("events"):
        web.events.extend(state["events"][-50:])
        for event in state["events"]:
            logger.debug("云端串流事件 | kind=%s | state=%s | code=%s", event.get("k"), event.get("s"), event.get("code"))
    return state


def _button(pop, *labels):
    return next((b for b in pop["btns"] if b["x"] in labels), None)


def _classify_remind(text):
    """remind 弹窗只按固定关键词分类；时长不足与环境不支持直接停止，其余可重试。"""
    if "剩余时长不足" in text or "前往充值" in text:
        return "no_duration"
    if "不满足云游戏运行要求" in text:
        return "env_unsupported"
    if any(word in text for word in ("网络", "异常", "失败", "退出", "无启动参数", "稍候重试")):
        return "retry"
    return "unknown"


def _handle_popup(context, web, pop, clicked):
    """处理一个可见弹窗，返回 'queue' / 'acted' / None；永不点击购买/充值/开通。"""
    kind, session = pop["kind"], web.session
    if kind == "queue":
        return "queue"
    if kind == "nodelist":
        chosen = [row for row in pop["rows"] if row["on"]]
        if not chosen and "node_row" not in clicked:
            usable = [row for row in pop["rows"] if "维护" not in row["t"]]
            row = next((r for r in usable if "推荐" in r["t"]), usable[0] if usable else None)
            if row is None:
                raise _WebError("cloud_web.node_missing", "no_usable_node")
            session.click_rect(row["r"])
            clicked.add("node_row")
            PrintT(context, "cloud_web.node_selected", re.sub(r"\s+", " ", row["t"])[:30])
            return "acted"
        confirm = _button(pop, "确定")
        if chosen and confirm and "node_ok" not in clicked:
            session.click_rect(confirm["r"])
            clicked.add("node_ok")
            return "acted"
        return None
    if kind == "consume-duration":
        confirm = _button(pop, "确定")
        if confirm and "duration_ok" not in clicked:
            # 自带 30 秒倒计时，超时会自动取消进入；保持“不再提醒”原样，只点确定。
            session.click_rect(confirm["r"])
            clicked.add("duration_ok")
            PrintT(context, "cloud_web.duration_confirmed")
            return "acted"
        return None
    if kind == "enter-game":
        confirm = _button(pop, "确定", "继续", "进入游戏")
        if confirm and "env_ok" not in clicked:
            PrintT(context, "cloud_web.browser_warning")
            session.click_rect(confirm["r"])
            clicked.add("env_ok")
            return "acted"
        return None
    if kind == "remind":
        category = _classify_remind(pop["text"])
        logger.warning("网页提示弹窗 | category=%s", category)
        if category == "no_duration":
            raise _WebError("cloud_web.no_duration", "remind_no_duration")
        if category == "env_unsupported":
            raise _WebError("cloud_web.env_unsupported", "remind_env_unsupported")
        confirm = _button(pop, "确定", "我知道了", "知道了")
        if confirm:
            session.click_rect(confirm["r"])
        PrintT(context, "cloud_web.page_notice", pop["text"][:60])
        return "retry" if category == "retry" else "acted"
    if kind in _CLOSE_KINDS:
        close = next((b for b in pop["btns"] if "close" in b["c"] or b["x"] in ("关闭", "取消")), None)
        if close:
            session.click_rect(close["r"])
            PrintT(context, "cloud_web.popup_closed", kind)
            return "acted"
    logger.debug("未处理的网页弹窗 | kind=%s | buttons=%d", kind, len(pop["btns"]))
    return None


def cleanup_cloud_web_session():
    """释放本任务状态并恢复指针锁定；保留浏览器与云端会话。"""
    global _web
    web, _web = _web, None
    if web is not None and web.session is not None:
        try:
            web.session.pointer_lock(False)
        except Exception:
            pass
        web.session.close()


@AgentServer.custom_action("cloud_web_reset")
class CloudWebReset(CustomAction):
    @_guard
    def run(self, context: Context, argv: CustomAction.RunArg):
        global _web
        cleanup_cloud_web_session()
        if controller_name() != CONTROLLER:
            raise _WebError("cloud_web.wrong_controller", "wrong_controller")
        auto = _attach(context, "CloudWebAutoQueueConfig", "auto_queue", True)
        full = _attach(context, "CloudWebFullscreenConfig", "fullscreen", True)
        if not isinstance(auto, bool) or not isinstance(full, bool):
            raise _WebError("cloud_web.invalid_config", "invalid_switch")
        web = _Web(task_id=argv.task_detail.task_id, auto_queue=auto,
                   login_timeout=_minutes(context, "CloudWebLoginTimeoutConfig", 10, 120) * 60,
                   queue_timeout=_minutes(context, "CloudWebQueueTimeoutConfig", 60, 1440) * 60, fullscreen=full)
        logger.info("云异环网页配置 | auto_queue=%s | login_timeout_min=%.0f | queue_timeout_min=%.0f | fullscreen=%s",
                    auto, web.login_timeout / 60, web.queue_timeout / 60, full)
        PrintT(context, "cloud_web.browser_starting")
        state = start_browser(timeout=90, stopped=lambda: context.tasker.stopping)
        PrintT(context, "cloud_web.browser_launched" if state.launched else "cloud_web.browser_reused")
        web.session = CloudSession.connect()
        web.session.install_hooks()
        web.session.set_stream_override()
        for attempt in range(PAGE_RETRIES + 1):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                page = _state(context, web)
                if page["href_ok"] and page["ready"] == "complete" and (page["logged_in"] or page["logged_out"] or page["game"]):
                    _web = web
                    _phase(web, "page", "loaded")
                    return True
                _sleep(context, 0.5)
            if attempt < PAGE_RETRIES:
                PrintT(context, "cloud_web.page_reload", attempt + 1, PAGE_RETRIES)
                web.session.reload()
                _sleep(context, 2)
                web.session.install_hooks()
        raise _WebError("cloud_web.page_timeout", "page_load_timeout")


@AgentServer.custom_action("cloud_web_login")
class CloudWebLogin(CustomAction):
    @_guard
    def run(self, context: Context, argv: CustomAction.RunArg):
        web = _require()
        page = _state(context, web)
        if page["game"]:
            PrintT(context, "cloud_web.already_in_game")
            return context.override_next(argv.node_name, ["CloudWebQueue"])
        if not page["logged_in"]:
            _phase(web, "login", "required")
            PrintT(context, "cloud_web.login_required", round(web.login_timeout / 60))
            try:
                web.session.window_state("normal")
                web.session.bring_to_front()
            except WebClientError:
                logger.debug("无法把云异环窗口切到前台")
            deadline = time.monotonic() + web.login_timeout
            while not page["logged_in"]:
                if time.monotonic() >= deadline:
                    raise _WebError("cloud_web.login_timeout", "login_deadline", round(web.login_timeout / 60))
                _sleep(context, 2)
                page = _state(context, web)
            PrintT(context, "cloud_web.login_detected")
        _phase(web, "login", "logged_in")
        PrintT(context, "cloud_web.duration_checking")
        for _ in range(10):
            if page["free_min"] is not None or page["paid_min"] is not None:
                break
            _sleep(context, 0.5)
            page = _state(context, web)
        free, paid = page["free_min"], page["paid_min"]
        if free is None and paid is None:
            PrintT(context, "cloud_web.duration_unknown")
        elif (free or 0) + (paid or 0) <= 0:
            raise _WebError("cloud_web.no_duration", "zero_duration")
        else:
            PrintT(context, "cloud_web.duration", format_minutes((free or 0) + (paid or 0)),
                   format_minutes(paid or 0), format_minutes(free or 0))
        if not web.auto_queue:
            return context.override_next(argv.node_name, ["CloudWebManualEnterNotice"])
        return True


@AgentServer.custom_action("cloud_web_lobby")
class CloudWebLobby(CustomAction):
    """首页 → 进入游戏 → 节点 → 时长确认 → 排队弹窗；每个按钮每轮只点一次。"""

    @_guard
    def run(self, context: Context, argv: CustomAction.RunArg):
        web = _require()
        web.attempts += 1
        if web.attempts > MAX_ATTEMPTS:
            raise _WebError("cloud_web.stream_failed_final", "max_attempts", MAX_ATTEMPTS)
        if web.attempts > 1:
            PrintT(context, "cloud_web.retrying", web.attempts, MAX_ATTEMPTS)
        web.session.set_stream_override()
        web.session.pointer_lock(True)
        clicked, deadline = set(), time.monotonic() + 60
        while time.monotonic() < deadline:
            page = _state(context, web)
            if page["game"]:
                _phase(web, "lobby", "game_container")
                PrintT(context, "cloud_web.no_queue")
                return context.override_next(argv.node_name, ["CloudWebQueue"])
            outcome = None
            for pop in page["pops"]:
                outcome = _handle_popup(context, web, pop, clicked)
                if outcome:
                    break
            if outcome == "queue":
                _phase(web, "lobby", "queue")
                return True
            if outcome == "retry":
                clicked.clear()
            if not outcome and page["gift"] and "gift" not in clicked:
                web.session.click_rect(page["gift"]["r"])
                clicked.add("gift")
                PrintT(context, "cloud_web.daily_gift_closed")
            elif not outcome and not page["pops"] and page["start"] and "start" not in clicked:
                web.session.click_rect(page["start"])
                clicked.add("start")
                PrintT(context, "cloud_web.entering")
            _sleep(context, 0.2)
        raise _WebError("cloud_web.lobby_timeout", "lobby_deadline")


def _stream_hint(context, web):
    codes = [e.get("code") for e in web.events if e.get("k") == "connect" and e.get("s") == 3]
    fails = [e.get("code") for e in web.events if e.get("k") in ("start_fail", "remote_exit")]
    logger.warning("云端串流未建立 | connect_fail_codes=%s | start_fail_codes=%s", codes[-3:], fails[-3:])
    PrintT(context, "cloud_web.stream_failed", (codes or fails or ["?"])[-1])
    if 6043 in codes:
        PrintT(context, "cloud_web.network_hint")
    web.events.clear()


@AgentServer.custom_action("cloud_web_queue")
class CloudWebQueue(CustomAction):
    """排队等待 + 串流首帧；回到首页视为串流失败，交回大厅重试（最多 3 轮）。"""

    @_guard
    def run(self, context: Context, argv: CustomAction.RunArg):
        web = _require()
        if web.queue_deadline is None:
            web.queue_deadline = time.monotonic() + web.queue_timeout
            logger.info("云异环排队计时开始 | timeout_s=%.0f", web.queue_timeout)
        announced, game_since = False, None
        while True:
            page = _state(context, web)
            queue = next((p for p in page["pops"] if p["kind"] == "queue"), None)
            if page["game"]:
                if game_since is None:
                    game_since = time.monotonic()
                    _phase(web, "queue", "game_loading")
                    PrintT(context, "cloud_web.queue_done")
                video = page["video"] or {}
                if not page["loading"] and video.get("content") and video.get("rs", 0) >= 2:
                    PrintT(context, "cloud_web.stream_ready", video.get("w"), video.get("h"),
                           round(time.monotonic() - game_since, 1))
                    if web.fullscreen:
                        try:
                            web.session.window_state("fullscreen")
                        except WebClientError:
                            logger.warning("切换全屏失败，保持窗口模式")
                    return True
                if time.monotonic() - game_since > 180:
                    raise _WebError("cloud_web.stream_timeout", "first_frame_timeout")
            elif queue:
                info = parse_queue(queue["text"])
                if not announced:
                    PrintT(context, "cloud_web.queue_started")
                    announced = True
                key = (info["position"], info["eta"])
                now = time.monotonic()
                eta_changed = not web.last_queue or info["eta"] != web.last_queue[1]
                if key != web.last_queue and (eta_changed or now - web.last_queue_log >= 30):
                    PrintT(context, "cloud_web.queue_status", info["position"] or "?", info["total"] or "?", info["eta"] or "?")
                    web.last_queue, web.last_queue_log = key, now
                if now >= web.queue_deadline:
                    leave = _button(queue, "退出排队")
                    if leave:
                        web.session.click_rect(leave["r"])  # 不让无人值守的队列在超时后继续消耗时长
                        _sleep(context, 1.5)
                        # 实测退出排队后会重新弹出节点选择，只点“关闭”回到首页。
                        for pop in _state(context, web)["pops"]:
                            close = next((b for b in pop["btns"] if "close" in b["c"]), None)
                            if pop["kind"] == "nodelist" and close:
                                web.session.click_rect(close["r"])
                    raise _WebError("cloud_web.queue_timeout", "queue_deadline", round(web.queue_timeout / 60))
            elif page["home"]:
                if game_since is not None:
                    _stream_hint(context, web)
                for pop in page["pops"]:
                    _handle_popup(context, web, pop, set())
                _phase(web, "queue", "back_to_home")
                return context.override_next(argv.node_name, ["CloudWebLobby"])
            _sleep(context, 0.5 if (page["game"] or not queue) else 2.0)


@AgentServer.custom_action("cloud_web_world")
class CloudWebWorld(CustomAction):
    """用 Pipeline 节点识别 CDP 帧，按 attach.web_action 点击/按键，两帧稳定命中大世界即完成。"""

    @_guard
    def run(self, context: Context, argv: CustomAction.RunArg):
        web = _require()
        PrintT(context, "cloud_web.world_waiting")
        deadline, stable, last_hit, last_act = time.monotonic() + 300, 0, None, {}
        while time.monotonic() < deadline:
            page = _state(context, web)
            if page["home"] or not page["game"]:
                _stream_hint(context, web)
                return context.override_next(argv.node_name, ["CloudWebLobby"])
            content = (page["video"] or {}).get("content")
            if not content:
                _sleep(context, 0.5)
                continue
            frame = web.session.capture(content)
            hit = None
            for node in WORLD_NODES:
                detail = context.run_recognition(node, frame)
                if detail is not None and detail.hit:
                    hit = (node, detail)
                    break
            name = hit[0] if hit else "unknown"
            if name != last_hit:
                logger.info("云异环游戏画面 | state=%s", name)
                last_hit = name
            if name == "CloudWebWorldReady":
                stable += 1
                if stable >= 2:
                    PrintT(context, "cloud_web.world_ready")
                    return True
                _sleep(context, 0.6)
                continue
            stable = 0
            if hit:
                action = _attach(context, name, "web_action", "wait")
                now = time.monotonic()
                if action != "wait" and now - last_act.get(name, -math.inf) >= 2.5:
                    last_act[name] = now
                    if action == "click":
                        box = hit[1].box
                        web.session.click(*web.session.frame_to_css(content, (box[0] + box[2] / 2, box[1] + box[3] / 2)))
                    elif action == "esc":
                        web.session.key("Escape")
                    PrintT(context, "cloud_web.world_" + _attach(context, name, "report", "action"))
            _sleep(context, 0.4)
        raise _WebError("cloud_web.world_timeout", "world_deadline")


@AgentServer.custom_action("cloud_web_finish")
class CloudWebFinish(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        manual = argv.node_name == "CloudWebManualEnterNotice"
        elapsed = time.monotonic() - _web.started_at if _web is not None else 0.0
        logger.info("云异环网页流程结束 | result=%s | elapsed=%.1fs", "manual" if manual else "succeeded", elapsed)
        if not manual:
            PrintT(context, "cloud_web.done", round(elapsed))
        cleanup_cloud_web_session()
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("cloud_web_fail")
class CloudWebFail(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        logger.error("云异环网页流程失败 | stopping=%s", context.tasker.stopping)
        cleanup_cloud_web_session()
        return CustomAction.RunResult(success=False)
