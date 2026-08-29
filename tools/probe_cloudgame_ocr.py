"""对云异环启动器截图跑 OCR，看哪些文字可读、坐标在哪。

排队等待的整套识别都建立在「OCR 能不能读启动器界面」这个前提上。这个启动器
是风格化的游戏 UI（非系统字体、半透明底、深色背景），不实测就不知道能不能读。
读得出什么、ROI 该怎么标，都由这个脚本的输出决定。

用法::

    python tools/probe_cloudgame_ocr.py debug/cloudgame/xxx.png
    python tools/probe_cloudgame_ocr.py debug/cloudgame/xxx.png --roi 780 320 460 220
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Windows 控制台默认 GBK，OCR 结果里可能出现 emoji 等 GBK 编不出的字符，
# 不换掉 stdout 会在打印时直接崩掉，把已经拿到的识别结果全丢了。
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="截图路径")
    parser.add_argument(
        "--roi",
        nargs=4,
        type=int,
        metavar=("X", "Y", "W", "H"),
        help="只识别这个区域；缺省识别整张图",
    )
    parser.add_argument(
        "--only-rec",
        action="store_true",
        help="跳过文本检测，把整个 ROI 当作一行识别（仅适合单行小 ROI）",
    )
    args = parser.parse_args()

    import cv2
    from maa.controller import Win32Controller
    from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    Toolkit.init_option("./debug")

    path = Path(args.image)
    if not path.is_absolute():
        path = REPO / path
    image = cv2.imread(str(path))
    if image is None:
        print(f"[FAIL] 读不到图片: {path}")
        return 1
    print(f"[ok] 图片 {image.shape[1]}x{image.shape[0]}")

    # OCR 模型在资源包里，必须先加载资源
    # bundle 根是 assets/resource/base（interface.json 的 resource[].path）
    res = Resource()
    if not res.post_bundle(str(REPO / "assets" / "resource" / "base")).wait().succeeded:
        print("[FAIL] 资源加载失败")
        return 1

    # Tasker.bind 要求同时给控制器，识别本身不用它。用云异环窗口即可；
    # 找不到窗口时退回任意桌面窗口，反正只借它满足 bind 的形参。
    windows = Toolkit.find_desktop_windows()
    target = next(
        (w for w in windows if w.hwnd and w.class_name.startswith("Qt")), None
    ) or next((w for w in windows if w.hwnd and w.window_name), None)
    if target is None:
        print("[FAIL] 找不到任何窗口用于绑定控制器")
        return 1

    ctrl = Win32Controller(
        target.hwnd,
        screencap_method=MaaWin32ScreencapMethodEnum.GDI,
        mouse_method=MaaWin32InputMethodEnum.Seize,
        keyboard_method=MaaWin32InputMethodEnum.Seize,
    )
    if not ctrl.post_connection().wait().succeeded:
        print("[FAIL] 控制器连接失败")
        return 1

    tasker = Tasker()
    if not tasker.bind(res, ctrl):
        print("[FAIL] Tasker 绑定失败")
        return 1

    roi = list(args.roi) if args.roi else [0, 0, image.shape[1], image.shape[0]]
    print(f"[..] OCR ROI={roi}")

    # only_rec 的含义容易搞反，实测确认过：
    #   only_rec=True  -> 跳过文本检测，把整个 ROI 当作**一行**去识别。
    #                     对整屏用会返回一条低分垃圾（实测整屏得到 "口" / 0.19）。
    #                     只适合已经框死到单行文本的小 ROI。
    #   only_rec=False -> 先做文本检测再逐块识别，能返回 ROI 内的**多条**文本。
    #                     探查阶段要的是这个。
    job = tasker.post_recognition(
        JRecognitionType.OCR,
        JOCR(roi=roi, only_rec=args.only_rec),
        image,
    )
    finished = job.wait()
    status = getattr(finished, "status", None)
    print(
        f"[..] job status={status} succeeded={getattr(status, 'succeeded', None)}"
    )
    detail = finished.get()

    if detail is None:
        print("[FAIL] 识别返回 None")
        return 1

    # post_recognition 返回的是 TaskDetail，识别结果在 nodes[].recognition 里，
    # 不是 detail 自身。直接读 detail.all_results 会永远是空。
    results = []
    for node in getattr(detail, "nodes", None) or []:
        reco = getattr(node, "recognition", None)
        if reco is None:
            continue
        print(
            f"[..] 节点 {getattr(node, 'name', '?')} hit={getattr(reco, 'hit', None)}"
        )
        results.extend(getattr(reco, "all_results", None) or [])
    print(f"[ok] 共 {len(results)} 条文本")
    print("-" * 68)
    for item in results:
        text = getattr(item, "text", None)
        if not text:
            continue  # 空文本条目对标定没用，跳过
        box = getattr(item, "box", None)
        score = getattr(item, "score", None)
        # box 可能是 list/tuple（x, y, w, h），也可能是带属性的对象
        if isinstance(box, (list, tuple)):
            box_text = "[" + ",".join(str(v) for v in box) + "]"
        elif box is not None:
            box_text = f"[{box.x},{box.y},{box.w},{box.h}]"
        else:
            box_text = "?"
        score_text = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(f"  {box_text:<26} {score_text:<6} {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
