"""把刷本配置里的 ROI 画到一张实机截图上，供肉眼核对。

## 为什么需要这个

刷本配置里的每个 ROI 都是「在哪一片里找这行字」。填错的后果不是报错，
而是 OCR 找不到目标 -> 步骤超时 -> 整轮失败，日志里只会写「没识别到」，
看不出到底是位置错了还是游戏界面变了。

所以提供最直接的核对方式：把框画出来，看它有没有框住目标。不需要 OCR
引擎，也不需要游戏在跑——只要一张截图。

## 用法

    python tools/calibrate_dungeon.py <截图路径> [配置名] [-o 输出路径]

配置名默认 ``rabbit_hole``。输出默认写到截图同目录下的
``<截图名>.calibrate.png``。控制台会按编号列出每个框对应的步骤。

框的颜色区分用途：绿色=识别（OCR/模板），红色=直接点击的矩形。
红框必须准，因为它不经过识别、点下去就是那个坐标。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None
    np = None

from agent.custom.action.Dungeon.config import (  # noqa: E402
    ALL_PHASES,
    STEP_CLICK,
    STEP_OCR,
    STEP_SEARCH,
    STEP_TEMPLATE,
    parse_config,
)

DUNGEON_DIR = REPO / "assets" / "resource" / "base" / "dungeon"
BASE_WIDTH = 1280
BASE_HEIGHT = 720

COLOR_RECOGNIZE = (0, 220, 0)
COLOR_CLICK = (0, 60, 235)
COLOR_TEXT = (255, 255, 255)


def collect_boxes(config):
    """展开所有带 ROI 的步骤，包括 search 的子步骤。

    返回 ``(标签, roi, 是否点击类)`` 列表。search 的 until/probe/on_found
    也要一并展开——「打完找出口」的 ROI 就藏在 until 里，漏掉它等于漏掉
    整个 locate 阶段的核对。
    """
    boxes = []

    def visit(step, label):
        if step is None:
            return
        if step.type == STEP_SEARCH:
            visit(step.until, f"{label}.until")
            for index, item in enumerate(step.probe):
                visit(item, f"{label}.probe[{index}]")
            for index, item in enumerate(step.on_found):
                visit(item, f"{label}.on_found[{index}]")
            for index, item in enumerate(step.sweep):
                visit(item, f"{label}.sweep[{index}]")
            return
        if step.type in (STEP_OCR, STEP_TEMPLATE) and step.roi:
            boxes.append((label, tuple(step.roi), False, step))
        elif step.type == STEP_CLICK and step.roi:
            boxes.append((label, tuple(step.roi), True, step))

    for phase in ALL_PHASES:
        for index, step in enumerate(config.steps(phase)):
            visit(step, f"{phase}[{index}]")
    if config.settle is not None:
        visit(config.settle, "settle")
    for index, rect in enumerate(config.difficulty_rects):
        boxes.append((f"difficulty_rects[{index}]", tuple(rect), True, None))
    return boxes


def describe_target(step) -> str:
    if step is None:
        return "难度按钮（绝对坐标点击）"
    if step.type == STEP_OCR:
        return f"OCR {list(step.text)}"
    if step.type == STEP_TEMPLATE:
        return f"模板 {step.path}"
    if step.type == STEP_CLICK:
        return "点击矩形"
    return step.type


def scale(roi, width, height):
    x, y, w, h = roi
    sx = width / BASE_WIDTH
    sy = height / BASE_HEIGHT
    return (
        int(round(x * sx)),
        int(round(y * sy)),
        max(1, int(round(w * sx))),
        max(1, int(round(h * sy))),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把刷本配置的 ROI 画到截图上核对")
    parser.add_argument("screenshot", help="实机截图路径")
    parser.add_argument("dungeon", nargs="?", default="rabbit_hole", help="配置名")
    parser.add_argument("-o", "--output", default=None, help="输出图片路径")
    args = parser.parse_args(argv)

    if cv2 is None or np is None:
        print("[FAIL] 需要 opencv-python 与 numpy：pip install opencv-python numpy")
        return 2

    shot_path = Path(args.screenshot)
    if not shot_path.exists():
        print(f"[FAIL] 截图不存在: {shot_path}")
        return 2

    config_path = DUNGEON_DIR / f"{args.dungeon}.json"
    if not config_path.exists():
        available = sorted(p.stem for p in DUNGEON_DIR.glob("*.json"))
        print(f"[FAIL] 配置不存在: {config_path}（可用: {available}）")
        return 2

    # imdecode 而不是 imread：路径含中文时 imread 在 Windows 上会静默返回 None
    buffer = np.fromfile(str(shot_path), dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        print(f"[FAIL] 无法解码截图: {shot_path}")
        return 2

    config = parse_config(json.loads(config_path.read_text("utf-8")))
    if config.errors:
        print(f"[WARN] 配置有 {len(config.errors)} 处错误，仍按能解析的部分画框：")
        for item in config.errors:
            print(f"        - {item}")

    height, width = image.shape[:2]
    ratio = width / height
    print(f"截图 {shot_path.name}: {width}x{height}（宽高比 {ratio:.3f}）")
    if abs(ratio - BASE_WIDTH / BASE_HEIGHT) > 0.02:
        print(
            "[WARN] 截图不是 16:9。ROI 按 720p 基准等比换算，"
            "非 16:9 时框会整体偏移——请用与实际运行相同比例的窗口重新截图。"
        )

    boxes = collect_boxes(config)
    if not boxes:
        print("[FAIL] 配置里没有任何带 ROI 的步骤")
        return 1

    canvas = image.copy()
    for index, (label, roi, is_click, step) in enumerate(boxes, start=1):
        x, y, w, h = scale(roi, width, height)
        color = COLOR_CLICK if is_click else COLOR_RECOGNIZE
        cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 2)
        tag = str(index)
        (tw, th), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(canvas, (x, y - th - 6), (x + tw + 6, y), color, -1)
        cv2.putText(
            canvas, tag, (x + 3, y - 4),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, COLOR_TEXT, 2,
        )
        kind = "点击" if is_click else "识别"
        target = describe_target(step)
        desc = step.desc if step is not None and step.desc else ""
        print(
            f"  {index:>2}. [{kind}] {label} roi={list(roi)} "
            f"-> 截图坐标 {[x, y, w, h]}"
        )
        print(f"      {target}" + (f"｜{desc}" if desc else ""))

    output = (
        Path(args.output)
        if args.output
        else shot_path.with_suffix(f"{shot_path.suffix}.calibrate.png")
    )
    ok, encoded = cv2.imencode(".png", canvas)
    if not ok:
        print("[FAIL] 编码输出图片失败")
        return 1
    encoded.tofile(str(output))
    print(f"\n已写出标注图: {output}")
    print("绿框=识别范围（框住目标文字即可，不必贴边）；红框=直接点击的坐标，必须准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
