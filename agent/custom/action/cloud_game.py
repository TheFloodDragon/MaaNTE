"""云异环的有界等待、单次操作校验和队列播报；场景分支由 Pipeline 管理。"""

import ctypes
import json
import math
import re
import sys
import time
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass, field
from functools import wraps
from html import escape
from uuid import uuid4

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from utils.logger import logger
from utils.maafocus import PrintT
from utils.pienv import controller_name

__all__ = [
    "CloudGameReset",
    "CloudGameWaitLogin",
    "CloudGameQueueWait",
    "CloudGameClick",
    "CloudGameConfirmReady",
    "CloudGameHandoff",
    "CloudGameFail",
    "CloudGameOwnedDialog",
    "CloudGameFinish",
    "cleanup_cloud_session",
]


# 只保存本次启动的非敏感运行状态。每次入口执行都重置，并校验 task_id。
@dataclass(frozen=True)
class _WindowInfo:
    hwnd: int
    rect: tuple[int, int, int, int]
    client_size: tuple[int, int]
    owner: int
    title: str
    class_name: str
    pid: int = 0


@dataclass(frozen=True)
class _ConfirmTarget:
    hwnd: int
    owner: int
    rect: tuple[int, int, int, int]
    client_size: tuple[int, int]
    box: tuple[int, int, int, int]
    pid: int = 0


@dataclass
class _Session:
    task_id: int
    auto_queue: bool
    timeout_minutes: float
    poll_interval: float
    login_timeout: float
    transition_timeout: float
    queue_deadline: float | None = None
    clicked: set[str] = field(default_factory=set)
    last_status: str = ""
    last_report_at: float = -math.inf
    queue_reported: bool = False
    loading_reported: bool = False
    confirm_target: _ConfirmTarget | None = None
    login_deadline: float | None = None
    main_hwnd: int = 0
    started_at: float = field(default_factory=lambda: time.monotonic())
    log_states: dict[str, str] = field(default_factory=dict)
    operation_deadline: float | None = None
    operation_timeout_key: str = "cloud_game.transition_failed"
    run_id: str = field(default_factory=lambda: uuid4().hex[:12])
    main_pid: int = 0


_session: _Session | None = None
_confirm_target: _ConfirmTarget | None = None
# 只缓存布尔值、计数和内部状态；按会话清空并限制大小，避免逐帧刷屏。
_diagnostics: dict[tuple, tuple] = {}
_MAX_DIAGNOSTICS = 64


class _CloudError(Exception):
    def __init__(self, key="cloud_game.failed", reason=None):
        self.key = key
        # reason 仅使用代码中的固定诊断原因，不接收异常正文或用户参数。
        self.reason = reason or key
        super().__init__(key)


def _log_diagnostic(key, values, message, *args):
    """同一诊断只在结果变化时输出；调用方不得传入 OCR 原文或凭据。"""
    if key in _diagnostics and _diagnostics[key] == values:
        return
    if key not in _diagnostics and len(_diagnostics) >= _MAX_DIAGNOSTICS:
        _diagnostics.pop(next(iter(_diagnostics)))
    _diagnostics[key] = values
    logger.debug(message, *args)


def _log_state(state, phase, status):
    previous = state.log_states.get(phase)
    if previous != status:
        logger.info(
            "云游戏状态变化 | task_id=%s | phase=%s | from=%s | to=%s | run_id=%s",
            state.task_id, phase, previous or "initial", status, state.run_id,
        )
        state.log_states[phase] = status


def _guarded(run):
    @wraps(run)
    def wrapper(self, context, argv):
        started = time.monotonic()
        action = run.__qualname__
        task_id = getattr(getattr(argv, "task_detail", None), "task_id", -1)
        if not isinstance(task_id, int):
            task_id = -1
        logger.info("云游戏操作开始 | action=%s | task_id=%s", action, task_id)
        success = False
        result, code, reason, error_type = "failed", "none", "returned_false", "none"
        source, line = "none", 0
        log_end = logger.error
        try:
            _check_stop(context)
            if _session is not None:
                _operation_deadline(_session, started + _session.transition_timeout, "cloud_game.transition_failed")
            success = bool(run(self, context, argv))
            if success:
                result, reason, log_end = "succeeded", "completed", logger.info
        except _CloudError as exc:
            code, reason = exc.key, exc.reason
            trace = exc.__traceback__
            while trace is not None:
                source, line = trace.tb_frame.f_code.co_name, trace.tb_lineno
                trace = trace.tb_next
            if exc.key == "cloud_game.stopped":
                result, log_end = "stopped", logger.info
            elif exc.key == "cloud_game.manual_enter":
                result, log_end = "manual_required", logger.warning
            if not context.tasker.stopping:
                PrintT(context, exc.key)
        except Exception as exc:
            # 仅提取类型和代码位置；不输出异常正文、堆栈局部变量或截图。
            code, reason, error_type = "cloud_game.failed", "unexpected_exception", type(exc).__name__
            trace = exc.__traceback__
            while trace is not None:
                source, line = trace.tb_frame.f_code.co_name, trace.tb_lineno
                trace = trace.tb_next
            if not context.tasker.stopping:
                PrintT(context, "cloud_game.failed")
        finally:
            log_end(
                "云游戏操作结束 | action=%s | task_id=%s | result=%s | elapsed_s=%.3f | code=%s | reason=%s | error=%s | source=%s:%d",
                action, task_id, result, time.monotonic() - started,
                code, reason, error_type, source, line,
            )
            if not success:
                cleanup_cloud_session()
            elif _session is not None:
                # 操作期限只约束本次回调；跨 Pipeline 阶段仅保留全局排队期限。
                _session.operation_deadline = None
        return CustomAction.RunResult(success=success)

    return wrapper


def _params(value):
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise _CloudError("cloud_game.invalid_config", "invalid_json") from None
    if not isinstance(value, dict):
        raise _CloudError("cloud_game.invalid_config", "config_not_object")
    return value


def _positive(params, key, default, maximum):
    value = params.get(key, default)
    if isinstance(value, bool):
        logger.debug("云游戏数值配置无效 | option=%s | reason=boolean_value", key)
        raise _CloudError("cloud_game.invalid_config", "invalid_numeric_option")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        logger.debug("云游戏数值配置无效 | option=%s | reason=not_numeric", key)
        raise _CloudError("cloud_game.invalid_config", "invalid_numeric_option") from None
    if not math.isfinite(value) or not 0 < value <= maximum:
        logger.debug("云游戏数值配置无效 | option=%s | reason=out_of_range | maximum=%s", key, maximum)
        raise _CloudError("cloud_game.invalid_config", "invalid_numeric_option")
    return value


