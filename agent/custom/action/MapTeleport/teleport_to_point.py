"""Map teleport flow entrypoint."""

from __future__ import annotations

import ctypes
import json
import math
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

try:
    from maa.agent.agent_server import AgentServer
    from maa.context import Context
    from maa.custom_action import CustomAction
    from maa.pipeline import JOCR, JRecognitionType
except ImportError:
    AgentServer = None
    Context = Any
    CustomAction = None
    JOCR = None
    JRecognitionType = None

from utils.task_control import (
    TaskCancelledError,
    TaskFailedError,
    action_succeeded,
    interruptible_wait,
    run_task_checked,
)

from ..Common.logger import get_logger
from ..Common.utils import match_template_in_region
from .check_teleport_required import (
    DEFAULT_COORDINATE_TYPE,
    find_named_record,
    load_json_resource,
    point_xy,
    resource_base_path,
)

logger = get_logger(__name__)

DEFAULT_TELEPORT_POINTS_FILE = "map_teleport/teleport_points.json"
MAP_INDEX_ICON_TEMPLATE = "image/map_teleport/map_index_icon.png"
AREA_NEXT_BTN_TEMPLATE = "image/map_teleport/area_next_btn.png"
SUB_SELECTION_BTN_TEMPLATE = "image/map_teleport/sub_seletion_btn.png"
ZOOM_CONTROL_BTN_TEMPLATE = "image/map_teleport/Zoom_control_button.png"

# 所有 ROI 都基于 1280x720；这些区域只覆盖地图索引和传送确认流程中需要看的小块。
MAP_INDEX_ICON_ROI = [1069, 626, 89, 74]
MAP_INDEX_TITLE_ROI = [907, 66, 121, 36]
AREA_NAME_ROI = [958, 127, 235, 40]
AREA_NEXT_BTN_ROI = [1203, 126, 44, 42]
MAIN_SELECTION_ROI = [901, 176, 358, 460]
SUB_SELECTION_ROI = [1190, 180, 55, 461]
TELEPORT_CONFIRM_POINT = [639, 361]
TELEPORT_BUTTON_ROI = [933, 620, 332, 45]
ZOOM_CONTROL_BTN_ROI = [47, 258, 34, 242]

KEY_ESC = 27
KEY_M = 77

DEFAULT_TEMPLATE_THRESHOLD = 0.8
DEFAULT_OCR_THRESHOLD = 0.5
DEFAULT_MAX_AREA_SWITCHES = 15
DEFAULT_ACTION_DELAY = 0.5
DEFAULT_POST_MAP_OPEN_DELAY = 1.0
DEFAULT_FINAL_CONFIRM_DELAY = 1.0
DEFAULT_STILL_MAX_WAIT = 8.0
DEFAULT_STILL_INTERVAL = 0.25
DEFAULT_STILL_CONSECUTIVE = 2
DEFAULT_STILL_DIFF_THRESHOLD = 1.5


@dataclass(frozen=True)
class TeleportPoint:
    id: str
    name: str
    point: tuple[float, float]
    coordinate_type: str
    area_name: str
    icon_index: int
    selection_name: str
    icon_path: str
    description: str


def parse_params(custom_action_param: Any) -> dict[str, Any]:
    if not custom_action_param:
        return {}
    if isinstance(custom_action_param, dict):
        return custom_action_param
    return json.loads(custom_action_param)


def load_teleport_point(
    teleport_point_id: str,
    *,
    points_file: str = DEFAULT_TELEPORT_POINTS_FILE,
) -> TeleportPoint:
    data = load_json_resource(points_file)
    record = find_named_record(data, "teleport_points", teleport_point_id)
    return TeleportPoint(
        id=str(record.get("id", teleport_point_id)),
        name=str(record.get("name", record.get("id", teleport_point_id))),
        point=point_xy(record),
        coordinate_type=str(record.get("coordinateType", DEFAULT_COORDINATE_TYPE)),
        area_name=str(record["areaName"]),
        icon_index=int(record["iconIndex"]),
        selection_name=str(record.get("selectionName", "")),
        icon_path=str(record.get("iconPath", "")),
        description=str(record.get("description", "")),
    )


def _notify(context: Context | None, message: str) -> None:
    if context is None or _is_stopping(context):
        return

    try:
        from utils.maafocus import Print

        Print(context, message)
    except Exception as exc:
        logger.warning("MapTeleport focus message failed: %s", exc)


