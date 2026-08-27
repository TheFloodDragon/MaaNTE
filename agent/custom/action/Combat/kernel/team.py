"""队伍 UI 判定：在队、黑屏、角色槽位高亮打分。

从 ``pinkpaw_core3`` 原样搬移为纯函数（输入截图，无 IO、无副作用），
方便离线用合成图做等价性比对：

- ``is_black_screen``            <- ``_is_black_screen_in_image``
- ``is_in_team``                 <- ``_is_in_team_in_image``
- ``slot_roi_score``             <- ``_current_char_roi_score``
- ``slot_scores``                <- ``_current_char_scores``
- ``slot_core_scores``           <- ``_current_char_core_scores``
- ``is_slot_score_accepted``     <- ``_is_current_char_score_accepted``
- ``is_slot2_core_accepted``     <- ``_is_slot2_core_score_accepted``
- ``current_slot_index``         <- ``get_current_char_index`` 的纯逻辑部分
- ``is_slot_active``             <- ``is_char_at_index`` 的纯逻辑部分
"""

from __future__ import annotations

from .constants import (
    BLACK_SCREEN_BRIGHT_PIXEL_COUNT,
    BLACK_SCREEN_BRIGHT_PIXEL_THRESHOLD,
    BLACK_SCREEN_MEAN_THRESHOLD,
    BLACK_SCREEN_SAMPLE_STRIDE,
    CURRENT_CHAR_COLORED_MIN_SATURATION,
    CURRENT_CHAR_CORE_SCORE_WEIGHT,
    CURRENT_CHAR_MARKER_CORE_ROI,
    CURRENT_CHAR_MARKER_ROI,
    CURRENT_CHAR_SLOT2_CORE_MIN_MARGIN,
    CURRENT_CHAR_SLOT2_CORE_MIN_SCORE,
    CURRENT_CHAR_SLOT_COLORED_THRESHOLDS,
    CURRENT_CHAR_SLOT_COUNT,
    CURRENT_CHAR_SLOT_MIN_MARGIN,
    CURRENT_CHAR_SLOT_MIN_SCORE,
    CURRENT_CHAR_SLOT_SCORE_BONUS,
    CURRENT_CHAR_SLOT_SPACING,
    CURRENT_CHAR_SLOT_WHITE_THRESHOLDS,
    CURRENT_CHAR_WHITE_MAX_SATURATION,
    TEAM_HEALTH_SLASH_ROI,
    TEAM_SLASH_BRIGHT_THRESHOLD,
    TEAM_SLASH_MAX_SATURATION,
    TEAM_SLASH_MIN_PIXELS,
)
from .frames import as_bgr_image, crop_scaled_roi, np


def is_black_screen(image) -> bool:
    """用画面亮度判断是否处于黑屏/加载状态，避免误判角色死亡。"""
    bgr = as_bgr_image(image)
    if bgr is None:
        return False
    sample = bgr[::BLACK_SCREEN_SAMPLE_STRIDE, ::BLACK_SCREEN_SAMPLE_STRIDE]
    if sample.size == 0:
        return False
    max_ch = sample.max(axis=2)
    return (
        float(max_ch.mean()) <= BLACK_SCREEN_MEAN_THRESHOLD
        and int((max_ch >= BLACK_SCREEN_BRIGHT_PIXEL_THRESHOLD).sum())
        <= BLACK_SCREEN_BRIGHT_PIXEL_COUNT
    )


def is_in_team(image) -> bool:
    """检测底部队伍 UI 特征，判断当前是否已回到可操作界面。

    注意：``np`` 缺失时返回 ``True``，与 core3 一致（宁可认为在队，
    也不要因为环境缺依赖而把战斗流程卡死）。
    """
    if np is None:
        return True
    roi = crop_scaled_roi(image, TEAM_HEALTH_SLASH_ROI)
    if roi is None:
        return False
    max_ch = roi.max(axis=2)
    min_ch = roi.min(axis=2)
    bright = (max_ch >= TEAM_SLASH_BRIGHT_THRESHOLD) & (
        (max_ch - min_ch) <= TEAM_SLASH_MAX_SATURATION
    )
    return int(bright.sum()) >= TEAM_SLASH_MIN_PIXELS


