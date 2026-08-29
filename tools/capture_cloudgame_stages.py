"""实机采集云异环从启动到进入游戏的全过程截图与 OCR 文案。

排队等待的识别必须建立在真实截图上，而这些中间态（未登录、队列选择、排队中、
串流加载）都是一次性的，手工掐时间抓不全。这个脚本按帧差自动存图，并对每张
变化帧跑一次 OCR，把文案与 box 写进 JSONL，供后续标定直接引用。

**会消耗账号的云游戏时长**：进入游戏后开始计费。已获用户明确同意才应运行。

用法::

    python tools/capture_cloudgame_stages.py --duration 600
    python tools/capture_cloudgame_stages.py --duration 600 --no-click   # 不点开始游戏

产物::

    debug/cloudgame/stages/<时间戳>/NNN_<diff>.png   变化帧
    debug/cloudgame/stages/<时间戳>/ocr.jsonl        每帧的 OCR 结果
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

WINDOW_CLASS_PREFIX = "Qt"
SW_RESTORE = 9

# 「开始游戏」按钮：由 debug/cloudgame/023714_restored.png 的 OCR 标定
# （box=[973,566,75,24]，分数 1.00）。点击取其中心。
START_BUTTON_TEXT = "开始游戏"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def make_dpi_aware() -> None:
    """声明 DPI 感知，否则 GetWindowRect 拿到的是逻辑坐标。

    实测：系统缩放 125% 时，非感知进程读到 1024x576，而窗口物理尺寸
    （也是截图尺寸）是 1280x720。不声明就会把两套坐标系混在一起。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def restore_window(hwnd: int) -> None:
    """最小化的窗口截出来是全白，必须先恢复。"""
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        log("窗口处于最小化，正在恢复")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(1.5)