def _is_stopping(context: Context | None) -> bool:
    return bool(
        context is not None
        and getattr(getattr(context, "tasker", None), "stopping", False)
    )


def _template_path(relative_path: str) -> str:
    return str(resource_base_path() / relative_path)


def _load_template(relative_path: str) -> Any:
    template = cv2.imread(_template_path(relative_path), cv2.IMREAD_COLOR)
    if template is None:
        raise FileNotFoundError("template not found: %s" % relative_path)
    return template


def _normalize_frame(frame: Any) -> Any:
    if frame is not None and len(frame.shape) == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


def _screencap(context: Context) -> Any:
    if _is_stopping(context):
        return None
    try:
        controller = context.tasker.controller
        if not controller.post_screencap().wait().succeeded or _is_stopping(context):
            return None
        frame = controller.cached_image
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            return None
        return _normalize_frame(frame)
    except Exception as exc:
        logger.error("MapTeleport screencap failed: %s", exc)
        return None


def _move_pointer(context: Context, x: int, y: int) -> bool:
    if _is_stopping(context):
        return False
    try:
        success = (
            context.tasker.controller.post_touch_move(int(x), int(y)).wait().succeeded
        )
        return bool(success and not _is_stopping(context))
    except Exception as exc:
        logger.error("MapTeleport pointer move failed: pos=(%s,%s) error=%s", x, y, exc)
        return False


def _release_touch(controller: Any) -> bool:
    # 即使停止或按下失败也要释放，避免原生控制器保留按下状态。
    try:
        return bool(controller.post_touch_up().wait().succeeded)
    except Exception as exc:
        logger.error("MapTeleport touch release failed: %s", exc)
        return False


def _click_point(context: Context, x: int, y: int, delay: float = 0.05) -> bool:
    if not math.isfinite(delay) or delay < 0:
        logger.error("MapTeleport invalid click delay: %s", delay)
        return False
    if not _move_pointer(context, x, y) or _is_stopping(context):
        return False
    controller = context.tasker.controller
    success = False
    try:
        success = bool(
            controller.post_touch_down(int(x), int(y)).wait().succeeded
            and not _is_stopping(context)
            and interruptible_wait(context, delay)
        )
    except Exception as exc:
        logger.error("MapTeleport touch click failed: pos=(%s,%s) error=%s", x, y, exc)
    finally:
        released = _release_touch(controller)
    return bool(success and released and not _is_stopping(context))


def _click_rect(context: Context, rect: list[int] | tuple[int, int, int, int]) -> bool:
    x, y, w, h = rect
    return _click_point(context, int(x + w / 2), int(y + h / 2))


def _press_key_action(context: Context, key: int) -> bool:
    # 通过 Pipeline 的 ClickKey 发键，避免 CustomAction 内直接调用 controller 按键时的原生层异常。
    if _is_stopping(context):
        return False
    node_name = "__MapTeleportPressKey"
    try:
        result = context.run_action(
            node_name,
            pipeline_override={
                node_name: {
                    "action": {
                        "type": "ClickKey",
                        "param": {
                            "key": [
                                int(key),
                            ],
                        },
                    },
                    "pre_delay": 0,
                    "post_delay": 0,
                    "rate_limit": 0,
                }
            },
        )
    except Exception as exc:
        logger.error("MapTeleport press key failed: key=%s error=%s", key, exc)
        return False
    return bool(not _is_stopping(context) and action_succeeded(result))


def _click_rect_action(context: Context, rect: list[int]) -> bool:
    # 关键确认点击同样交给 Pipeline Click，兼容 SeizeInput 等不同控制器实现。
    if _is_stopping(context):
        return False
    node_name = "__MapTeleportClick"
    try:
        result = context.run_action(
            node_name,
            pipeline_override={
                node_name: {
                    "action": {
                        "type": "Click",
                        "param": {
                            "target": rect,
                        },
                    },
                    "pre_delay": 0,
                    "post_delay": 0,
                    "rate_limit": 0,
                }
            },
        )
    except Exception as exc:
        logger.error("MapTeleport click failed: rect=%s error=%s", rect, exc)
        return False
    return bool(not _is_stopping(context) and action_succeeded(result))