def _state(argv):
    if _session is None or _session.task_id != argv.task_detail.task_id:
        raise _CloudError("cloud_game.invalid_config", "session_missing_or_task_mismatch")
    return _session


def _check_stop(context):
    if context.tasker.stopping:
        raise _CloudError("cloud_game.stopped")


def _check_budget(context):
    """检查当前操作及跨阶段排队预算，慢截图/OCR 后也不得接受迟到结果。"""
    _check_stop(context)
    state = _session
    if state is None:
        return
    now = time.monotonic()
    if state.queue_deadline is not None and now >= state.queue_deadline:
        raise _CloudError("cloud_game.queue_timeout", "queue_deadline_reached")
    if state.operation_deadline is not None and now >= state.operation_deadline:
        raise _CloudError(state.operation_timeout_key, "operation_deadline_reached")


def _operation_deadline(state, deadline, timeout_key):
    state.operation_deadline = deadline
    state.operation_timeout_key = timeout_key
    return min(deadline, state.queue_deadline) if state.queue_deadline is not None else deadline


def _pause(context, seconds, deadline):
    # 这是状态轮询节流，不是动作后的固定等待。停止信号最多等待 100 ms。
    end = min(time.monotonic() + seconds, deadline)
    while time.monotonic() < end:
        _check_stop(context)
        time.sleep(min(0.1, max(0.0, end - time.monotonic())))
    _check_stop(context)


def _capture(context):
    _check_budget(context)
    controller = context.tasker.controller
    if controller is None:
        raise _CloudError("cloud_game.client_missing", "controller_unavailable")
    if not controller.post_screencap().wait().succeeded:
        raise _CloudError("cloud_game.client_missing", "screencap_failed")
    _check_budget(context)
    image = controller.cached_image
    if not isinstance(image, np.ndarray) or image.size == 0:
        raise _CloudError("cloud_game.client_missing", "empty_frame")
    if (
        image.ndim != 3
        or image.shape[:2] != (720, 1280)
        or image.shape[2] not in (3, 4)
    ):
        logger.debug("云游戏截图尺寸不符 | shape=%s | expected=720x1280x3/4", image.shape)
        raise _CloudError("cloud_game.invalid_frame", "unexpected_frame_shape")
    return image


def _detail(context, node, image):
    _check_budget(context)
    detail = context.run_recognition(node, image)
    _check_budget(context)
    if detail is None:
        # 未运行识别不能等同于“弹窗不存在”；只记录内部节点名，不记录识别原文。
        logger.debug("云游戏识别未执行 | node=%s", node)
        raise _CloudError("cloud_game.recognition_failed", "recognition_not_executed")
    return detail


def _hit(context, node, image):
    return bool(_detail(context, node, image).hit)


def _error_check(context, image):
    if _hit(context, "CloudGameErrorState", image):
        raise _CloudError("cloud_game.failed", "client_error_screen")


def _first_hit(context, nodes, image):
    return next((node for node in nodes if _hit(context, node, image)), None)


_POST_LOGIN = (
    "CloudGameDailyLoginTitle",
    "CloudGameQueueScreen",
    "CloudGameLoading",
    "CloudGameGameLogin",
    "CloudGameInWorld",
    "CloudGameHome",
)


def _confirm_dialog(context):
    """启动确认弹窗在独立窗口里，必须同时命中时长提示和进入按钮。"""
    return (
        _find_owned_confirm(
            context,
            "CloudGameStartConfirmNotice",
            "CloudGameStartConfirmEnter",
        )
        is not None
    )


def _post_login_state(context, image):
    """短时确认窗口优先于主窗口底图，不重走整组慢 OCR。"""
    if _confirm_dialog(context):
        return "CloudGameStartConfirm"
    return _first_hit(context, _POST_LOGIN, image)


def _normalize_text(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text))).strip()


_QUEUE_LABEL = re.compile(
    r"^(?:目前正排在第|当前排队位置|當前排隊位置|预计等待(?:时间)?|預計等待(?:時間)?|"
    r"queue position|estimated wait(?: time)?|待ち時間|待機順位|대기 순서|예상 대기(?: 시간)?)[ :：]*(.*)$",
    re.I,
)
_QUEUE_VALUE = re.compile(
    r"^[\d\s.,:/+~～\-–—<>≤≥小时時分秒钟鐘人名位約约未満以上以内내분초시간명]*"
    r"(?:minutes?|mins?|seconds?|secs?|hours?|hrs?)?[\d\s.,:+~\-]*$", re.I,
)


def _queue_value(text):
    return bool(re.search(r"\d", text) and not re.search(r"\d{7}", text) and _QUEUE_VALUE.fullmatch(text))


def _queue_texts(detail):
    """仅补回已过滤队列标签旁的数值框，不播报全屏 OCR 原文。"""
    anchors = list(detail.filtered_results)
    results = list(getattr(detail, "all_results", None) or anchors)
    lines = []
    for anchor in anchors:
        text = _normalize_text(getattr(anchor, "text", ""))
        if not _QUEUE_LABEL.fullmatch(text):
            continue
        try:
            x, y, w, h = _box_values(anchor.box)
        except (AttributeError, _CloudError):
            lines.append(text)
            continue
        neighbors = []
        for candidate in results:
            if candidate is anchor:
                continue
            value = _normalize_text(getattr(candidate, "text", ""))
            if not (_queue_value(value) or value == "/"):
                continue
            try:
                cx, cy, cw, ch = _box_values(candidate.box)
            except (AttributeError, _CloudError):
                continue
            same_row = abs((cy + ch / 2) - (y + h / 2)) <= max(h, ch) * 0.6
            beside = same_row and x + w - 4 <= cx <= min(1280, x + w + 240)
            below = y + h <= cy <= y + h * 2.5 and x - h <= cx <= x + w
            if beside or below:
                neighbors.append((cy, cx, value))
        suffix = " ".join(value for _, _, value in sorted(neighbors))
        lines.append((text + " " + suffix).strip())
    return lines


def _queue_status(texts):
    """保留单位、排名分母和区间；标签和数值均用受限语法，拒绝自由文本。"""
    selected = []
    labelled = False
    for text in dict.fromkeys(_normalize_text(value) for value in texts if value):
        label = _QUEUE_LABEL.fullmatch(text)
        if label and (not label.group(1) or _queue_value(label.group(1))):
            labelled = True
            selected.append(text)
        elif labelled and _queue_value(text):
            selected.append(text)
    return " | ".join(selected)[:200]