def bring_to_front(hwnd: int) -> bool:
    """把窗口提到前台。

    ``CloudGame-Front`` 用的是 ``mouse: Seize``——真实鼠标输入，点的是屏幕
    坐标。窗口不在前台时点击会落到别的窗口上，实测表现为「点了没反应、
    画面 8 分钟零变化」。所以点击前必须置前。
    """
    user32 = ctypes.windll.user32
    try:
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.8)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def window_size(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def find_window():
    from maa.toolkit import Toolkit

    for window in Toolkit.find_desktop_windows():
        if window.hwnd and window.class_name.startswith(WINDOW_CLASS_PREFIX):
            return window
    return None


def launch_client() -> int | None:
    """用仓库自己的 launcher 拉起客户端，返回窗口 hwnd。

    注意：导入 ``custom.action.*`` 会把进程带进 AgentServer 上下文，之后
    ``Toolkit.find_desktop_windows()`` 会直接抛
    "Toolkit is not available in AgentServer context."。所以这里必须直接用
    launcher 返回的 hwnd，不能再回头去 Toolkit 找窗口。
    """
    from custom.action.CloudGame.launcher import launch_cloud_game

    result = launch_cloud_game(logger=log)
    log(
        f"launcher: started={result.started} already={result.already_running} "
        f"hwnd={result.hwnd} {result.message}"
    )
    return result.hwnd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=600.0, help="采集总秒数")
    parser.add_argument("--interval", type=float, default=1.5, help="轮询间隔")
    parser.add_argument("--threshold", type=float, default=4.0, help="帧差阈值")
    parser.add_argument(
        "--no-click", action="store_true", help="不点「开始游戏」，只旁观"
    )
    args = parser.parse_args()

    import cv2
    import numpy as np
    from maa.controller import Win32Controller
    from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    Toolkit.init_option("./debug")
    make_dpi_aware()

    # 先用 Toolkit 找窗口；找不到再启动。顺序不能颠倒：启动那条路会导入
    # agent 模块并使 Toolkit 失效。
    window = find_window()
    if window is not None:
        hwnd = window.hwnd
        log(f"窗口 hwnd={hwnd} class={window.class_name}")
    else:
        log("未找到云异环窗口，尝试启动客户端")
        hwnd = launch_client()
        if hwnd is None:
            log("启动失败或未拿到窗口句柄")
            return 1

    restore_window(hwnd)
    log(f"窗口尺寸 {window_size(hwnd)}")

    # 与 interface.json 的 CloudGame-Front 保持一致：FramePool + Seize。
    # 用别的组合采到的画面与实机任务看到的不是同一套，标定就白做了。
    controller = Win32Controller(
        hwnd,
        screencap_method=MaaWin32ScreencapMethodEnum.FramePool,
        mouse_method=MaaWin32InputMethodEnum.Seize,
        keyboard_method=MaaWin32InputMethodEnum.Seize,
    )
    if not controller.post_connection().wait().succeeded:
        log("控制器连接失败")
        return 1

    resource = Resource()
    if not resource.post_bundle(
        str(REPO / "assets" / "resource" / "base")
    ).wait().succeeded:
        log("资源加载失败（OCR 模型可能缺失，先跑 tools/ci/configure.py）")
        return 1
    tasker = Tasker()
    if not tasker.bind(resource, controller):
        log("Tasker 绑定失败")
        return 1

    out_dir = REPO / "debug" / "cloudgame" / "stages" / datetime.now().strftime(
        "%H%M%S"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / "ocr.jsonl"
    log(f"产物目录 {out_dir.relative_to(REPO)}")

    def grab():
        if not controller.post_screencap().wait().succeeded:
            return None
        return controller.cached_image

    def ocr(image):
        """整屏 OCR（only_rec=False 才会先检测再逐块识别）。"""
        job = tasker.post_recognition(
            JRecognitionType.OCR,
            JOCR(roi=[0, 0, image.shape[1], image.shape[0]], only_rec=False),
            image,
        )
        detail = job.wait().get()
        items = []
        for node in getattr(detail, "nodes", None) or []:
            reco = getattr(node, "recognition", None)
            for res in getattr(reco, "all_results", None) or []:
                text = getattr(res, "text", "")
                if not text:
                    continue
                items.append(
                    {
                        "text": text,
                        "box": list(getattr(res, "box", []) or []),
                        "score": round(float(getattr(res, "score", 0.0)), 3),
                    }
                )
        return items

    def record(image, index: int, diff: float, note: str) -> list:
        path = out_dir / f"{index:03d}_{note}.png"
        cv2.imwrite(str(path), image)
        items = ocr(image)
        with jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "index": index,
                        "file": path.name,
                        "diff": round(diff, 2),
                        "note": note,
                        "elapsed": round(time.monotonic() - started, 1),
                        "size": [image.shape[1], image.shape[0]],
                        "texts": items,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        preview = " | ".join(i["text"] for i in items[:10])
        log(f"#{index:03d} diff={diff:.1f} {note} -> {len(items)} 条: {preview}")
        return items

    started = time.monotonic()
    index = 0
    image = grab()
    if image is None:
        log("首帧截图失败")
        return 1
    if image.mean() > 250:
        log("警告：画面接近全白，窗口可能仍处于最小化/遮挡状态")
    items = record(image, index, 0.0, "start")
    last = image

    # 点「开始游戏」
    if not args.no_click:
        hit = next((i for i in items if START_BUTTON_TEXT in i["text"]), None)
        if hit and len(hit["box"]) == 4:
            x, y, w, h = hit["box"]
            cx, cy = x + w // 2, y + h // 2
            if bring_to_front(hwnd):
                log("窗口已置前")
            else:
                log("警告：置前失败，Seize 点击可能落到别的窗口")
            log(f"点击「{START_BUTTON_TEXT}」于 ({cx},{cy})，来自本帧 OCR")
            controller.post_click(cx, cy).wait()
        else:
            log(f"本帧没找到「{START_BUTTON_TEXT}」，跳过点击（可能已在游戏中）")

    deadline = started + args.duration
    while time.monotonic() < deadline:
        time.sleep(args.interval)
        current = grab()
        if current is None:
            continue
        if current.shape != last.shape:
            diff = 255.0
        else:
            diff = float(
                np.mean(
                    cv2.absdiff(
                        cv2.resize(last, (160, 90)), cv2.resize(current, (160, 90))
                    )
                )
            )
        if diff < args.threshold:
            continue
        index += 1
        record(current, index, diff, "chg")
        last = current

    log(f"采集结束，共 {index + 1} 帧，产物在 {out_dir.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