def _swipe_up_in_roi(context: Context, roi: list[int], *, duration: int) -> bool:
    if _is_stopping(context):
        return False
    if not math.isfinite(duration) or duration < 0:
        logger.error("MapTeleport invalid swipe duration: %s", duration)
        return False
    x, y, w, h = roi
    start_x = int(x + w / 2)
    start_y = int(y + h)  # 从 ROI 最底下
    end_x = start_x
    end_y = int(y)  # 拖到 ROI 最顶上
    controller = context.tasker.controller
    success = False
    try:
        success = bool(
            controller.post_swipe(
                start_x,
                start_y,
                end_x,
                end_y,
                duration=duration,
            )
            .wait()
            .succeeded
        )
    except Exception as exc:
        logger.error("MapTeleport swipe failed: roi=%s error=%s", roi, exc)
    finally:
        released = _release_touch(controller)
    return bool(success and released and not _is_stopping(context))


def _box_to_rect(box: Any) -> list[int] | None:
    if box is None:
        return None
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        return [int(box[0]), int(box[1]), int(box[2]), int(box[3])]
    if all(hasattr(box, attr) for attr in ("x", "y", "w", "h")):
        return [int(box.x), int(box.y), int(box.w), int(box.h)]
    return None


def _detail_box(detail: Any) -> list[int] | None:
    if detail is None:
        return None

    rect = _box_to_rect(getattr(detail, "box", None))
    if rect is not None:
        return rect

    best_result = getattr(detail, "best_result", None)
    rect = _box_to_rect(getattr(best_result, "box", None))
    if rect is not None:
        return rect

    for item in getattr(detail, "filtered_results", None) or []:
        rect = _box_to_rect(getattr(item, "box", None))
        if rect is not None:
            return rect

    for item in getattr(detail, "all_results", None) or []:
        rect = _box_to_rect(getattr(item, "box", None))
        if rect is not None:
            return rect
    return None


def _wait_screen_still(
    context: Context,
    *,
    max_wait: float = DEFAULT_STILL_MAX_WAIT,
    interval: float = DEFAULT_STILL_INTERVAL,
    consecutive: int = DEFAULT_STILL_CONSECUTIVE,
    diff_threshold: float = DEFAULT_STILL_DIFF_THRESHOLD,
) -> Any | None:
    if (
        not math.isfinite(max_wait)
        or max_wait < 0
        or not math.isfinite(interval)
        or interval <= 0
        or consecutive < 1
        or not math.isfinite(diff_threshold)
        or diff_threshold < 0
        or _is_stopping(context)
    ):
        return None
    deadline = time.monotonic() + max_wait
    previous = _screencap(context)
    if previous is None or _is_stopping(context):
        return None

    # 仅采样画面，不重复 UI 动作；超时不能将仍在变化的最后一帧视为稳定。
    stable_count = 0
    while not _is_stopping(context):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not interruptible_wait(context, min(interval, remaining)):
            return None
        if time.monotonic() >= deadline:
            return None
        current = _screencap(context)
        if current is None or _is_stopping(context):
            return None

        prev_small = cv2.resize(previous, (160, 90), interpolation=cv2.INTER_AREA)
        curr_small = cv2.resize(current, (160, 90), interpolation=cv2.INTER_AREA)
        diff = float(
            np.mean(
                cv2.absdiff(
                    cv2.cvtColor(prev_small, cv2.COLOR_BGR2GRAY),
                    cv2.cvtColor(curr_small, cv2.COLOR_BGR2GRAY),
                )
            )
        )
        if diff <= diff_threshold:
            stable_count += 1
            if stable_count >= consecutive:
                return current if not _is_stopping(context) else None
        else:
            stable_count = 0
        previous = current

    return None


def _match_template(
    frame: Any,
    roi: list[int],
    template: Any,
    threshold: float = DEFAULT_TEMPLATE_THRESHOLD,
) -> tuple[bool, float, int, int]:
    return match_template_in_region(frame, roi, template, threshold)