def _report_queue(context, state, image):
    if not state.queue_reported:
        PrintT(context, "cloud_game.queue_started")
        state.queue_reported = True
    detail = _detail(context, "CloudGameQueueText", image)
    texts = _queue_texts(detail)
    status = _queue_status(texts)
    _log_diagnostic(
        ("queue_text",), (bool(status),),
        "云游戏队列提示识别 | available=%s", bool(status),
    )
    now = time.monotonic()
    if status and status != state.last_status and now - state.last_report_at >= 5:
        PrintT(context, "cloud_game.queue_status", escape(status))
        # 沿用用户播报的五秒节流，但文件日志不保存可能夹带账号信息的 OCR 文本。
        logger.debug("云游戏队列提示更新 | task_id=%s | text_count=%d", state.task_id, len(texts))
        state.last_status, state.last_report_at = status, now


def _begin_queue(state):
    if state.queue_deadline is None:
        state.queue_deadline = time.monotonic() + state.timeout_minutes * 60
        logger.info(
            "云游戏排队计时开始 | task_id=%s | timeout_s=%.1f",
            state.task_id, state.timeout_minutes * 60,
        )
    if time.monotonic() >= state.queue_deadline:
        raise _CloudError("cloud_game.queue_timeout", "queue_deadline_reached")


# 云客户端把启动确认等提示放在与主窗口完全重合的独立顶层窗口里。
# Win32 控制器只截取自己绑定的窗口，因此这类弹窗必须在 Python 侧单独取帧。
_CLOUD_TITLE = "云·异环"
_MAIN_CLASS = re.compile(r"^Qt\d+QWindow(?:Icon|OwnDC)?$", re.I)
_POPUP_CLASS = re.compile(r"^Qt\d+QWindowToolSaveBits(?:\w*)?$", re.I)
_GW_OWNER = 4
_GA_ROOT = 2
_BASE_WIDTH, _BASE_HEIGHT = 1280, 720


def _hwnd_value(hwnd):
    if isinstance(hwnd, int):
        return hwnd
    return int(getattr(hwnd, "value", 0) or 0)


def _init_win32_api(user32):
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    for name in ("IsWindow", "IsWindowVisible", "IsWindowEnabled"):
        function = getattr(user32, name)
        function.argtypes = [wintypes.HWND]
        function.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p


def _enumerate_cloud_windows():
    """按进程身份枚举可见云窗口；失败不能伪装为弹窗不存在。"""
    if sys.platform != "win32":
        return []
    from cloud_start import client_pids

    pids = client_pids()
    user32 = ctypes.windll.user32
    _init_win32_api(user32)
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        title, class_name = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(160)
        window_rect, client_rect = wintypes.RECT(), wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(window_rect)) or not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
            return True
        width, height = client_rect.right - client_rect.left, client_rect.bottom - client_rect.top
        if min(width, height) <= 0:
            return True
        user32.GetWindowTextW(hwnd, title, len(title))
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        windows.append(_WindowInfo(
            hwnd=_hwnd_value(hwnd),
            rect=(window_rect.left, window_rect.top, window_rect.right, window_rect.bottom),
            client_size=(width, height),
            owner=_hwnd_value(user32.GetWindow(hwnd, _GW_OWNER)),
            title=title.value, class_name=class_name.value, pid=pid.value,
        ))
        return True

    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous:
        raise _CloudError("cloud_game.client_missing")
    try:
        if not user32.EnumWindows(callback_type(visit), 0):
            raise _CloudError("cloud_game.recognition_failed")
    finally:
        user32.SetThreadDpiAwarenessContext(previous)
    return windows


def _same_rect(left, right):
    return tuple(left) == tuple(right)


def _owned_dialog_candidates():
    """按 owner 关系定位 Qt 弹窗；窗口可与启动器具有不同尺寸和位置。"""
    windows = _enumerate_cloud_windows()
    owners = [
        item for item in windows
        if item.title == _CLOUD_TITLE and _MAIN_CLASS.match(item.class_name)
    ]
    if _session is not None and _session.main_hwnd:
        owners = [item for item in owners if item.hwnd == _session.main_hwnd]
    if len(owners) != 1:
        raise _CloudError("cloud_game.client_missing", "main_window_not_unique")
    owner = owners[0]
    if _session is not None and _session.main_pid and owner.pid != _session.main_pid:
        raise _CloudError("cloud_game.client_missing", "main_process_changed")
    return [
        item for item in windows
        if item.hwnd != owner.hwnd
        and item.owner == owner.hwnd
        and item.pid == owner.pid
        and min(item.client_size) > 0
    ]


def _window_by_hwnd(hwnd):
    value = _hwnd_value(hwnd)
    return next(
        (item for item in _enumerate_cloud_windows() if item.hwnd == value),
        None,
    )


def _owned_dialog_rect():
    """返回唯一候选 owned 弹窗 (hwnd, 宽, 高)，供旧诊断脚本兼容。"""
    matches = _owned_dialog_candidates()
    if len(matches) != 1:
        return None
    item = matches[0]
    return item.hwnd, item.rect[2] - item.rect[0], item.rect[3] - item.rect[1]


def _release_owned_capture_controller():
    """兼容旧诊断入口；原生取帧工作进程在每次调用结束时已回收。"""
    return None


def cleanup_cloud_session():
    """仅释放本任务状态，不结束用户已有的云游戏会话。"""
    global _session
    if _session is not None:
        logger.debug(
            "云游戏会话清理 | task_id=%s | elapsed_s=%.3f | submitted_actions=%d",
            _session.task_id, time.monotonic() - _session.started_at, len(_session.clicked),
        )
    _clear_confirm_target()
    _session = None
    _diagnostics.clear()


def _normalize_owned_frame(image):
    if not isinstance(image, np.ndarray) or image.size == 0 or image.ndim != 3:
        return None
    if image.shape[2] not in (3, 4):
        return None
    image = image[:, :, :3]
    if image.shape[:2] == (_BASE_HEIGHT, _BASE_WIDTH):
        return image.copy()
    try:
        from PIL import Image

        rgb = Image.fromarray(image[:, :, ::-1], "RGB")
        rgb = rgb.resize((_BASE_WIDTH, _BASE_HEIGHT), Image.Resampling.LANCZOS)
        return np.asarray(rgb)[:, :, ::-1].copy()
    except Exception as exc:
        _log_diagnostic(
            ("owned_normalize",), (type(exc).__name__,),
            "云游戏弹窗归一化失败 | error=%s", type(exc).__name__,
        )
        return None


