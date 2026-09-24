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


_session: _Session | None = None
_confirm_target: _ConfirmTarget | None = None


class _CloudError(Exception):
    def __init__(self, key="cloud_game.failed"):
        self.key = key
        super().__init__(key)


def _guarded(run):
    @wraps(run)
    def wrapper(self, context, argv):
        try:
            _check_stop(context)
            success = bool(run(self, context, argv))
            if not success:
                cleanup_cloud_session()
            return CustomAction.RunResult(success=success)
        except _CloudError as exc:
            if not context.tasker.stopping:
                PrintT(context, exc.key)
            logger.debug("云启动动作 %s 结束: %s", argv.node_name, exc.key)
        except Exception as exc:
            # 不记录参数、OCR 原文或异常正文，避免输出登录页中的敏感内容。
            logger.error("云启动动作 %s 异常: %s", argv.node_name, type(exc).__name__)
            if not context.tasker.stopping:
                PrintT(context, "cloud_game.failed")
        cleanup_cloud_session()
        return CustomAction.RunResult(success=False)

    return wrapper


def _params(value):
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise _CloudError("cloud_game.invalid_config") from None
    if not isinstance(value, dict):
        raise _CloudError("cloud_game.invalid_config")
    return value


def _positive(params, key, default, maximum):
    value = params.get(key, default)
    if isinstance(value, bool):
        raise _CloudError("cloud_game.invalid_config")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        raise _CloudError("cloud_game.invalid_config") from None
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise _CloudError("cloud_game.invalid_config")
    return value


def _state(argv):
    if _session is None or _session.task_id != argv.task_detail.task_id:
        raise _CloudError("cloud_game.invalid_config")
    return _session


def _check_stop(context):
    if context.tasker.stopping:
        raise _CloudError("cloud_game.stopped")


def _pause(context, seconds, deadline):
    # 这是状态轮询节流，不是动作后的固定等待。停止信号最多等待 100 ms。
    end = min(time.monotonic() + seconds, deadline)
    while time.monotonic() < end:
        _check_stop(context)
        time.sleep(min(0.1, max(0.0, end - time.monotonic())))
    _check_stop(context)


def _capture(context):
    _check_stop(context)
    controller = context.tasker.controller
    if controller is None or not controller.post_screencap().wait().succeeded:
        raise _CloudError("cloud_game.client_missing")
    _check_stop(context)
    image = controller.cached_image
    if not isinstance(image, np.ndarray) or image.size == 0:
        raise _CloudError("cloud_game.client_missing")
    if (
        image.ndim != 3
        or image.shape[:2] != (720, 1280)
        or image.shape[2] not in (3, 4)
    ):
        raise _CloudError("cloud_game.invalid_frame")
    return image


def _detail(context, node, image):
    _check_stop(context)
    detail = context.run_recognition(node, image)
    if detail is None:
        # 未运行识别不能等同于“弹窗不存在”。
        raise _CloudError("cloud_game.recognition_failed")
    return detail


def _hit(context, node, image):
    return bool(_detail(context, node, image).hit)


def _error_check(context, image):
    if _hit(context, "CloudGameErrorState", image):
        raise _CloudError()


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
    """返回已登录证据，兼顾主窗口状态与独立确认弹窗。"""
    node = _first_hit(context, _POST_LOGIN, image)
    if node:
        return node
    return "CloudGameStartConfirm" if _confirm_dialog(context) else None


def _normalize_text(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text))).strip()


def _queue_status(texts):
    """保留原始单位和区间，不把排名、人数或 OCR 乱码猜成分钟数。"""
    lines = list(dict.fromkeys(_normalize_text(text) for text in texts if text))
    # 仅播报队列标签或完整数值/时间行，不用任意 OCR 原文兜底。
    label = re.compile(
        r"排[队隊]|等待|预[计估]|預[計估]|位置|queue|wait|position|待ち|待機|대기|예상",
        re.I,
    )
    value = re.compile(
        r"^[\d\s.,:+~～\-–—<>≤≥小时時分秒钟鐘人名位約约未満以上以内내분초시간명]*(?:minutes?|mins?|seconds?|secs?|hours?|hrs?)?[\d\s.,:+~\-]*$",
        re.I,
    )
    selected = [
        line
        for line in lines
        if label.search(line) or (re.search(r"\d", line) and value.fullmatch(line))
    ]
    return " | ".join(selected)[:200]