def _find_template_matches(
    frame: Any,
    roi: list[int],
    template: Any,
    *,
    threshold: float = DEFAULT_TEMPLATE_THRESHOLD,
    max_results: int = 20,
) -> list[tuple[int, int, int, int, float]]:
    if frame is None or not isinstance(frame, np.ndarray):
        return []

    x, y, w, h = roi
    frame_h, frame_w = frame.shape[:2]
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(frame_w, x + w)
    y2 = min(frame_h, y + h)
    if x2 <= x1 or y2 <= y1:
        return []

    region = frame[y1:y2, x1:x2]
    template_h, template_w = template.shape[:2]
    if region.shape[0] < template_h or region.shape[1] < template_w:
        return []

    result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)

    # 传送点图标会有多个，逐个取最高分并抑制同一图标周围的重复响应。
    matches: list[tuple[int, int, int, int, float]] = []
    suppress_x = max(1, template_w)
    suppress_y = max(1, template_h)
    while len(matches) < max_results:
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            break

        match_x = x1 + max_loc[0]
        match_y = y1 + max_loc[1]
        matches.append((match_x, match_y, template_w, template_h, float(max_val)))

        sx1 = max(0, max_loc[0] - suppress_x)
        sy1 = max(0, max_loc[1] - suppress_y)
        sx2 = min(result.shape[1], max_loc[0] + suppress_x)
        sy2 = min(result.shape[0], max_loc[1] + suppress_y)
        result[sy1:sy2, sx1:sx2] = -1.0

    matches.sort(key=lambda item: (item[1], item[0]))
    return matches


def _ocr_match(
    context: Context,
    frame: Any,
    text: str,
    roi: list[int],
    *,
    threshold: float = DEFAULT_OCR_THRESHOLD,
) -> Any | None:
    if JOCR is None or JRecognitionType is None:
        raise RuntimeError("maa.pipeline OCR is unavailable")
    if frame is None or _is_stopping(context):
        return None

    detail = context.run_recognition_direct(
        JRecognitionType.OCR,
        JOCR(expected=[text], roi=tuple(roi), threshold=threshold),
        frame,
    )
    if (
        not _is_stopping(context)
        and detail is not None
        and getattr(detail, "hit", False)
    ):
        return detail
    return None


def _ensure_in_world(context: Context) -> bool:
    try:
        if not run_task_checked(context, "SceneAnyEnterWorld"):
            return False
        frame = _wait_screen_still(context, max_wait=5.0)
        if frame is None or _is_stopping(context):
            return False
        result = context.run_recognition("InWorld", frame)
        return bool(not _is_stopping(context) and result and result.hit)
    except Exception as exc:
        logger.error("MapTeleport enter world failed: %s", exc)
        return False


def _click_map_index(context: Context, template: Any, threshold: float) -> bool:
    frame = _wait_screen_still(context)
    if frame is None or _is_stopping(context):
        return False
    matched, score, x, y = _match_template(
        frame,
        MAP_INDEX_ICON_ROI,
        template,
        threshold=threshold,
    )
    logger.debug("MapTeleport map index icon match=%s score=%.3f", matched, score)
    if not matched:
        return False

    return _click_rect(context, [x, y, template.shape[1], template.shape[0]])


def _wait_ocr_text(
    context: Context,
    text: str,
    roi: list[int],
    *,
    threshold: float,
    max_wait: float = 5.0,
) -> Any | None:
    if not math.isfinite(max_wait) or max_wait < 0:
        return None
    deadline = time.monotonic() + max_wait
    while not _is_stopping(context):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        frame = _wait_screen_still(context, max_wait=min(2.0, remaining))
        if frame is None or _is_stopping(context):
            return None
        detail = _ocr_match(context, frame, text, roi, threshold=threshold)
        if _is_stopping(context):
            return None
        if detail is not None:
            return detail
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not interruptible_wait(context, min(0.2, remaining)):
            return None
    return None


def _switch_to_area(
    context: Context,
    area_name: str,
    area_next_template: Any,
    *,
    template_threshold: float,
    ocr_threshold: float,
    max_switches: int,
    action_delay: float,
) -> bool:
    if not math.isfinite(action_delay) or action_delay < 0 or max_switches < 1:
        return False
    for attempt in range(max_switches):
        if _is_stopping(context):
            return False

        # 地区名没命中时点击右上角切换按钮，最多翻 15 次避免卡死。
        frame = _wait_screen_still(context, max_wait=3.0)
        if frame is None or _is_stopping(context):
            return False
        detail = _ocr_match(
            context, frame, area_name, AREA_NAME_ROI, threshold=ocr_threshold
        )
        if _is_stopping(context):
            return False
        if detail is not None:
            logger.info("MapTeleport area matched: %s", area_name)
            return True

        matched, score, x, y = _match_template(
            frame,
            AREA_NEXT_BTN_ROI,
            area_next_template,
            threshold=template_threshold,
        )
        logger.debug(
            "MapTeleport area next attempt=%s match=%s score=%.3f",
            attempt + 1,
            matched,
            score,
        )
        if not matched:
            return False

        if not _click_rect(
            context, [x, y, area_next_template.shape[1], area_next_template.shape[0]]
        ) or not interruptible_wait(context, action_delay):
            return False

    return False


