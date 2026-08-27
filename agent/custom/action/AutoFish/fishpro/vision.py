"""FishPro 控条的识别层。

完整移植附件 ``autofish.py`` 的 ``find_green_target`` 与
``find_yellow_cursor``：形态学开闭运算、多重形状过滤、候选打分择优，
以及光标与绿条的纵带一致性、内部加分、越界惩罚和帧间连续性惩罚。

本模块只做识别，不持有任何控制状态，便于离线用合成图验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import cv2
import numpy as np

from .config import FishProConfig


@dataclass(frozen=True)
class Detection:
    """一次识别命中的矩形与派生量，坐标位于 ROI 局部坐标系。"""

    center_x: float
    left: int
    right: int
    top: int
    bottom: int
    box: tuple[int, int, int, int]
    area: float

    @property
    def width(self) -> float:
        return float(self.right - self.left)

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0


def clip_roi(
    image: Optional[np.ndarray], roi: Sequence[int]
) -> Optional[np.ndarray]:
    """按 ROI 裁剪画面，越界或空图返回 ``None``。"""
    if image is None:
        return None
    if getattr(image, "ndim", 0) != 3 or image.shape[2] < 3:
        return None

    height, width = image.shape[:2]
    x, y, w, h = (int(value) for value in roi)
    if w <= 0 or h <= 0:
        return None
    if x < 0 or y < 0 or x + w > width or y + h > height:
        return None

    return np.ascontiguousarray(image[y : y + h, x : x + w, :3])


def resolve_roi(
    config: FishProConfig, frame_width: int, frame_height: int
) -> tuple[int, int, int, int]:
    """按配置解析 ROI；比例模式直接用画面尺寸换算。"""
    if config.use_ratio_roi:
        x1 = int(frame_width * config.roi_left_ratio)
        y1 = int(frame_height * config.roi_top_ratio)
        x2 = int(frame_width * config.roi_right_ratio)
        y2 = int(frame_height * config.roi_bottom_ratio)
        return x1, y1, max(0, x2 - x1), max(0, y2 - y1)

    x, y, w, h = config.roi_px
    return int(x), int(y), int(w), int(h)


def find_green_target(
    roi: Optional[np.ndarray], config: FishProConfig
) -> Optional[Detection]:
    """识别顶部绿色安全区。

    过滤过大的绿色背景块、过高的区域和纵横比不符的块，再按宽度、面积、
    长宽比与纵向位置打分择优。
    """
    if roi is None or roi.size == 0:
        return None

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    roi_h, roi_w = roi.shape[:2]

    mask = cv2.inRange(
        hsv,
        np.array(config.green_min_hsv, dtype=np.uint8),
        np.array(config.green_max_hsv, dtype=np.uint8),
    )
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 3), np.uint8))
    # 光标覆盖绿条中段会把掩码切成左右两块，若沿用「取最大连通域」会只识别
    # 到半条，导致中心与宽度同时算错。先做水平闭运算把窄缝桥接回去。
    if config.green_bridge_px > 1:
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            np.ones((1, int(config.green_bridge_px)), np.uint8),
        )

    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    max_height = max(
        config.green_min_height, int(roi_h * config.green_max_height_ratio)
    )
    max_area = int(roi_w * roi_h * config.green_max_area_ratio)

    best: Optional[tuple[float, int, int, int, int, float]] = None
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        box_area = w * h
        if box_area <= 0:
            continue
        contour_area = float(cv2.contourArea(contour))

        aspect_ratio = w / max(1, h)
        fill_ratio = contour_area / box_area
        y_center_ratio = (y + h / 2) / max(1, roi_h)

        if box_area < config.green_min_area:
            continue
        if w < config.green_min_width:
            continue
        if h < config.green_min_height:
            continue
        if h > max_height:
            continue
        if box_area > max_area:
            continue
        if aspect_ratio < config.green_min_aspect_ratio:
            continue
        if y_center_ratio > config.green_max_y_ratio:
            continue
        if fill_ratio < config.green_min_fill_ratio:
            continue

        score = (
            w * 3.0
            + contour_area * 0.8
            + aspect_ratio * 20.0
            - h * 6.0
            - abs(y_center_ratio - 0.45) * 40.0
        )
        if best is None or score > best[0]:
            best = (score, x, y, w, h, contour_area)

    if best is None:
        return None

    _, x, y, w, h, contour_area = best
    return Detection(
        center_x=float(x + w // 2),
        left=int(x),
        right=int(x + w),
        top=int(y),
        bottom=int(y + h),
        box=(int(x), int(y), int(w), int(h)),
        area=contour_area,
    )


def _build_yellow_mask(
    roi: np.ndarray, config: FishProConfig
) -> np.ndarray:
    """主色域 + 黄昏暗色域 HSV 掩码，再叠加 BGR 通道约束。"""
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    channels = roi.astype(np.int16)
    b = channels[:, :, 0]
    g = channels[:, :, 1]
    r = channels[:, :, 2]

    primary_mask = cv2.inRange(
        hsv,
        np.array(config.yellow_primary_min_hsv, dtype=np.uint8),
        np.array(config.yellow_primary_max_hsv, dtype=np.uint8),
    )
    dusk_mask = cv2.inRange(
        hsv,
        np.array(config.yellow_dusk_min_hsv, dtype=np.uint8),
        np.array(config.yellow_dusk_max_hsv, dtype=np.uint8),
    )

    color_mask = (
        (g >= config.yellow_green_floor)
        & (r >= config.yellow_red_floor)
        & (((r + g) // 2 - b) >= config.yellow_blue_gap_min)
        & (np.abs(r - g) <= config.yellow_rg_diff_max)
        & (r >= b + config.yellow_red_blue_gap)
        & (g >= b + config.yellow_green_blue_gap)
    )

    mask = cv2.bitwise_and(
        cv2.bitwise_or(primary_mask, dusk_mask),
        color_mask.astype(np.uint8) * 255,
    )
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 2), np.uint8))
    return mask


def find_yellow_cursor(
    roi: Optional[np.ndarray],
    config: FishProConfig,
    last_center_x: Optional[float] = None,
    target: Optional[Detection] = None,
) -> Optional[Detection]:
    """识别黄色指针。

    兼容黄昏/偏暗场景，并优先选择与绿条同一纵向带、位于绿条内部、
    且接近上一帧位置的候选。
    """
    if roi is None or roi.size == 0:
        return None

    mask = _build_yellow_mask(roi, config)
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    target_center_y: Optional[float] = None
    target_left: Optional[float] = None
    target_right: Optional[float] = None
    target_inner_left: Optional[float] = None
    target_inner_right: Optional[float] = None
    if target is not None:
        target_center_y = (target.top + target.bottom) / 2.0
        target_inner_left = float(target.left)
        target_inner_right = float(target.right)
        target_width = max(1.0, target_inner_right - target_inner_left)
        dynamic_x_margin = max(
            float(config.yellow_target_x_margin_px),
            target_width * config.yellow_target_x_margin_ratio,
        )
        target_left = target_inner_left - dynamic_x_margin
        target_right = target_inner_right + dynamic_x_margin

    best: Optional[tuple[float, int, int, int, int, float, float]] = None
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        box_area = w * h
        if box_area <= 0:
            continue
        contour_area = float(cv2.contourArea(contour))

        aspect_ratio = h / max(1, w)
        fill_ratio = contour_area / box_area
        center_x = float(x + w // 2)
        center_y = y + h / 2.0

        if box_area < config.yellow_min_area:
            continue
        if h < config.yellow_min_height:
            continue
        if w > config.yellow_max_width:
            continue
        if aspect_ratio < config.yellow_min_aspect_ratio:
            continue
        if fill_ratio < config.yellow_min_fill_ratio:
            continue

        score = h * 7.0 + contour_area * 3.0 - w * 3.0

        if target_center_y is not None:
            y_distance = abs(center_y - target_center_y)
            if y_distance > config.yellow_target_y_tolerance_px:
                continue

            x_margin_distance = 0.0
            if center_x < target_left:
                x_margin_distance = target_left - center_x
            elif center_x > target_right:
                x_margin_distance = center_x - target_right

            x_distance = 0.0
            if center_x < target_inner_left:
                x_distance = target_inner_left - center_x
            elif center_x > target_inner_right:
                x_distance = center_x - target_inner_right

            score -= y_distance * 4.5
            score -= x_distance * 0.35
            score -= x_margin_distance * 1.8
            if target_inner_left <= center_x <= target_inner_right:
                score += 18.0

        if last_center_x is not None:
            distance_penalty = min(
                abs(center_x - last_center_x),
                float(config.yellow_track_bias_px),
            )
            score -= distance_penalty * 2.4

        if best is None or score > best[0]:
            best = (score, x, y, w, h, contour_area, center_x)

    if best is None:
        return None

    _, x, y, w, h, contour_area, center_x = best
    return Detection(
        center_x=center_x,
        left=int(x),
        right=int(x + w),
        top=int(y),
        bottom=int(y + h),
        box=(int(x), int(y), int(w), int(h)),
        area=contour_area,
    )


def detect_control(
    roi: Optional[np.ndarray],
    config: FishProConfig,
    last_cursor_center_x: Optional[float] = None,
) -> tuple[Optional[Detection], Optional[Detection]]:
    """一次识别绿条与光标，光标识别复用绿条结果做约束。"""
    target = find_green_target(roi, config)
    if target is None:
        return None, None
    cursor = find_yellow_cursor(
        roi, config, last_center_x=last_cursor_center_x, target=target
    )
    return target, cursor