def slot_roi_score(image, roi, index) -> int:
    """计算指定角色槽位高亮区域中的亮色/彩色像素分数。"""
    crop = crop_scaled_roi(image, roi)
    if crop is None:
        return 0
    max_ch = crop.max(axis=2)
    min_ch = crop.min(axis=2)
    sat = max_ch - min_ch
    white_threshold = CURRENT_CHAR_SLOT_WHITE_THRESHOLDS[index]
    colored_threshold = CURRENT_CHAR_SLOT_COLORED_THRESHOLDS[index]
    white = (max_ch >= white_threshold) & (sat <= CURRENT_CHAR_WHITE_MAX_SATURATION)
    colored = (max_ch >= colored_threshold) & (
        sat >= CURRENT_CHAR_COLORED_MIN_SATURATION
    )
    return int((white | colored).sum())


def slot_scores(image) -> list[int]:
    """计算四个角色槽位的大区域高亮分数，用于判断当前角色。"""
    if np is None:
        return [0, 0, 0, 0]
    scores = []
    for index in range(CURRENT_CHAR_SLOT_COUNT):
        broad_roi = list(CURRENT_CHAR_MARKER_ROI)
        broad_roi[1] += CURRENT_CHAR_SLOT_SPACING * index
        score = slot_roi_score(image, broad_roi, index)
        score += CURRENT_CHAR_SLOT_SCORE_BONUS[index]
        scores.append(score)
    return scores


def slot_core_scores(image) -> list[int]:
    """计算四个角色槽位的小核心高亮分数，给二号位暗头像兜底。"""
    if np is None:
        return [0, 0, 0, 0]
    scores = []
    for index in range(CURRENT_CHAR_SLOT_COUNT):
        core_roi = list(CURRENT_CHAR_MARKER_CORE_ROI)
        core_roi[1] += CURRENT_CHAR_SLOT_SPACING * index
        scores.append(
            slot_roi_score(image, core_roi, index) * CURRENT_CHAR_CORE_SCORE_WEIGHT
        )
    return scores


def is_slot_score_accepted(scores, index) -> bool:
    """用最低分和领先差值判断目标槽位高亮是否可信。"""
    if not scores or not 0 <= index < len(scores):
        return False
    target_score = scores[index]
    other_scores = [score for idx, score in enumerate(scores) if idx != index]
    best_other = max(other_scores) if other_scores else 0
    min_score = CURRENT_CHAR_SLOT_MIN_SCORE[index]
    min_margin = CURRENT_CHAR_SLOT_MIN_MARGIN[index]
    return target_score >= min_score and target_score - best_other >= min_margin


def is_slot2_core_accepted(image) -> bool:
    """二号位头像偏暗时，用核心高亮区域单独确认是否切到二号位。"""
    scores = slot_core_scores(image)
    target_score = scores[1]
    best_other = max(score for idx, score in enumerate(scores) if idx != 1)
    return (
        target_score >= CURRENT_CHAR_SLOT2_CORE_MIN_SCORE
        and target_score - best_other >= CURRENT_CHAR_SLOT2_CORE_MIN_MARGIN
    )


def current_slot_index(image) -> int:
    """返回当前高亮的角色槽位索引；无法可靠判断时返回 -1。"""
    scores = slot_scores(image)
    if not scores:
        return -1
    best_idx = max(range(len(scores)), key=lambda idx: scores[idx])
    if is_slot_score_accepted(scores, best_idx):
        return best_idx
    return -1


def is_slot_active(image, index) -> bool:
    """判断当前高亮角色是否为指定槽位，二号位会额外走核心兜底。"""
    index = int(index)
    if is_slot_score_accepted(slot_scores(image), index):
        return True
    if index == 1:
        return is_slot2_core_accepted(image)
    return False
