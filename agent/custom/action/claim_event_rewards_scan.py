"""仅证明限时侧栏完整性；导航、点击和七格领奖仍由 Pipeline 负责。"""

import json

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils.logger import logger

SCAN_NODE = "ClaimEventRewardsScanSidebar"
HEADER_NODE = "ClaimEventRewardsScanHeader"
NAMES_NODE = "ClaimEventRewardsScanNames"
TARGET_NODE = "ClaimEventRewardsEventTabText"
UP_NODE = "ClaimEventRewardsScanUp"
DOWN_NODE = "ClaimEventRewardsScanDown"
EVENT_TAB_NODE = "ClaimEventRewardsEventTab"
NO_EVENT_NODE = "ClaimEventRewardsNoEvent"
TEXT_ROI = (75, 157, 124, 460)
HEADER_ROI = (28, 109, 153, 44)
TARGET_TEXT = "环期赠礼"
MIN_SCORE = 0.8
MAX_SWIPES = 8  # 顶部定位与向底扫描共享预算；没有跨次缓存。


def _text(value):
    return "".join(value.split())


def _inside(box, roi):
    if isinstance(box, (list, tuple)):
        x, y, w, h = box
    else:
        x, y, w, h = box.x, box.y, box.w, box.h
    rx, ry, rw, rh = roi
    return w > 0 and h > 0 and rx < x < rx + rw - w and ry < y < ry + rh - h


def _exact(detail, text, roi):
    if not detail or not detail.hit:
        return False
    return any(
        _text(row.text) == text
        and MIN_SCORE <= row.score <= 1
        and _inside(row.box, roi)
        for row in detail.filtered_results
    )


def _observe(context):
    controller = context.tasker.controller
    if context.tasker.stopping or not controller.post_screencap().wait().succeeded:
        raise ValueError("stopped or fresh capture failed")
    if context.tasker.stopping:
        raise ValueError("scan stopped after capture")
    frame = controller.cached_image
    if not isinstance(frame, np.ndarray):
        raise ValueError("missing image array")
    if frame.shape != (720, 1280, 3) or frame.dtype != np.uint8:
        raise ValueError("expected a 1280x720 BGR uint8 frame")
    scene = context.run_recognition("InEventsMenu", frame)
    header = context.run_recognition(HEADER_NODE, frame)
    target = context.run_recognition(TARGET_NODE, frame)
    if context.tasker.stopping or not scene or not scene.hit:
        raise ValueError("unverified events menu")
    if not _exact(header, "限时", HEADER_ROI):
        raise ValueError("unverified limited-events header")
    if target is None:
        raise ValueError("unknown target recognition")
    if _exact(target, TARGET_TEXT, TEXT_ROI):
        return True, (), None
    detail = context.run_recognition(NAMES_NODE, frame)
    if (
        context.tasker.stopping
        or not detail
        or not detail.hit
        or not detail.all_results
    ):
        raise ValueError("unknown visible sidebar names")
    rows = []
    for row in detail.all_results:  # 包含低可信原始结果，不能用 filtered 隐藏疑似目标。
        text = _text(row.text)
        if (
            not text
            or not MIN_SCORE <= row.score <= 1
            or not _inside(row.box, TEXT_ROI)
        ):
            raise ValueError("uncertain or clipped sidebar text")
        # 与四字目标只相差一个字时，至少保留其中一个完整二字片段。
        if "环期" in text or "赠礼" in text:
            raise ValueError("suspected target without exact recognition")
        box = row.box
        x, y = box[:2] if isinstance(box, (list, tuple)) else (box.x, box.y)
        rows.append((y, x, text))
    names = tuple(row[2] for row in sorted(rows))
    if len(set(names)) != len(names):
        raise ValueError("ambiguous duplicate sidebar names")
    # 仅保留名单区低色度亮文字：不比较立绘、倒计时、NEW/红点、右侧边框或高亮背景。
    pixels = frame[157:617, 75:188]
    low, high = pixels.min(axis=2), pixels.max(axis=2)
    colored = cv2.dilate((high - low > 40).astype(np.uint8), np.ones((5, 5), np.uint8))
    glyphs = (low >= 110) & (colored == 0)
    if not np.any(glyphs):
        raise ValueError("no verifiable text image")
    return False, names, glyphs


def _near(first, second):
    foreground = np.count_nonzero(first | second)
    return np.count_nonzero(first != second) <= foreground * 0.005


@AgentServer.custom_action("claim_event_rewards_scan")
class ClaimEventRewardsScan(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        try:
            if not context.override_next(SCAN_NODE, [EVENT_TAB_NODE]):
                raise ValueError("could not reset scan next")
            raw = argv.custom_action_param
            params = {} if raw in (None, "") else json.loads(raw)
            if params != {} or not isinstance(params, dict):
                raise ValueError("scan accepts no custom_action_param")
            found, names, image = _observe(context)
            if found:
                return CustomAction.RunResult(success=True)
            up, seen = True, [(names, image)]
            for number in range(MAX_SWIPES):
                if context.tasker.stopping:
                    raise ValueError("scan stopped")
                node = UP_NODE if up else DOWN_NODE
                task = context.run_task(
                    node
                )  # 节点契约：正面识别 + 单次 Swipe + 稳定等待，next=[]。
                if not task or not task.status.succeeded or len(task.nodes) != 1:
                    raise ValueError("single-swipe task failed")
                completed = task.nodes[0]
                if completed.name != node or not completed.completed:
                    raise ValueError("single-swipe node did not complete")
                if not completed.action or not completed.action.success:
                    raise ValueError("single-swipe action did not succeed")
                found, current, fresh = _observe(context)
                if found:
                    return CustomAction.RunResult(success=True)
                logger.debug("sidebar scan: step=%d node=%s", number + 1, node)
                if current == names:
                    if not _near(image, fresh):
                        raise ValueError("same OCR but unstable text image")
                    if not up:
                        if context.tasker.stopping:
                            raise ValueError("scan stopped")
                        success = context.override_next(SCAN_NODE, [NO_EVENT_NODE])
                        return CustomAction.RunResult(success=bool(success))
                    up, seen = False, [
                        (current, fresh)
                    ]  # 顶部有新帧证据；向下允许合法重访。
                else:
                    if _near(image, fresh):
                        raise ValueError("changed OCR without text-image progress")
                    for old, old_image in seen:
                        if current == old and _near(fresh, old_image):
                            raise ValueError("sidebar viewport cycle")
                    before, after = (current, names) if up else (names, current)
                    limit = min(len(before), len(after)) + 1
                    overlap = any(before[-n:] == after[:n] for n in range(1, limit))
                    if not overlap:
                        raise ValueError("no ordered page overlap; coverage unknown")
                    seen.append((current, fresh))
                names, image = current, fresh
            raise ValueError("sidebar scan swipe budget exhausted")
        except Exception as exc:
            logger.warning("claim_event_rewards_scan failed: %s", exc)
            return CustomAction.RunResult(success=False)


@AgentServer.custom_action("claim_event_rewards_fail")
class ClaimEventRewardsFail(CustomAction):
    """保持 Pipeline 失败节点的报错结果可见，并返回失败状态。"""

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        return CustomAction.RunResult(success=False)