def _capture_owned_window(item, context=None):
    """一次取帧使用剩余预算；失败交回外层状态观察，不在内部重试。"""
    from utils.cloud_window import capture_window

    timeout = 3.0
    if context is not None:
        _check_budget(context)
        if _session is not None:
            deadlines = [value for value in (_session.operation_deadline, _session.queue_deadline) if value is not None]
            if deadlines:
                timeout = min(timeout, min(deadlines) - time.monotonic())
        if timeout <= 0:
            _check_budget(context)
            return None
    frame = _normalize_owned_frame(capture_window(
        item.hwnd, timeout=timeout,
        stopped=(lambda: context.tasker.stopping) if context is not None else None,
    ))
    if context is not None:
        _check_budget(context)
    _log_diagnostic(
        ("owned_capture", item.hwnd), (frame is not None,),
        "云游戏弹窗取帧结果 | available=%s | attempts=1", frame is not None,
    )
    return frame


def _owned_dialog_frame():
    """抓取唯一 owned 弹窗画面并归一化到 1280x720 BGR。"""
    matches = _owned_dialog_candidates()
    if len(matches) != 1:
        return None
    return _capture_owned_window(matches[0])


def _box_values(box):
    try:
        values = (
            list(box)
            if isinstance(box, (list, tuple))
            else [box.x, box.y, box.w, box.h]
        )
        if len(values) != 4 or any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError()
        x, y, width, height = values
        if (
            min(x, y) < 0
            or min(width, height) <= 0
            or x + width > _BASE_WIDTH
            or y + height > _BASE_HEIGHT
        ):
            raise ValueError()
        return x, y, width, height
    except (AttributeError, ValueError, TypeError):
        raise _CloudError("cloud_game.ambiguous_target") from None


def _clear_confirm_target():
    global _confirm_target
    _confirm_target = None
    if _session is not None:
        _session.confirm_target = None


def _save_confirm_target(item, box):
    global _confirm_target
    target = _ConfirmTarget(
        hwnd=item.hwnd,
        owner=item.owner,
        rect=item.rect,
        client_size=item.client_size,
        box=_box_values(box),
        pid=item.pid,
    )
    _confirm_target = target
    if _session is not None:
        _session.confirm_target = target
    return target


def _recognize_owned_confirm(context, item, frame, screen, target):
    key = ("owned_probe", item.hwnd, screen, target)
    try:
        notice = _detail(context, screen, frame)
        button = _detail(context, target, frame)
    except _CloudError:
        raise
    except Exception as exc:
        _log_diagnostic(
            key, ("exception", type(exc).__name__),
            "云游戏弹窗识别异常 | error=%s", type(exc).__name__,
        )
        return None
    notice_hit = None if notice is None else notice.hit
    button_hit = None if button is None else button.hit
    button_count = 0 if button is None else len(button.filtered_results)
    _log_diagnostic(
        key, (notice_hit, button_hit, button_count),
        "云游戏弹窗识别结果 | notice=%s | button=%s | button_count=%d",
        notice_hit, button_hit, button_count,
    )
    if notice is None or not notice.hit:
        return None
    if button is None or not button.hit or len(button.filtered_results) != 1:
        return None
    try:
        return _save_confirm_target(item, button.box)
    except _CloudError:
        return None


def _find_owned_confirm(context, screen, target, expected_hwnd=None):
    matches = []
    _check_budget(context)
    candidates = _owned_dialog_candidates()
    if len(candidates) != 1 or (expected_hwnd is not None and candidates[0].hwnd != expected_hwnd):
        _clear_confirm_target()
        return None
    for item in candidates:
        frame = _capture_owned_window(item, context)
        if frame is None:
            continue
        result = _recognize_owned_confirm(context, item, frame, screen, target)
        if result is not None:
            matches.append(result)
    _log_diagnostic(
        ("owned_confirm", screen, target, expected_hwnd),
        (tuple(item.hwnd for item in candidates), len(matches)),
        "云游戏弹窗目标检查 | candidates=%d | matched=%d | locked=%s",
        len(candidates), len(matches), expected_hwnd is not None,
    )
    if len(matches) != 1:
        if expected_hwnd is None:
            _clear_confirm_target()
        return None
    return matches[0]


@AgentServer.custom_recognition("cloud_game_owned_dialog")
class CloudGameOwnedDialog(CustomRecognition):
    """识别独立 owned 弹窗中的按钮，并返回可直接点击的 720p 坐标。"""

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        try:
            _check_stop(context)
            params = _params(argv.custom_recognition_param)
            screen, target = params.get("screen"), params.get("target")
            if not isinstance(screen, str) or not isinstance(target, str):
                _log_diagnostic(
                    ("owned_analysis",), ("invalid_config",),
                    "云游戏弹窗识别未执行 | reason=invalid_recognition_nodes",
                )
                return None
            result = _find_owned_confirm(context, screen, target)
            _log_diagnostic(
                ("owned_analysis",), (result is not None,),
                "云游戏弹窗识别完成 | matched=%s", result is not None,
            )
            if result is None:
                return None
            return CustomRecognition.AnalyzeResult(box=list(result.box), detail={})
        except Exception as exc:
            _clear_confirm_target()
            _log_diagnostic(
                ("owned_analysis",), ("exception", type(exc).__name__),
                "云游戏弹窗识别失败 | error=%s", type(exc).__name__,
            )
            return None


def _prepare_cloud_client(context, state):
    from cloud_start import (
        client_windows,
        desktop_interactive,
        prepare_client_window,
        window_responding,
    )

    # 点击走后台消息投递、截图走 PrintWindow，均不依赖活动桌面；这里只记录状态便于排查。
    logger.info(
        "云游戏客户端检查开始 | task_id=%s | desktop_interactive=%s",
        state.task_id, desktop_interactive(),
    )
    windows = client_windows()
    logger.debug("云游戏主窗口检查 | candidates=%d", len(windows))
    if len(windows) != 1:
        raise _CloudError(
            "cloud_game.client_missing" if not windows else "cloud_game.ambiguous_target",
            "no_client_window" if not windows else "multiple_client_windows",
        )
    window = windows[0]
    if not window_responding(window.hwnd):
        raise _CloudError("cloud_game.client_unresponsive", "window_not_responding")
    if not prepare_client_window(window.hwnd):
        raise _CloudError("cloud_game.screen_too_small", "window_preparation_failed")
    _log_state(state, "client", "waiting_for_frame")
    deadline = time.monotonic() + state.transition_timeout
    while time.monotonic() < deadline:
        _check_stop(context)
        controller = context.tasker.controller
        captured = controller is not None and controller.post_screencap().wait().succeeded
        image = controller.cached_image if captured else None
        shape = image.shape if isinstance(image, np.ndarray) else None
        _log_diagnostic(
            ("main_frame",), (bool(captured), shape),
            "云游戏主窗口取帧检查 | captured=%s | shape=%s", bool(captured), shape,
        )
        if isinstance(image, np.ndarray) and image.ndim == 3 and image.shape[:2] == (720, 1280):
            state.main_hwnd = window.hwnd
            state.main_pid = window.pid
            _log_state(state, "client", "ready")
            PrintT(context, "cloud_game.client_ready")
            return
        _pause(context, 0.2, deadline)
    raise _CloudError("cloud_game.invalid_frame", "frame_ready_timeout")


