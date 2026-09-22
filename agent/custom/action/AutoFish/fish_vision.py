"""钓鱼控条的高频视觉识别；所有输出坐标均为 1280×720。"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import cv2
import numpy as np

Box = Tuple[int, int, int, int]
CONTROL_ROI = (399, 43, 486, 14)
GREEN_LOWER = np.array((78, 141, 170), dtype=np.uint8)
GREEN_UPPER = np.array((86, 209, 241), dtype=np.uint8)
CURSOR_LOWER = np.array((24, 64, 253), dtype=np.uint8)
CURSOR_UPPER = np.array((30, 154, 255), dtype=np.uint8)
_DUSK_LOWER = np.array((16, 70, 100), dtype=np.uint8)
_DUSK_UPPER = np.array((42, 255, 255), dtype=np.uint8)


def normalize_control_image(image: np.ndarray) -> Optional[np.ndarray]:
    """只归一化实际截图，不使用窗口大小，也不拉伸非 16:9 画面。"""
    if (
        not isinstance(image, np.ndarray)
        or image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] not in (3, 4)
    ):
        return None
    height, width = image.shape[:2]
    if min(width, height) <= 0 or abs(width * 9 - height * 16) > 16:
        return None
    bgr = image[:, :, :3]
    if (width, height) == (1280, 720):
        return bgr
    interpolation = cv2.INTER_AREA if height > 720 else cv2.INTER_LINEAR
    return cv2.resize(bgr, (1280, 720), interpolation=interpolation)


def _components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return [tuple(int(value) for value in row) for row in stats[1:]]


def _green_box(mask: np.ndarray, cursor_mask: np.ndarray) -> Optional[Box]:
    components = sorted(
        (row for row in _components(mask) if row[2] >= 2 and row[3] >= 3 and row[4] >= 6),
        key=lambda row: row[0],
    )
    candidates = []
    for index, (x, y, width, height, area) in enumerate(components):
        # 仅合并有黄色光标遮挡证据的相邻片段，不包围所有同色点。
        right, bottom = x + width, y + height
        for nx, ny, nw, nh, na in components[index + 1 :]:
            gap = nx - right
            if gap > 14:
                break
            overlap_top, overlap_bottom = max(y, ny), min(bottom, ny + nh)
            overlap = overlap_bottom - overlap_top
            if gap <= 0 or overlap < min(height, nh) * 0.7:
                continue
            if abs(y - ny) > 2 or abs(bottom - (ny + nh)) > 2:
                continue
            covered = cv2.countNonZero(cursor_mask[overlap_top:overlap_bottom, right:nx])
            if covered < gap * overlap * 0.45:
                continue
            right, bottom = nx + nw, max(bottom, ny + nh)
            y, area = min(y, ny), area + na
            width, height = right - x, bottom - y
        if (
            12 <= width <= 400
            and 4 <= height <= CONTROL_ROI[3]
            and width / height >= 1.8
            and area >= 32
            and area / (width * height) >= 0.4
        ):
            candidates.append((area + width * 2, (x, y, width, height)))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _cursor_box(
    mask: np.ndarray, green: Optional[Box], last_cursor_center: Optional[float]
) -> Optional[Box]:
    candidates = []
    for x, y, width, height, area in _components(mask):
        if not (1 <= width <= 8 and height >= 7 and area >= 12):
            continue
        if height / width < 1.6 or area / (width * height) < 0.55:
            continue
        center = x + width / 2.0
        score = height * 4.0 + area
        if green is not None:
            gx, gy, gw, gh = green
            if abs((y + height / 2.0) - (gy + gh / 2.0)) > 4:
                continue
            # 不限制左右距离：框外恢复必须仍看得到光标。
            score -= max(gx - center, center - gx - gw, 0.0) * 0.04
        if last_cursor_center is not None and math.isfinite(last_cursor_center):
            score -= min(abs(center + CONTROL_ROI[0] - last_cursor_center), 100.0) * 0.6
        candidates.append((score, (x, y, width, height)))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def detect_control_boxes(
    image: np.ndarray, last_cursor_center: Optional[float] = None
) -> tuple[Optional[Box], Optional[Box]]:
    """一帧/一次 HSV 转换；暗色光标仅在同一 ROI 内受形状约束兜底。"""
    normalized = normalize_control_image(image)
    if normalized is None:
        return None, None
    x, y, width, height = CONTROL_ROI
    roi = normalized[y : y + height, x : x + width]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    green_mask = cv2.inRange(hsv, GREEN_LOWER, GREEN_UPPER)
    primary = cv2.inRange(hsv, CURSOR_LOWER, CURSOR_UPPER)
    dusk = cv2.inRange(hsv, _DUSK_LOWER, _DUSK_UPPER)
    b, g, r = (roi[:, :, channel].astype(np.int16) for channel in range(3))
    yellow_color = (
        (r >= 110)
        & (g >= 95)
        & (((r + g) / 2 - b) >= 44)
        & (np.abs(r - g) <= 42)
    ).astype(np.uint8) * 255
    dusk = cv2.bitwise_and(dusk, yellow_color)
    green = _green_box(green_mask, cv2.bitwise_or(primary, dusk))
    cursor = _cursor_box(primary, green, last_cursor_center)
    if cursor is None:
        cursor = _cursor_box(dusk, green, last_cursor_center)

    def absolute(box):
        if box is None:
            return None
        bx, by, bw, bh = box
        return x + bx, y + by, bw, bh

    return absolute(green), absolute(cursor)