def _get_cursor_pos() -> tuple[int, int]:
    """获取当前鼠标在游戏窗口内的坐标。"""
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    hwnd = ctypes.windll.user32.WindowFromPoint(pt)
    if hwnd:
        ctypes.windll.user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y)


def _require_still_frame(context: Context, *, max_wait: float) -> Any:
    frame = _wait_screen_still(context, max_wait=max_wait)
    if _is_stopping(context):
        raise TaskCancelledError("MapTeleport screen wait")
    if frame is None:
        raise TaskFailedError("MapTeleport could not obtain a stable fresh frame")
    return frame


def _identify_new_list_nodes(
    context: Context,
    template: Any,
    *,
    template_threshold: float,
) -> tuple[int, int, int, int, float] | None:
    """识别推荐地点列表中鼠标分界线以下第一个新出现的选项。

    通过系统 API 获取鼠标 Y 坐标，在子选项列表中匹配目标图标模板，
    取 Y 严格大于鼠标 Y 的第一个匹配点。
    """
    frame = _require_still_frame(context, max_wait=3.0)

    _, mouse_y = _get_cursor_pos()

    matches = _find_template_matches(
        frame,
        SUB_SELECTION_ROI,
        template,
        threshold=template_threshold,
    )

    for x, y, w, h, score in matches:
        if y > mouse_y:
            return (x, y, w, h, score)

    return None