_LOGIN_HINTS = (
    ("CloudGameLoginFailure", "cloud_game.login_retry_required"),
    ("CloudGameVerification", "cloud_game.verification_required"),
    ("CloudGameAuthorization", "cloud_game.authorization_required"),
    ("CloudGameLoginForm", "cloud_game.login_required"),
)


def _login_obstacle(context, image):
    """只有全局唯一且没有未知遮挡的记住账号窗口可提交；凭据始终交给用户。"""
    unknown_overlay = False
    known_overlay = False
    submits = []
    for item in _owned_dialog_candidates():
        frame = _capture_owned_window(item, context)
        if frame is None:
            unknown_overlay = True
            continue
        for node, message in _LOGIN_HINTS:
            if _hit(context, node, frame):
                return message, None
        if _hit(context, "CloudGameStartConfirmNotice", frame) or _hit(context, "CloudGameDailyLoginTitle", frame):
            # 这些窗口交给 Pipeline，但不能与记住账号窗口同时存在时仍向后者输入。
            known_overlay = True
            continue
        if _hit(context, "CloudGameLoginOtherMethods", frame):
            button = _detail(context, "CloudGameLoginSubmit", frame)
            if button.hit and len(button.filtered_results) == 1:
                submits.append(_save_confirm_target(item, button.box))
                continue
        unknown_overlay = True
    for node, message in _LOGIN_HINTS:
        if _hit(context, node, image):
            return message, None
    if len(submits) == 1 and not unknown_overlay and not known_overlay:
        return "cloud_game.login_remembered", submits[0]
    if submits or unknown_overlay:
        return "cloud_game.login_required", None
    return None, None


