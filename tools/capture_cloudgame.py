"""抓取云异环客户端各阶段的真实画面，作为排队识别的标定依据。

为什么需要它：``CloudGame/ready.py`` 刻意不识别云客户端自身界面，因为仓库里
没有任何云客户端 UI 的模板与 ROI，而规范禁止凭空编写识别节点。要让任务能
区分「正在排队」「等待登录」「队列选择」，必须先有真实截图。

用法::

    python tools/capture_cloudgame.py                  # 抓一张，存到 debug/cloudgame/
    python tools/capture_cloudgame.py --label queue    # 带标签命名
    python tools/capture_cloudgame.py --watch 300      # 持续抓 300 秒，画面变化才存

``--watch`` 用于跨阶段采集：启动器从登录页走到排队再到游戏内的过程是一次性的，
盯着它按帧差存图，比手工掐时间可靠。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "debug" / "cloudgame"

# 与 launcher.py 保持一致：云异环窗口类名以 Qt 开头（实机 Qt51517QWindowOwnDC）
WINDOW_CLASS_PREFIX = "Qt"


def find_window():
    """找到云异环窗口；返回 toolkit 的 window 对象或 None。"""
    from maa.toolkit import Toolkit

    for window in Toolkit.find_desktop_windows():
        if window.hwnd and window.class_name.startswith(WINDOW_CLASS_PREFIX):
            return window
    return None


def make_controller(hwnd):
    from maa.controller import Win32Controller
    from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum

    controller = Win32Controller(
        hwnd,
        # GDI 已在本机实测可用；DXGI_DesktopDup 在这台机器上连不上
        screencap_method=MaaWin32ScreencapMethodEnum.GDI,
        mouse_method=MaaWin32InputMethodEnum.Seize,
        keyboard_method=MaaWin32InputMethodEnum.Seize,
    )
    if not controller.post_connection().wait().succeeded:
        return None
    return controller


def grab(controller):
    """截一帧；返回 numpy BGR 数组或 None。"""
    if not controller.post_screencap().wait().succeeded:
        return None
    return controller.cached_image


def save(image, label: str) -> Path:
    import cv2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    name = f"{stamp}_{label}.png" if label else f"{stamp}.png"
    path = OUT_DIR / name
    cv2.imwrite(str(path), image)
    return path


def frame_diff(a, b) -> float:
    """两帧的平均绝对差，用来判断画面是否变了。"""
    import cv2
    import numpy as np

    if a is None or b is None or a.shape != b.shape:
        return 255.0
    small_a = cv2.resize(a, (160, 90))
    small_b = cv2.resize(b, (160, 90))
    return float(np.mean(cv2.absdiff(small_a, small_b)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="", help="文件名标签")
    parser.add_argument(
        "--watch",
        type=float,
        default=0.0,
        help="持续监视的秒数；画面显著变化时存图",
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, help="监视模式的轮询间隔"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=6.0,
        help="监视模式判定画面变化的平均像素差阈值",
    )
    args = parser.parse_args()

    from maa.toolkit import Toolkit

    Toolkit.init_option("./debug")

    window = find_window()
    if window is None:
        print(f"[FAIL] 找不到云异环窗口（类名以 {WINDOW_CLASS_PREFIX} 开头）")
        print("       请先启动云异环客户端")
        return 1
    print(f"[ok] 窗口 hwnd={window.hwnd} class={window.class_name}")

    controller = make_controller(window.hwnd)
    if controller is None:
        print("[FAIL] 控制器连接失败")
        return 1

    image = grab(controller)
    if image is None:
        print("[FAIL] 截图失败")
        return 1
    print(f"[ok] 画面尺寸 {image.shape[1]}x{image.shape[0]}")

    path = save(image, args.label or "shot")
    print(f"[ok] 已保存 {path.relative_to(REPO)}")

    if args.watch <= 0:
        return 0

    print(f"[..] 进入监视模式 {args.watch:.0f}s，画面变化即存图（Ctrl+C 结束）")
    deadline = time.monotonic() + args.watch
    last = image
    count = 1
    while time.monotonic() < deadline:
        time.sleep(args.interval)
        current = grab(controller)
        if current is None:
            continue
        diff = frame_diff(last, current)
        if diff >= args.threshold:
            count += 1
            path = save(current, f"{args.label or 'watch'}{count:02d}")
            print(f"[ok] 变化 {diff:.1f} -> {path.name}")
            last = current
    print(f"[done] 共保存 {count} 张，目录 {OUT_DIR.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