def _find_closest_icon_center(
    context: Context,
    template: Any,
    *,
    template_threshold: float,
) -> tuple[int, int] | None:
    """找到离鼠标 Y 最近的选项，返回其拖动起始坐标。"""
    cursor_x, mouse_y = _get_cursor_pos()

    roi_h = 85
    search_roi = [1196, mouse_y - roi_h // 2, 47, roi_h]

    frame = _require_still_frame(context, max_wait=1.5)
    matches = _find_template_matches(
        frame,
        search_roi,
        template,
        threshold=template_threshold,
    )
    if not matches:
        return None

    closest = min(matches, key=lambda m: abs(m[1] + m[3] // 2 - mouse_y))
    _, y, _, h, _ = closest
    return (1160, y + h // 2)


def _drag_list_upward(
    context: Context,
    template: Any,
    *,
    template_threshold: float,
) -> bool:
    """拖住列表底部图标向上移动，露出下方新图标。"""
    if _is_stopping(context):
        return False
    drag_start = _find_closest_icon_center(
        context,
        template,
        template_threshold=template_threshold,
    )
    if drag_start is None:
        return False

    roi = [drag_start[0] - 1, drag_start[1] - 17, 2, 20]
    if not _swipe_up_in_roi(context, roi, duration=200):
        return False

    # 重新定位鼠标到当前最近的图标中心
    anchor = _find_closest_icon_center(
        context,
        template,
        template_threshold=template_threshold,
    )
    return anchor is not None and _move_pointer(context, anchor[0], anchor[1])


def _check_closest_icon(
    context: Context,
    icon_path: str,
    *,
    template_threshold: float,
) -> bool:
    """检查鼠标附近是否恰好匹配到一个目标传送图标。"""
    _, mouse_y = _get_cursor_pos()

    search_roi = [914, mouse_y - 20, 40, 41]
    template = _load_template(icon_path)

    frame = _require_still_frame(context, max_wait=1.5)
    matches = _find_template_matches(
        frame,
        search_roi,
        template,
        threshold=template_threshold,
        max_results=2,
    )
    if len(matches) == 1:
        return True
    else:
        return False


def _drag_zoom_control(
    context: Context,
    *,
    template_threshold: float = DEFAULT_TEMPLATE_THRESHOLD,
) -> bool:
    """拖动缩放调节按钮。识别按钮后向上拖拽放大地图视图。"""
    frame = _wait_screen_still(context, max_wait=3.0)
    if frame is None or _is_stopping(context):
        return False
    template = _load_template(ZOOM_CONTROL_BTN_TEMPLATE)

    matches = _find_template_matches(
        frame,
        ZOOM_CONTROL_BTN_ROI,
        template,
        threshold=template_threshold,
        max_results=1,
    )
    if not matches:
        return False

    x, y, w, h, score = matches[0]
    cx = x + w // 2
    cy = y + h // 2

    # roi 底部正好落在按钮中心 (cx, cy)，从按钮拖到上方 300px
    end_y = cy - 300
    roi = [cx - 1, end_y, 2, 300]
    if not _swipe_up_in_roi(context, roi, duration=300):
        return False

    logger.info(
        "MapTeleport zoom control dragged: from=(%s,%s) to=(%s,%s)",
        cx,
        cy,
        cx,
        end_y,
    )
    return True


def _click_main_selection(
    context: Context,
    selection_name: str,
    *,
    ocr_threshold: float,
) -> bool:
    frame = _wait_screen_still(context, max_wait=3.0)
    if frame is None or _is_stopping(context):
        return False
    detail = _ocr_match(
        context,
        frame,
        selection_name,
        MAIN_SELECTION_ROI,
        threshold=ocr_threshold,
    )
    if detail is None:
        logger.error(
            "MapTeleport main selection OCR not found: name=%s",
            selection_name,
        )
        return False

    rect = _detail_box(detail) or MAIN_SELECTION_ROI
    return _click_rect(context, rect)


def _click_teleport_icon(
    context: Context,
    sub_btn_template: Any,
    icon_path: str,
    icon_index: int,
    *,
    template_threshold: float,
) -> bool:
    """在列表中逐项拖动，找到第 icon_index 个目标传送图标并点击。"""
    if icon_index < 1 or _is_stopping(context):
        return False

    # 鼠标移到屏幕顶部；每个已识别条目只处理一次，滚动后重新定位。
    if not _move_pointer(context, 1160, 50) or not interruptible_wait(context, 0.3):
        return False

    count = 0
    consecutive_miss = 0
    visited = set()
    max_entries = max(40, icon_index * 20)
    for _ in range(max_entries):
        if _is_stopping(context) or consecutive_miss >= 4:
            return False
        new_node = _identify_new_list_nodes(
            context,
            sub_btn_template,
            template_threshold=template_threshold,
        )
        if _is_stopping(context):
            return False
        if new_node is not None:
            consecutive_miss = 0
            x, y, w, h, _ = new_node
            node = (x, y, w, h)
            if node in visited:
                logger.error("MapTeleport list did not advance: node=%s", node)
                return False
            visited.add(node)
            cy = y + h // 2
            if (
                not interruptible_wait(context, 0.1)
                or not _move_pointer(context, 1160, cy)
                or not interruptible_wait(context, 0.1)
            ):
                return False

            matched = _check_closest_icon(
                context,
                icon_path,
                template_threshold=template_threshold,
            )
            if _is_stopping(context):
                return False
            if matched:
                count += 1
                logger.info("MapTeleport teleport icon match: count=%s", count)
                if count == icon_index:
                    if not _click_point(context, 1160, cy):
                        return False
                    logger.info(
                        "MapTeleport teleport icon clicked: index=%s pos=(1160,%s)",
                        icon_index,
                        cy,
                    )
                    return True
        else:
            consecutive_miss += 1
            # 未见新选项时保留原有两次小幅拖动，每次都验证结果和新画面。
            for _ in range(2):
                if _is_stopping(context) or not _drag_list_upward(
                    context,
                    sub_btn_template,
                    template_threshold=template_threshold,
                ):
                    return False
            visited.clear()

    logger.error(
        "MapTeleport teleport icon not found: icon_index=%s count=%s",
        icon_index,
        count,
    )
    return False


def _click_teleport_button(
    context: Context,
    icon_path: str,
    *,
    ocr_threshold: float,
) -> bool:
    frame = _wait_screen_still(context, max_wait=5.0)
    if frame is None or _is_stopping(context):
        return False
    detail = _ocr_match(
        context,
        frame,
        "传送",
        TELEPORT_BUTTON_ROI,
        threshold=ocr_threshold,
    )
    if _is_stopping(context):
        return False
    if detail is None:  # 未识别到传送，可能是多个兴趣点叠加，进一步判断
        icon_template = _load_template(icon_path)
        matched, score, ix, iy = _match_template(
            frame,
            [737, 305, 43, 126],
            icon_template,
            0.5,
        )
        logger.info(
            "MapTeleport teleport button fallback: match=%s score=%.3f pos=(%s,%s)",
            matched,
            score,
            ix,
            iy,
        )
        if not matched:
            return False

        if not _click_rect(
            context, [ix, iy, icon_template.shape[1], icon_template.shape[0]]
        ) or not interruptible_wait(context, 0.5):
            return False

        frame = _wait_screen_still(context, max_wait=3.0)
        if frame is None or _is_stopping(context):
            return False
        detail = _ocr_match(
            context,
            frame,
            "传送",
            TELEPORT_BUTTON_ROI,
            threshold=ocr_threshold,
        )
        if detail is None:
            return False

    rect = _detail_box(detail) or TELEPORT_BUTTON_ROI
    return _click_rect_action(context, rect)


def _wait_teleport_loading(context: Context) -> bool:
    try:
        if not run_task_checked(context, "SceneLoadingType1"):
            return False
        # 加载任务可经 NextTrue 结束，必须用新截图确认大世界，不能仅依赖子任务成功。
        frame = _screencap(context)
        if frame is None or _is_stopping(context):
            return False
        result = context.run_recognition("InWorld", frame)
        return bool(not _is_stopping(context) and result and result.hit)
    except Exception as exc:
        logger.error("MapTeleport wait loading failed: %s", exc)
        return False


def _run_map_teleport_flow(
    context: Context | None,
    teleport_point_id: str,
    *,
    points_file: str = DEFAULT_TELEPORT_POINTS_FILE,
    template_threshold: float = DEFAULT_TEMPLATE_THRESHOLD,
    ocr_threshold: float = DEFAULT_OCR_THRESHOLD,
    max_area_switches: int = DEFAULT_MAX_AREA_SWITCHES,
    action_delay: float = DEFAULT_ACTION_DELAY,
) -> bool:
    teleport_point = load_teleport_point(
        teleport_point_id,
        points_file=points_file,
    )
    logger.info(
        "MapTeleport flow resolved: id=%s name=%s area=%s icon_index=%s point=%s",
        teleport_point.id,
        teleport_point.name,
        teleport_point.area_name,
        teleport_point.icon_index,
        teleport_point.point,
    )

    message = "准备使用地图传送：%s（%s，第 %s 个）" % (
        teleport_point.name,
        teleport_point.area_name,
        teleport_point.icon_index,
    )
    _notify(context, message)

    if context is None:
        logger.error("MapTeleport needs Maa context to operate UI")
        return False

    controller = context.tasker.controller
    map_index_icon_template = _load_template(MAP_INDEX_ICON_TEMPLATE)
    area_next_template = _load_template(AREA_NEXT_BTN_TEMPLATE)
    sub_selection_btn_template = _load_template(SUB_SELECTION_BTN_TEMPLATE)

    if not _ensure_in_world(context):
        _notify(context, "地图传送失败：当前未确认处于大世界界面")
        return False

    if not _press_key_action(context, KEY_M):
        _notify(context, "地图传送失败：打开地图按键失败")
        return False
    if not interruptible_wait(context, DEFAULT_POST_MAP_OPEN_DELAY):
        return False

    if not _drag_zoom_control(context):
        _notify(context, "地图传送失败：缩放调节失败")
        return False

    if not _click_map_index(context, map_index_icon_template, template_threshold):
        _notify(context, "地图传送失败：未找到地图索引按钮")
        return False

    if (
        _wait_ocr_text(
            context,
            "地图索引",
            MAP_INDEX_TITLE_ROI,
            threshold=ocr_threshold,
        )
        is None
    ):
        _notify(context, "地图传送失败：未进入地图索引")
        return False

    if not _switch_to_area(
        context,
        teleport_point.area_name,
        area_next_template,
        template_threshold=template_threshold,
        ocr_threshold=ocr_threshold,
        max_switches=max_area_switches,
        action_delay=action_delay,
    ):
        _notify(context, "地图传送失败：未找到地区 %s" % teleport_point.area_name)
        return False

    if not _click_main_selection(
        context,
        teleport_point.selection_name,
        ocr_threshold=ocr_threshold,
    ):
        _notify(context, "地图传送失败：未找到主选项")
        return False

    if not interruptible_wait(context, action_delay):
        return False

    if not _click_teleport_icon(
        context,
        sub_selection_btn_template,
        teleport_point.icon_path,
        teleport_point.icon_index,
        template_threshold=template_threshold,
    ):
        _notify(
            context,
            "地图传送失败：未找到第 %s 个传送图标" % teleport_point.icon_index,
        )
        return False

    # 选中传送点后先退出地图指引，再点屏幕中央附近打开最终传送确认框。
    if _wait_screen_still(context, max_wait=5.0) is None or _is_stopping(context):
        return False
    if not _press_key_action(context, KEY_ESC):
        _notify(context, "地图传送失败：退出地图按键失败")
        return False
    if not interruptible_wait(context, DEFAULT_FINAL_CONFIRM_DELAY):
        return False
    if not _click_rect_action(
        context,
        [TELEPORT_CONFIRM_POINT[0] - 1, TELEPORT_CONFIRM_POINT[1] - 1, 2, 2],
    ):
        _notify(context, "地图传送失败：确认传送点击失败")
        return False

    if not _click_teleport_button(
        context, teleport_point.icon_path, ocr_threshold=ocr_threshold
    ):
        _notify(context, "地图传送失败：未找到传送按钮")
        return False

    # 点击“传送”后交给通用加载节点等待，加载图消失即返回。
    if not interruptible_wait(context, action_delay):
        return False
    if not _wait_teleport_loading(context):
        _notify(context, "地图传送失败：等待传送加载结束失败")
        return False

    if _is_stopping(context):
        return False
    _notify(context, "地图传送流程已完成：%s" % teleport_point.name)
    return not _is_stopping(context)


def run_map_teleport_flow(
    context: Context | None,
    teleport_point_id: str,
    *,
    points_file: str = DEFAULT_TELEPORT_POINTS_FILE,
    template_threshold: float = DEFAULT_TEMPLATE_THRESHOLD,
    ocr_threshold: float = DEFAULT_OCR_THRESHOLD,
    max_area_switches: int = DEFAULT_MAX_AREA_SWITCHES,
    action_delay: float = DEFAULT_ACTION_DELAY,
) -> bool:
    if context is None or _is_stopping(context):
        return False
    try:
        if (
            not math.isfinite(action_delay)
            or action_delay < 0
            or not math.isfinite(template_threshold)
            or not 0 <= template_threshold <= 1
            or not math.isfinite(ocr_threshold)
            or not 0 <= ocr_threshold <= 1
            or not isinstance(max_area_switches, int)
            or max_area_switches < 1
        ):
            raise ValueError("MapTeleport delays, thresholds or area limit are invalid")
        return _run_map_teleport_flow(
            context,
            teleport_point_id,
            points_file=points_file,
            template_threshold=template_threshold,
            ocr_threshold=ocr_threshold,
            max_area_switches=max_area_switches,
            action_delay=action_delay,
        )
    except TaskCancelledError:
        logger.debug("MapTeleport flow cancelled")
    except Exception as exc:
        logger.error("MapTeleport flow failed: %s", exc)
        _notify(context, "地图传送失败：%s" % exc)
    return False


if AgentServer is not None and CustomAction is not None:

    @AgentServer.custom_action("map_teleport_to_point")
    class MapTeleportToPointAction(CustomAction):
        def run(
            self, context: Context, argv: CustomAction.RunArg
        ) -> CustomAction.RunResult:
            try:
                params = parse_params(argv.custom_action_param)
                teleport_point_id = str(
                    params.get("teleport_point_id", params.get("teleport_id", ""))
                ).strip()
                if not teleport_point_id:
                    raise ValueError("teleport_point_id is required")
                points_file = str(
                    params.get("teleport_points_file", DEFAULT_TELEPORT_POINTS_FILE)
                ).strip()
                template_threshold = float(
                    params.get("template_threshold", DEFAULT_TEMPLATE_THRESHOLD)
                )
                ocr_threshold = float(
                    params.get("ocr_threshold", DEFAULT_OCR_THRESHOLD)
                )
                max_area_switches = int(
                    params.get("max_area_switches", DEFAULT_MAX_AREA_SWITCHES)
                )
                action_delay = float(params.get("action_delay", DEFAULT_ACTION_DELAY))
            except Exception as exc:
                logger.error("MapTeleportToPoint param invalid: %s", exc)
                _notify(context, "地图传送参数错误：%s" % exc)
                return CustomAction.RunResult(success=False)

            try:
                success = run_map_teleport_flow(
                    context,
                    teleport_point_id,
                    points_file=points_file,
                    template_threshold=template_threshold,
                    ocr_threshold=ocr_threshold,
                    max_area_switches=max_area_switches,
                    action_delay=action_delay,
                )
            except Exception as exc:
                logger.error("MapTeleportToPoint failed: %s", exc)
                _notify(context, "地图传送失败：%s" % exc)
                return CustomAction.RunResult(success=False)
            return CustomAction.RunResult(success=success)