@AgentServer.custom_action("cloud_game_reset")
class CloudGameReset(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        global _session
        _session = None
        _diagnostics.clear()
        _clear_confirm_target()
        _release_owned_capture_controller()
        if controller_name() != "CloudGame-Front":
            raise _CloudError("cloud_game.wrong_controller")
        params = _params(argv.custom_action_param)
        auto_config = context.get_node_data("CloudGameAutoQueueConfig") or {}
        time_config = context.get_node_data("CloudGameQueueTimeoutConfig") or {}
        auto_queue = _params(auto_config.get("attach")).get("auto_queue", True)
        if not isinstance(auto_queue, bool):
            raise _CloudError("cloud_game.invalid_config")
        state = _Session(
            task_id=argv.task_detail.task_id,
            auto_queue=auto_queue,
            timeout_minutes=_positive(
                _params(time_config.get("attach")), "timeout_minutes", 30, 1440
            ),
            poll_interval=_positive(params, "poll_interval_seconds", 2, 5),
            login_timeout=_positive(params, "login_timeout_seconds", 600, 3600),
            transition_timeout=_positive(params, "transition_timeout_seconds", 30, 120),
        )
        profile = context.get_node_data("CloudGameProfile") or {}
        if profile.get("attach", {}).get("calibrated") is not True:
            raise _CloudError("cloud_game.calibration_required")
        # 分支和参数一起设置，避免仅禁用 action 节点导致登录检测也被禁用。
        if not context.override_pipeline(
            {"CloudGameManualEnterNotice": {"enabled": not auto_queue}}
        ):
            raise _CloudError("cloud_game.invalid_config")
        logger.debug(
            "云游戏会话配置 | auto_queue=%s | queue_timeout_s=%.1f | login_timeout_s=%.1f | transition_timeout_s=%.1f | poll_interval_s=%.1f",
            state.auto_queue, state.timeout_minutes * 60, state.login_timeout,
            state.transition_timeout, state.poll_interval,
        )
        _prepare_cloud_client(context, state)
        _session = state
        return True


@AgentServer.custom_action("cloud_game_wait_login")
class CloudGameWaitLogin(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        state = _state(argv)
        if state.login_deadline is None:
            state.login_deadline = time.monotonic() + state.login_timeout
        deadline = _operation_deadline(state, state.login_deadline, "cloud_game.login_timeout")
        if not context.override_next(argv.node_name, ["CloudGameDispatch"]):
            raise _CloudError("cloud_game.invalid_config", "login_route_failed")
        logger.debug("云游戏登录等待参数 | timeout_s=%.1f | remaining_s=%.1f | poll_interval_s=%.1f",
                     state.login_timeout, max(0.0, deadline - time.monotonic()), state.poll_interval)
        prompted = set()
        stable = 0

        def prompt(key):
            if key not in prompted:
                PrintT(context, key)
                prompted.add(key)

        while time.monotonic() < deadline:
            image = _capture(context)
            _error_check(context, image)
            if state.auto_queue and _confirm_dialog(context):
                _check_budget(context)
                _log_state(state, "login", "CloudGameStartConfirm")
                return context.override_next(argv.node_name, ["CloudGameStartConfirm"])
            obstacle, submit = _login_obstacle(context, image)
            if submit is not None and "login_submit" not in state.clicked:
                # 点击前重新识别所有候选；不能复用切换到凭据表单之前的目标。
                fresh_hint, fresh = _login_obstacle(context, _capture(context))
                same = fresh is not None and (fresh.hwnd, fresh.owner, fresh.pid, fresh.client_size) == (
                    submit.hwnd, submit.owner, submit.pid, submit.client_size)
                _check_budget(context)
                if same:
                    _log_state(state, "login", "cloud_game.login_remembered")
                    prompt(obstacle)
                    state.clicked.add("login_submit")
                    logger.info("云游戏单次点击 | kind=login_submit | target=owned_window")
                    _click_owned_target(fresh, context)
                else:
                    prompt(fresh_hint or "cloud_game.login_required")
                stable = 0
            elif obstacle:
                stable = 0
                status = "cloud_game.login_required" if submit is not None else obstacle
                _log_state(state, "login", status)
                prompt(status)
            elif _hit(context, "CloudGameLoginScreen", image):
                stable = 0
                _log_state(state, "login", "cloud_game.login_required")
                prompt("cloud_game.login_required")
                if not state.clicked & {"login", "login_submit"}:
                    point = _single_box(_detail(context, "CloudGameLoginScreen", image))
                    state.clicked.add("login")
                    logger.info("云游戏单次点击 | kind=login | target=main_window")
                    _click_main_window(context, state, point)
            elif login_state := _first_hit(context, _POST_LOGIN, image):
                _log_state(state, "login", login_state)
                stable += 1
                if stable >= 2:
                    _check_budget(context)
                    _clear_confirm_target()
                    logger.info("云游戏登录已确认 | evidence=%s | stable_frames=%d", login_state, stable)
                    PrintT(context, "cloud_game.login_detected")
                    return True
            else:
                stable = 0
                _log_state(state, "login", "unknown_or_overlay")
                prompt("cloud_game.login_required")
            _pause(context, state.poll_interval, deadline)
        raise _CloudError("cloud_game.login_timeout", "login_deadline_reached")


@AgentServer.custom_action("cloud_game_queue_wait")
class CloudGameQueueWait(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        state = _state(argv)
        if not state.auto_queue:
            raise _CloudError("cloud_game.manual_enter", "auto_queue_disabled")
        _begin_queue(state)
        _operation_deadline(state, state.queue_deadline, "cloud_game.queue_timeout")
        if not context.override_next(argv.node_name, ["CloudGameDispatch"]):
            raise _CloudError("cloud_game.invalid_config", "queue_route_failed")
        logger.debug(
            "云游戏排队等待参数 | remaining_s=%.1f | poll_interval_s=%.1f | transition_timeout_s=%.1f",
            max(0.0, state.queue_deadline - time.monotonic()), state.poll_interval, state.transition_timeout,
        )
        unknown_since = None
        while time.monotonic() < state.queue_deadline:
            image = _capture(context)
            _error_check(context, image)
            # 返回 Pipeline 处理弹窗/确认框/游戏登录，不在 Python 内点击或跑整条流程。
            if _hit(context, "CloudGameDailyLoginTitle", image):
                _log_state(state, "queue", "CloudGameDailyLoginTitle")
                return True
            if _confirm_dialog(context):
                _log_state(state, "queue", "CloudGameStartConfirm")
                return context.override_next(argv.node_name, ["CloudGameStartConfirm"])
            if _hit(context, "CloudGameLoginScreen", image):
                _log_state(state, "queue", "cloud_game.login_required")
                raise _CloudError("cloud_game.login_required", "login_expired_during_queue")
            next_state = _first_hit(context, ("CloudGameGameLogin", "CloudGameInWorld"), image)
            if next_state:
                _log_state(state, "queue", next_state)
                return True
            if _hit(context, "CloudGameQueueScreen", image):
                unknown_since = None
                _log_state(state, "queue", "CloudGameQueueScreen")
                _report_queue(context, state, image)
            elif _hit(context, "CloudGameLoading", image):
                unknown_since = None
                _log_state(state, "queue", "CloudGameLoading")
                if not state.loading_reported:
                    PrintT(context, "cloud_game.loading")
                    state.loading_reported = True
            else:
                _log_state(state, "queue", "unknown_or_black_screen")
                if unknown_since is None:
                    unknown_since = time.monotonic()
                # 云端启动常有较长黑屏（客户端文案：“游戏启动中，黑屏时间可能较长”），
                # 属于预期过渡态；容忍时长由 transition_timeout 控制（云入口已放宽）。
                if time.monotonic() - unknown_since >= state.transition_timeout:
                    raise _CloudError("cloud_game.unknown_state", "unknown_queue_state_timeout")
            _pause(context, state.poll_interval, state.queue_deadline)
        raise _CloudError("cloud_game.queue_timeout", "queue_deadline_reached")


def _box_center(box):
    x, y, width, height = _box_values(box)
    return x + width // 2, y + height // 2


def _single_box(detail):
    if not detail.hit or len(detail.filtered_results) != 1:
        logger.debug(
            "云游戏点击目标不唯一 | hit=%s | candidates=%d",
            detail.hit, len(detail.filtered_results),
        )
        raise _CloudError("cloud_game.ambiguous_target", "click_target_not_unique")
    return _box_center(detail.box)


def _map_720p_to_client(point, client_size):
    x, y = point
    width, height = client_size
    if width <= 0 or height <= 0:
        raise _CloudError("cloud_game.ambiguous_target")
    mapped_x = round(x * width / _BASE_WIDTH)
    mapped_y = round(y * height / _BASE_HEIGHT)
    return (
        max(0, min(width - 1, mapped_x)),
        max(0, min(height - 1, mapped_y)),
    )


def _is_window_visible(hwnd):
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    _init_win32_api(user32)
    try:
        return bool(user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def _main_window_info(state):
    """按会话锁定的 HWND 复核主窗口身份，不接受同进程的其他窗口。"""
    hwnd = state.main_hwnd if state is not None else 0
    if not hwnd:
        return None
    item = _window_by_hwnd(hwnd)
    if (
        item is None
        or item.title != _CLOUD_TITLE
        or not _MAIN_CLASS.match(item.class_name)
    ):
        return None
    return item


def _native_click(hwnd, point):
    """向目标窗口后台投递一次左键单击，不激活窗口、不移动真实鼠标。

    click_window 内部会校验窗口可见、未最小化、可用、是顶层窗口且坐标在客户区内，
    任一不符直接拒绝，不会盲发；这里只汇报成功与否，由调用方决定错误语义。
    """
    from utils.cloud_window import click_window

    accepted = bool(click_window(hwnd, point))
    logger.debug("云游戏后台点击结束 | accepted=%s", accepted)
    return accepted


def _click_main_window(context, state, point):
    """主窗口点击同样走后台消息投递，而不是控制器的 Seize 合成点击。

    实机取证：未激活的 Qt OpenGL 窗口会丢弃控制器的 SendInput 合成输入；
    而直接投递到窗口的 WM_LBUTTON* 消息在桌面断开时也能被执行。
    """
    item = _main_window_info(state)
    if item is None:
        raise _CloudError("cloud_game.client_missing")
    _check_budget(context)
    if _owned_dialog_candidates():
        raise _CloudError("cloud_game.overlay_blocked", "owned_overlay_blocks_main_input")
    target = _map_720p_to_client(point, item.client_size)
    _check_budget(context)
    if not _native_click(item.hwnd, target):
        raise _CloudError("cloud_game.input_rejected")
    return True


def _target_window_info(target):
    item = _window_by_hwnd(target.hwnd)
    if item is None:
        return None
    # 弹窗身份用 hwnd + owner + 进程 + 类名 + 客户区尺寸确认即可；窗口的屏幕位置
    # 在弹出动画期间会小幅漂移，不能据此判定为“不同窗口”（点击坐标用当前窗口的
    # ClientToScreen 实时换算，与保存时的 rect 无关）。
    owner_ok = item.owner == target.owner
    process_ok = not target.pid or item.pid == target.pid
    class_ok = bool(_POPUP_CLASS.match(item.class_name))
    size_ok = item.client_size == target.client_size
    _log_diagnostic(
        ("target_identity", target.hwnd), (owner_ok, process_ok, class_ok, size_ok),
        "云游戏弹窗身份检查 | owner_match=%s | process_match=%s | class_match=%s | size_match=%s",
        owner_ok, process_ok, class_ok, size_ok,
    )
    if not (owner_ok and process_ok and class_ok and size_ok):
        return None
    return item


def _click_owned_target(target, context=None):
    """锁定已识别的 HWND 与几何信息；失败不回退到主窗口输入。"""
    candidates = _owned_dialog_candidates()
    if len(candidates) != 1 or candidates[0].hwnd != target.hwnd:
        raise _CloudError("cloud_game.ambiguous_target", "owned_target_not_in_candidates")
    item = _target_window_info(target)
    if item is None:
        raise _CloudError("cloud_game.ambiguous_target", "owned_target_identity_changed")
    point = _map_720p_to_client(_box_center(target.box), item.client_size)
    if context is not None:
        _check_budget(context)
    if not _native_click(item.hwnd, point):
        raise _CloudError("cloud_game.input_rejected", "owned_window_input_rejected")
    return True


def _confirm_target_state(target):
    """返回 present/gone/invalid/ambiguous，禁止把取帧失败当成弹窗消失。"""
    candidates = _owned_dialog_candidates()
    item = _window_by_hwnd(target.hwnd)
    if item is not None:
        return "present" if _target_window_info(target) is not None else "invalid"
    if _is_window_visible(target.hwnd):
        return "invalid"
    if candidates:
        return "ambiguous"
    return "gone"


@AgentServer.custom_action("cloud_game_click")
class CloudGameClick(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        state = _state(argv)
        kind = _params(argv.custom_action_param).get("kind")
        # 客户端不存在队列选择页，因此没有 queue 类型：进入游戏后直接排队。
        choices = {
            "enter": ("CloudGameHome", "CloudGameEnterText"),
            "confirm": (None, None),
            "daily": ("CloudGameDailyLoginTitle", "CloudGameDailyLoginCloseButton"),
        }
        if kind not in choices:
            raise _CloudError("cloud_game.invalid_config", "unsupported_click_kind")
        if kind != "daily" and not state.auto_queue:
            raise _CloudError("cloud_game.manual_enter", "auto_queue_disabled")
        if kind in state.clicked:
            raise _CloudError("cloud_game.transition_failed", "duplicate_click_blocked")
        logger.info("云游戏单次操作 | kind=%s", kind)
        if kind == "confirm":
            return self._confirm(context, argv, state)
        source, target = choices[kind]
        phase = "click_" + kind
        image = _capture(context)
        _error_check(context, image)
        if not _hit(context, source, image):
            raise _CloudError("cloud_game.transition_failed", "source_state_changed")
        if kind != "daily" and _hit(context, "CloudGameDailyLoginTitle", image):
            raise _CloudError("cloud_game.transition_failed", "daily_popup_blocks_input")
        if _hit(context, "CloudGameLoginScreen", image):
            raise _CloudError("cloud_game.login_required", "login_expired_before_click")
        _log_state(state, phase, source)
        detail = _detail(context, target, image)
        if kind == "daily":
            # OCR 文本只是“点击空白区域关闭”的存在性证据，不能点击文字本身。
            # 奖励卡片与提示均位于画面中央，左侧中部是实机验证的遮罩空白区。
            _single_box(detail)
            x, y = 240, 360
        else:
            x, y = _single_box(detail)
            _begin_queue(state)
        state.clicked.add(kind)
        _click_main_window(context, state, (x, y))
        logger.debug("云游戏单次点击已发送 | kind=%s | target=%s", kind, target)
        # 每个按钮只发一次点击；随后持续截图验证状态变化，绝不重复输入。
        deadline = time.monotonic() + state.transition_timeout
        if state.queue_deadline is not None:
            deadline = min(deadline, state.queue_deadline)
        stable = 0
        while time.monotonic() < deadline:
            image = _capture(context)
            _error_check(context, image)
            if kind == "daily":
                gone = not _hit(context, "CloudGameDailyLoginTitle", image)
                known = (
                    _first_hit(
                        context, _POST_LOGIN[1:] + ("CloudGameLoginScreen",), image
                    )
                    or (_confirm_dialog(context) and "CloudGameStartConfirm")
                    if gone
                    else None
                )
                _log_state(state, phase, known or ("unknown" if gone else source))
                stable = stable + 1 if known else 0
                if stable >= 2:
                    PrintT(context, "cloud_game.daily_login_closed")
                    return True
            else:
                # 确认弹窗是独立窗口，主窗口仍显示首页，因此它不需要 source 消失。
                if kind == "enter" and _confirm_dialog(context):
                    _log_state(state, phase, "CloudGameStartConfirm")
                    # 弹窗自带 30 秒倒计时。实机取证（2026-09-26）：返回分派节点后，
                    # 排在前面的全图 OCR 节点每个 6-8 秒，轮到确认时弹窗已关闭。
                    # 因此在同一动作内立即确认，确认仍只点一次、仍校验同一弹窗。
                    return self._confirm(context, argv, state)
                candidates = tuple(node for node in _POST_LOGIN[:-1] if node != source)
                # 来源界面仍在时，背景中的同类文字不是点击成功证据。
                known = _first_hit(context, candidates, image) if not _hit(context, source, image) else None
                _log_state(state, phase, known or "waiting_for_transition")
                if known:
                    return True
            _pause(context, state.poll_interval, deadline)
        if (
            state.queue_deadline is not None
            and time.monotonic() >= state.queue_deadline
        ):
            raise _CloudError("cloud_game.queue_timeout", "queue_deadline_reached")
        raise _CloudError(
            "cloud_game.daily_login_close_failed"
            if kind == "daily"
            else "cloud_game.transition_failed",
            "click_transition_timeout",
        )

    def _confirm(self, context, argv, state):
        """锁定识别时的 owned HWND，点击同一窗口并验证该窗口确实消失。"""
        target = state.confirm_target or _confirm_target
        if target is None:
            target = _find_owned_confirm(
                context,
                "CloudGameStartConfirmNotice",
                "CloudGameStartConfirmEnter",
            )
        if target is None:
            raise _CloudError("cloud_game.transition_failed", "confirmation_target_missing")
        fresh = _find_owned_confirm(
            context, "CloudGameStartConfirmNotice", "CloudGameStartConfirmEnter",
            expected_hwnd=target.hwnd,
        )
        # 重新在同一 owned 弹窗上识别到进入按钮即可；不比较屏幕 rect（弹窗动画期间
        # 会漂移），只校验客户区尺寸一致，避免误判为 ambiguous。
        if fresh is None or fresh.client_size != target.client_size:
            raise _CloudError("cloud_game.ambiguous_target", "confirmation_target_changed")
        target = fresh
        _log_state(state, "click_confirm", "target_verified")
        _begin_queue(state)
        _check_stop(context)
        _click_owned_target(target, context)
        state.clicked.add("confirm")
        logger.debug("云游戏单次点击已发送 | kind=confirm | target=owned_window")
        deadline = min(
            time.monotonic() + state.transition_timeout, state.queue_deadline
        )
        while time.monotonic() < deadline:
            # 仍需读取主窗口：用于错误/登录失效判断，也提供弹窗关闭后的正向证据。
            image = _capture(context)
            _error_check(context, image)
            if _hit(context, "CloudGameLoginScreen", image):
                raise _CloudError("cloud_game.login_required", "login_expired_after_confirm")
            target_state = _confirm_target_state(target)
            _log_state(state, "confirm_window", target_state)
            # target_state 不是 gone 时一律继续等待，不能把取帧/枚举失败当成关闭成功。
            if target_state == "gone":
                # 确认弹窗关闭即代表“进入游戏”点击生效，随后是云端加载（可能长时间
                # 黑屏，无任何可识别文案）。出现排队/加载/游戏登录/大世界等后续状态，
                # 或主窗口已离开首页（进入黑屏加载），都视为过渡成功，交由分派与排队
                # 等待处理较长的加载，避免在此处 30s 过渡超时内误判失败。
                next_state = _first_hit(context, _POST_LOGIN[:-1], image)
                if next_state or not _hit(context, "CloudGameHome", image):
                    _log_state(state, "click_confirm", next_state or "unknown_or_black_screen")
                    _clear_confirm_target()
                    return True
            _pause(context, state.poll_interval, deadline)
        if time.monotonic() >= state.queue_deadline:
            raise _CloudError("cloud_game.queue_timeout", "queue_deadline_reached")
        raise _CloudError("cloud_game.transition_failed", "confirmation_transition_timeout")


@AgentServer.custom_action("cloud_game_handoff")
class CloudGameHandoff(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        from cloud_start import desktop_interactive

        state = _state(argv)
        image = _capture(context)
        _error_check(context, image)
        if _owned_dialog_candidates():
            raise _CloudError("cloud_game.overlay_blocked", "owned_overlay_blocks_handoff")
        if _hit(context, "CloudGameInWorld", image):
            _log_state(state, "handoff", "already_in_world")
            return True  # 仍交给 CloudGameReady 做两帧及遮挡确认。
        if not _hit(context, "CloudGameGameLogin", image):
            raise _CloudError("cloud_game.transition_failed", "game_login_changed_before_handoff")
        if not desktop_interactive():
            raise _CloudError("cloud_game.desktop_required", "interactive_desktop_required_for_scene_input")
        _check_budget(context)
        _log_state(state, "handoff", "scene_started")
        # Maa 5.10.4 的同步公共场景调用不能安全地从 Agent 并发取消。
        # 此处只能拒绝迟到结果；内部加载自循环的硬截止仍需框架/公共场景接口支持。
        # 不并发 post_stop（会竞争同一 IPC 通道），也不改写私有场景节点。
        result = context.run_task("SceneAnyEnterWorld")
        _check_budget(context)
        if result is None or not result.status.succeeded:
            raise _CloudError("cloud_game.transition_failed", "scene_handoff_failed")
        _log_state(state, "handoff", "scene_returned")
        return True


@AgentServer.custom_action("cloud_game_confirm_ready")
class CloudGameConfirmReady(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        state = _state(argv)
        deadline = _operation_deadline(state, time.monotonic() + state.transition_timeout, "cloud_game.transition_failed")
        stable = 0
        warned = False
        while time.monotonic() < deadline:
            image = _capture(context)
            _error_check(context, image)
            candidates = _owned_dialog_candidates()
            if candidates:
                stable = 0
                if state.auto_queue and _confirm_dialog(context):
                    return context.override_next(argv.node_name, ["CloudGameStartConfirm"])
                _log_state(state, "ready", "owned_overlay")
                if not warned:
                    PrintT(context, "cloud_game.overlay_blocked")
                    warned = True
                _pause(context, state.poll_interval, deadline)
                continue
            if _hit(context, "CloudGameDailyLoginTitle", image):
                _log_state(state, "ready", "CloudGameDailyLoginTitle")
                return context.override_next(argv.node_name, ["CloudGameDispatch"])
            if _hit(context, "CloudGameLoginScreen", image):
                raise _CloudError("cloud_game.login_required", "login_expired_before_ready")
            blocked = _first_hit(context, ("CloudGameQueueScreen", "CloudGameLoading"), image)
            in_world = not blocked and _hit(context, "CloudGameInWorld", image)
            _log_state(state, "ready", blocked or ("CloudGameInWorld" if in_world else "unknown"))
            stable = stable + 1 if in_world else 0
            if stable >= 2:
                _check_budget(context)
                if _owned_dialog_candidates():
                    stable = 0
                    continue
                logger.info("云游戏大世界已确认 | stable_frames=%d", stable)
                return context.override_next(argv.node_name, ["CloudGameStartDone"])
            _pause(context, state.poll_interval, deadline)
        _check_budget(context)
        raise _CloudError("cloud_game.transition_failed", "world_ready_timeout")


@AgentServer.custom_action("cloud_game_fail")
class CloudGameFail(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        stopped = context.tasker.stopping
        log_end = logger.info if stopped else logger.error
        log_end(
            "云游戏流程结束 | result=%s | reason=%s",
            "stopped" if stopped else "failed",
            "task_stopping" if stopped else "pipeline_failed",
        )
        cleanup_cloud_session()
        return CustomAction.RunResult(success=False)


@AgentServer.custom_action("cloud_game_finish")
class CloudGameFinish(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        logger.info(
            "云游戏流程结束 | result=succeeded | auto_queue=%s | elapsed_s=%.3f",
            _session.auto_queue if _session is not None else None,
            time.monotonic() - _session.started_at if _session is not None else 0.0,
        )
        cleanup_cloud_session()
        return CustomAction.RunResult(success=True)