def _report_queue(context, state, image):
    if not state.queue_reported:
        PrintT(context, "cloud_game.queue_started")
        state.queue_reported = True
    detail = _detail(context, "CloudGameQueueText", image)
    texts = [getattr(result, "text", "") for result in detail.filtered_results]
    status = _queue_status(texts)
    now = time.monotonic()
    if status and status != state.last_status and now - state.last_report_at >= 5:
        PrintT(context, "cloud_game.queue_status", escape(status))
        state.last_status, state.last_report_at = status, now


def _begin_queue(state):
    if state.queue_deadline is None:
        state.queue_deadline = time.monotonic() + state.timeout_minutes * 60
    if time.monotonic() >= state.queue_deadline:
        raise _CloudError("cloud_game.queue_timeout")


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
    for name in ("IsWindow", "IsWindowVisible", "IsWindowEnabled", "SetForegroundWindow"):
        function = getattr(user32, name)
        function.argtypes = [wintypes.HWND]
        function.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
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
        return []
    owner = owners[0]
    return [
        item for item in windows
        if item.hwnd != owner.hwnd
        and item.owner == owner.hwnd
        and _POPUP_CLASS.match(item.class_name)
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
    _clear_confirm_target()
    _session = None


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
        logger.debug("owned 弹窗归一化失败: %s", type(exc).__name__)
        return None


def _capture_owned_window(item):
    """AgentServer 无 ControllerCreate API；用有超时的原生工作进程取帧。"""
    from utils.cloud_window import capture_window

    return _normalize_owned_frame(capture_window(item.hwnd))


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
    try:
        notice = context.run_recognition(screen, frame)
        button = context.run_recognition(target, frame)
    except Exception as exc:
        logger.debug("owned 弹窗识别异常: %s", type(exc).__name__)
        return None
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
    candidates = _owned_dialog_candidates()
    if expected_hwnd is not None:
        candidates = [item for item in candidates if item.hwnd == expected_hwnd]
    for item in candidates:
        frame = _capture_owned_window(item)
        if frame is None:
            continue
        result = _recognize_owned_confirm(context, item, frame, screen, target)
        if result is not None:
            matches.append(result)
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
                return None
            result = _find_owned_confirm(context, screen, target)
            if result is None:
                return None
            return CustomRecognition.AnalyzeResult(box=list(result.box), detail={})
        except Exception as exc:
            _clear_confirm_target()
            logger.debug("cloud_game_owned_dialog 识别失败: %s", type(exc).__name__)
            return None


def _prepare_cloud_client(context, state):
    from cloud_start import (
        client_windows,
        desktop_interactive,
        prepare_client_window,
        window_responding,
    )

    if not desktop_interactive():
        raise _CloudError("cloud_game.desktop_unavailable")
    windows = client_windows()
    if len(windows) != 1:
        raise _CloudError("cloud_game.client_missing" if not windows else "cloud_game.ambiguous_target")
    window = windows[0]
    if not window_responding(window.hwnd):
        raise _CloudError("cloud_game.client_unresponsive")
    if not prepare_client_window(window.hwnd):
        raise _CloudError("cloud_game.screen_too_small")
    deadline = time.monotonic() + state.transition_timeout
    while time.monotonic() < deadline:
        _check_stop(context)
        controller = context.tasker.controller
        if controller is not None and controller.post_screencap().wait().succeeded:
            image = controller.cached_image
            if isinstance(image, np.ndarray) and image.ndim == 3 and image.shape[:2] == (720, 1280):
                state.main_hwnd = window.hwnd
                PrintT(context, "cloud_game.client_ready")
                return
        _pause(context, 0.2, deadline)
    raise _CloudError("cloud_game.invalid_frame")


_LOGIN_HINTS = (
    ("CloudGameLoginFailure", "cloud_game.login_retry_required"),
    ("CloudGameVerification", "cloud_game.verification_required"),
    ("CloudGameAuthorization", "cloud_game.authorization_required"),
    ("CloudGameLoginForm", "cloud_game.login_required"),
)


def _login_obstacle(context, image):
    """返回 (提示消息, 可点击的“登录”目标或 None)。

    凭据表单、验证码、授权、协议勾选只是等待信号，不读取或自动提交凭据；
    只有客户端自己记住的账号（无任何输入框）才允许点击一次“登录”。
    """
    unknown_overlay = False
    submit = None
    for item in _owned_dialog_candidates():
        frame = _capture_owned_window(item)
        if frame is None:
            # 取帧瞬时失败不能当成弹窗不存在，也不能终止等待。
            unknown_overlay = True
            continue
        for node, message in _LOGIN_HINTS:
            if _hit(context, node, frame):
                return message, None
        if _hit(context, "CloudGameStartConfirmNotice", frame) or _hit(
            context, "CloudGameDailyLoginTitle", frame
        ):
            continue
        if _hit(context, "CloudGameLoginOtherMethods", frame):
            button = _detail(context, "CloudGameLoginSubmit", frame)
            if button.hit and len(button.filtered_results) == 1 and submit is None:
                submit = _save_confirm_target(item, button.box)
                continue
        unknown_overlay = True
    if submit is not None:
        return "cloud_game.login_remembered", submit
    for node, message in _LOGIN_HINTS:
        if _hit(context, node, image):
            return message, None
    # 未知遮挡时，背景首页不能证明登录或授权已经完成。
    return ("cloud_game.login_required" if unknown_overlay else None), None


@AgentServer.custom_action("cloud_game_reset")
class CloudGameReset(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        global _session
        _session = None
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
        deadline = state.login_deadline
        prompted = set()
        stable = 0

        def prompt(key):
            if key not in prompted:
                PrintT(context, key)
                prompted.add(key)

        while time.monotonic() < deadline:
            image = _capture(context)
            _error_check(context, image)
            obstacle, submit = _login_obstacle(context, image)
            if submit is not None and "login_submit" not in state.clicked:
                # 客户端记住的账号：只点一次“登录”，随后继续按画面状态等待。
                state.clicked.add("login_submit")
                prompt(obstacle)
                _check_stop(context)
                _click_owned_target(submit)
                stable = 0
            elif obstacle:
                stable = 0
                prompt("cloud_game.login_required" if submit is not None else obstacle)
            elif _hit(context, "CloudGameLoginScreen", image):
                stable = 0
                prompt("cloud_game.login_required")
                # 已提交过登录后主窗口可能短暂仍显示入口提示，再点会重新打开登录窗口。
                if not state.clicked & {"login", "login_submit"}:
                    point = _single_box(_detail(context, "CloudGameLoginScreen", image))
                    state.clicked.add("login")
                    _click_main_window(context, state, point)
            elif _post_login_state(context, image):
                stable += 1
                if stable >= 2:
                    _clear_confirm_target()
                    PrintT(context, "cloud_game.login_detected")
                    return True
            else:
                stable = 0
                # 未识别到输入表单时同样给出可执行提示，而非静默等待十分钟。
                prompt("cloud_game.login_required")
            # 用户可在客户端内修正账号、验证码或重试网络请求；总截止时间不延长。
            # 不自动刷新验证码、不重复提交登录、不代用户接受条款或第三方授权。
            _pause(context, state.poll_interval, deadline)
        raise _CloudError("cloud_game.login_timeout")


@AgentServer.custom_action("cloud_game_queue_wait")
class CloudGameQueueWait(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        state = _state(argv)
        if not state.auto_queue:
            raise _CloudError("cloud_game.manual_enter")
        _begin_queue(state)
        unknown_since = None
        while time.monotonic() < state.queue_deadline:
            image = _capture(context)
            _error_check(context, image)
            # 返回 Pipeline 处理弹窗/确认框/游戏登录，不在 Python 内点击或跑整条流程。
            if _hit(context, "CloudGameDailyLoginTitle", image) or _confirm_dialog(
                context
            ):
                return True
            if _hit(context, "CloudGameLoginScreen", image):
                raise _CloudError("cloud_game.login_required")
            if _first_hit(context, ("CloudGameGameLogin", "CloudGameInWorld"), image):
                return True
            if _hit(context, "CloudGameQueueScreen", image):
                unknown_since = None
                _report_queue(context, state, image)
            elif _hit(context, "CloudGameLoading", image):
                unknown_since = None
                if not state.loading_reported:
                    PrintT(context, "cloud_game.loading")
                    state.loading_reported = True
            else:
                if unknown_since is None:
                    unknown_since = time.monotonic()
                if time.monotonic() - unknown_since >= state.transition_timeout:
                    raise _CloudError("cloud_game.unknown_state")
            _pause(context, state.poll_interval, state.queue_deadline)
        raise _CloudError("cloud_game.queue_timeout")


def _box_center(box):
    x, y, width, height = _box_values(box)
    return x + width // 2, y + height // 2


def _single_box(detail):
    if not detail.hit or len(detail.filtered_results) != 1:
        raise _CloudError("cloud_game.ambiguous_target")
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


def _activate_window(hwnd):
    """把目标窗口提到前台。未激活的 Qt OpenGL 窗口会丢弃合成鼠标输入。"""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    _init_win32_api(user32)
    try:
        if not user32.IsWindowVisible(hwnd):
            return False
        user32.ShowWindow(hwnd, 5)
        return bool(user32.SetForegroundWindow(hwnd))
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
    """激活目标窗口后发送一次原生点击。

    click_window 内部还会校验前台归属、遮挡与光标落点，任一不符直接拒绝，
    不会盲发按键；因此这里只需汇报成功与否，由调用方决定错误语义。
    """
    from utils.cloud_window import click_window

    return bool(_activate_window(hwnd)) and bool(click_window(hwnd, point))


def _click_main_window(context, state, point):
    """主窗口点击同样先激活窗口再发原生输入，而不是直接用控制器合成点击。

    实机取证（2026-09-24 20:49 与 21:24 同一像素对照）：控制器已把 720p 的
    (640,548) 正确换算到屏幕 (960,703)，但当时窗口 active/focus 均为 0，
    客户端日志没有任何新增记录，登录窗口也没出现；改走“激活 + 原生点击”
    后立刻唤出登录窗口。因此这不是坐标问题，而是未激活的 Qt OpenGL 窗口
    丢弃了合成输入。
    """
    item = _main_window_info(state)
    if item is None:
        raise _CloudError("cloud_game.client_missing")
    target = _map_720p_to_client(point, item.client_size)
    _check_stop(context)
    if not _native_click(item.hwnd, target):
        raise _CloudError("cloud_game.input_rejected")
    return True


def _target_window_info(target):
    item = _window_by_hwnd(target.hwnd)
    if item is None:
        return None
    if (
        item.owner != target.owner
        or (target.pid and item.pid != target.pid)
        or not _POPUP_CLASS.match(item.class_name)
        or item.rect != target.rect
        or item.client_size != target.client_size
    ):
        return None
    return item


def _click_owned_target(target):
    """锁定已识别的 HWND 与几何信息；失败不回退到主窗口输入。"""
    candidates = _owned_dialog_candidates()
    if not any(item.hwnd == target.hwnd for item in candidates):
        raise _CloudError("cloud_game.ambiguous_target")
    item = _target_window_info(target)
    if item is None:
        raise _CloudError("cloud_game.ambiguous_target")
    point = _map_720p_to_client(_box_center(target.box), item.client_size)
    if not _native_click(item.hwnd, point):
        raise _CloudError("cloud_game.input_rejected")
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
            raise _CloudError("cloud_game.invalid_config")
        if kind != "daily" and not state.auto_queue:
            raise _CloudError("cloud_game.manual_enter")
        if kind in state.clicked:
            raise _CloudError("cloud_game.transition_failed")
        if kind == "confirm":
            return self._confirm(context, argv, state)
        source, target = choices[kind]
        image = _capture(context)
        _error_check(context, image)
        if not _hit(context, source, image):
            raise _CloudError("cloud_game.transition_failed")
        if kind != "daily" and _hit(context, "CloudGameDailyLoginTitle", image):
            raise _CloudError("cloud_game.transition_failed")
        if _hit(context, "CloudGameLoginScreen", image):
            raise _CloudError("cloud_game.login_required")
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
                stable = stable + 1 if known else 0
                if stable >= 2:
                    PrintT(context, "cloud_game.daily_login_closed")
                    return True
            else:
                # 确认弹窗是独立窗口，主窗口仍显示首页，因此它不需要 source 消失。
                if kind == "enter" and _confirm_dialog(context):
                    return True
                candidates = tuple(node for node in _POST_LOGIN[:-1] if node != source)
                # 来源界面仍在时，背景中的同类文字不是点击成功证据。
                if not _hit(context, source, image) and _first_hit(
                    context, candidates, image
                ):
                    return True
            _pause(context, state.poll_interval, deadline)
        if (
            state.queue_deadline is not None
            and time.monotonic() >= state.queue_deadline
        ):
            raise _CloudError("cloud_game.queue_timeout")
        raise _CloudError(
            "cloud_game.daily_login_close_failed"
            if kind == "daily"
            else "cloud_game.transition_failed"
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
            raise _CloudError("cloud_game.transition_failed")
        fresh = _find_owned_confirm(
            context, "CloudGameStartConfirmNotice", "CloudGameStartConfirmEnter",
            expected_hwnd=target.hwnd,
        )
        if fresh is None or fresh.rect != target.rect or fresh.client_size != target.client_size:
            raise _CloudError("cloud_game.ambiguous_target")
        target = fresh
        _begin_queue(state)
        _check_stop(context)
        _click_owned_target(target)
        state.clicked.add("confirm")
        deadline = min(
            time.monotonic() + state.transition_timeout, state.queue_deadline
        )
        while time.monotonic() < deadline:
            # 仍需读取主窗口：用于错误/登录失效判断，也提供弹窗关闭后的正向证据。
            image = _capture(context)
            _error_check(context, image)
            if _hit(context, "CloudGameLoginScreen", image):
                raise _CloudError("cloud_game.login_required")
            target_state = _confirm_target_state(target)
            # target_state 不是 gone 时一律继续等待，不能把取帧/枚举失败当成关闭成功。
            if target_state == "gone":
                # 确认后首页可能短暂保留；必须等到排队、加载、游戏登录或大世界
                # 等非首页状态出现，避免回到分派节点再次点击“开始游戏”。
                next_state = _first_hit(context, _POST_LOGIN[:-1], image)
                if next_state:
                    _clear_confirm_target()
                    return True
            _pause(context, state.poll_interval, deadline)
        if time.monotonic() >= state.queue_deadline:
            raise _CloudError("cloud_game.queue_timeout")
        raise _CloudError("cloud_game.transition_failed")


@AgentServer.custom_action("cloud_game_confirm_ready")
class CloudGameConfirmReady(CustomAction):
    @_guarded
    def run(self, context: Context, argv: CustomAction.RunArg):
        state = _state(argv)
        deadline = time.monotonic() + state.transition_timeout
        stable = 0
        while time.monotonic() < deadline:
            image = _capture(context)
            _error_check(context, image)
            if _hit(context, "CloudGameDailyLoginTitle", image):
                return context.override_next(argv.node_name, ["CloudGameDispatch"])
            if _hit(context, "CloudGameLoginScreen", image):
                raise _CloudError("cloud_game.login_required")
            blocked = _first_hit(
                context,
                (
                    "CloudGameQueueScreen",
                    "CloudGameLoading",
                ),
                image,
            )
            stable = (
                stable + 1
                if not blocked and _hit(context, "CloudGameInWorld", image)
                else 0
            )
            if stable >= 2:
                return context.override_next(argv.node_name, ["CloudGameStartDone"])
            _pause(context, state.poll_interval, deadline)
        raise _CloudError("cloud_game.transition_failed")


@AgentServer.custom_action("cloud_game_fail")
class CloudGameFail(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        cleanup_cloud_session()
        return CustomAction.RunResult(success=False)


@AgentServer.custom_action("cloud_game_finish")
class CloudGameFinish(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        cleanup_cloud_session()
        return CustomAction.RunResult(success=True)
