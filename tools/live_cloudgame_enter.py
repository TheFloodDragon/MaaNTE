"""实机端到端验证：跑真实的 CloudGameLaunch 任务，看它能否自己进入游戏。

## 为什么不用旁路脚本

`InWorld` 是 `And(EscMenuButton, TasksMenuButton)` 的复合识别。在旁路脚本里
重实现这套复合逻辑，测到的是复现品而不是真实路径。所以这里搭的是完整链路：

    Resource + Win32Controller + Tasker + AgentClient + agent 子进程

跑的是真实 pipeline 节点 `CloudGameLaunchMain` 与真实注册的 CustomAction
`CloudGameLaunch`，与 MXU 里的执行路径一致。

## 同时完成标定采集

任务跑的过程中按帧差抓图并跑 OCR，写入 `debug/cloudgame/live/<时间戳>/`。
排队界面此前一直没采到（开发机曾无输入桌面权限），这次顺带补上。

## 会消耗云游戏时长

进入游戏后开始计费。

用法::

    python tools/live_cloudgame_enter.py                 # 默认最多跑 10 分钟
    python tools/live_cloudgame_enter.py --timeout 900
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def make_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def input_desktop_available() -> tuple[bool, str]:
    """当前进程能否真的操作输入桌面。

    为什么必须先查：``CloudGame-Front`` 用 ``mouse: Seize``，写的是**真实**
    鼠标输入，需要一个可交互的桌面。远程会话断开或未连接时：

    - ``SetCursorPos`` 返回 0、光标冻在 ``(0, 0)``
    - ``GetForegroundWindow()`` 返回 0
    - 但 ``post_touch_*`` 仍然**返回成功**——API 调用成功不等于输入送达

    不先查就会跑满整个超时才发现点击全落空，而日志里满屏「已点击」。
    """
    user32 = ctypes.windll.user32
    ok = bool(user32.SetCursorPos(700, 400))
    time.sleep(0.2)
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    moved = (point.x, point.y) != (0, 0)
    foreground = user32.GetForegroundWindow()
    if ok and moved:
        return True, f"可用（光标可移动，前台窗口={foreground}）"
    return False, (
        f"不可用：SetCursorPos={ok} 光标=({point.x},{point.y}) "
        f"前台窗口={foreground}。"
        "远程会话需保持连接且桌面可交互，否则 Seize 点击不会送达"
    )


def find_cloud_window():
    """找云异环窗口；返回 (hwnd, class_name) 或 None。"""
    from maa.toolkit import Toolkit

    for window in Toolkit.find_desktop_windows():
        if (
            window.hwnd
            and window.class_name.startswith("Qt")
            and "异环" in (window.window_name or "")
        ):
            return window.hwnd, window.class_name
    return None


def launch_client_via_repo() -> int | None:
    """用仓库自己的 launcher 拉起客户端，返回 hwnd。

    在**子进程**里做：导入 ``custom.action.*`` 会把当前进程带进 AgentServer
    上下文，之后 ``Toolkit.find_desktop_windows()`` 会直接抛异常。
    """
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{REPO / 'agent'}');"
        "from custom.action.CloudGame.launcher import launch_cloud_game;"
        "r = launch_cloud_game();"
        "print('HWND=%s' % (r.hwnd or ''));"
        "print('MSG=%s' % r.message)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    hwnd = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("HWND="):
            value = line[5:].strip()
            hwnd = int(value) if value.isdigit() else None
        elif line.startswith("MSG="):
            log(f"launcher: {line[4:]}")
    if hwnd is None and proc.stderr:
        log(f"launcher stderr: {proc.stderr.strip()[:300]}")
    return hwnd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=600.0, help="任务上限秒数")
    parser.add_argument(
        "--shot-interval", type=float, default=2.0, help="采集轮询间隔"
    )
    parser.add_argument(
        "--shot-threshold", type=float, default=3.0, help="采集帧差阈值"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="输入桌面不可用时仍然继续（只采集画面，点击预期无效）",
    )
    parser.add_argument(
        "--wait-input",
        type=float,
        default=0.0,
        help=(
            "输入桌面不可用时先等它变可用，最多等这么多秒。"
            "远程会话的可交互性会随客户端连接状态闪断，"
            "与其反复手动重试，不如让工具等到能干活再开始。"
        ),
    )
    parser.add_argument(
        "--no-collect",
        action="store_true",
        help=(
            "关掉采集线程。采集线程在主进程直接操作 controller，而 agent 子进程"
            "通过代理操作同一个 native 对象，两边并发可能导致 access violation。"
            "排查点击失败时先用这个开关做对照。"
        ),
    )
    args = parser.parse_args()

    make_dpi_aware()

    available, reason = input_desktop_available()
    log(f"输入桌面: {reason}")

    if not available and args.wait_input > 0:
        log(f"等待输入桌面变为可用，最多 {args.wait_input:.0f}s…")
        deadline = time.monotonic() + args.wait_input
        last_report = 0.0
        while time.monotonic() < deadline:
            time.sleep(3.0)
            available, reason = input_desktop_available()
            if available:
                log(f"输入桌面已可用：{reason}")
                break
            waited = time.monotonic() - (deadline - args.wait_input)
            # 每 30s 报一次进度，不刷屏
            if waited - last_report >= 30.0:
                last_report = waited
                log(f"  仍不可用（已等 {waited:.0f}s）")
        if not available:
            log(f"等满 {args.wait_input:.0f}s 仍不可用")

    if not available:
        log("")
        log("点击不会生效，先解决这个再跑。可选做法：")
        log("  1. 保持远程桌面客户端处于连接且窗口未最小化的状态")
        log("  2. 或改用不依赖输入桌面的控制器（云客户端为 Qt，实测消息型输入无效）")
        log("")
        log("仍要继续可加 --force（只为采集画面，点击预期无效）")
        if not args.force:
            return 2

    import cv2
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import Win32Controller
    from maa.define import MaaWin32InputMethodEnum as Input
    from maa.define import MaaWin32ScreencapMethodEnum as Cap
    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    Toolkit.init_option("./debug")

    # —— 1. 客户端 ————————————————————————————————
    found = find_cloud_window()
    if found is None:
        log("云异环未运行，用仓库 launcher 拉起")
        hwnd = launch_client_via_repo()
        if hwnd is None:
            log("启动失败")
            return 1
        # launcher 返回的 hwnd 可信，但再确认一次窗口类名
        time.sleep(2.0)
        found = find_cloud_window() or (hwnd, "?")
    hwnd, cls = found
    log(f"窗口 hwnd={hwnd} class={cls}")

    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
        time.sleep(1.5)
        log("窗口已从最小化恢复")
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    log(f"前台窗口={user32.GetForegroundWindow()} 目标={hwnd}")

    # —— 2. 与 interface.json 的 CloudGame-Front 一致 ————————
    controller = Win32Controller(
        hwnd,
        screencap_method=Cap.FramePool,
        mouse_method=Input.Seize,
        keyboard_method=Input.Seize,
    )
    if not controller.post_connection().wait().succeeded:
        log("控制器连接失败")
        return 1
    log("控制器已连接（FramePool + Seize）")

    resource = Resource()
    if not resource.post_bundle(
        str(REPO / "assets" / "resource" / "base")
    ).wait().succeeded:
        log("资源加载失败")
        return 1
    log(f"资源已加载 loaded={resource.loaded}")

    # —— 3. AgentClient + agent 子进程 ————————————————
    client = AgentClient()
    client.bind(resource)
    socket_id = client.identifier
    if not socket_id:
        log("拿不到 AgentClient identifier")
        return 1
    log(f"AgentClient socket_id={socket_id}")

    agent_proc = subprocess.Popen(
        [sys.executable, str(REPO / "agent" / "main.py"), socket_id],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def pump_agent_output():
        for line in agent_proc.stdout or []:
            text = line.rstrip()
            if text:
                print(f"    [agent] {text[:200]}", flush=True)

    threading.Thread(target=pump_agent_output, daemon=True).start()

    log("等待 agent 连接…")
    if not client.connect():
        log("agent 连接失败")
        agent_proc.terminate()
        return 1
    log(f"agent 已连接，注册的动作数={len(client.custom_action_list or [])}")

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        log("Tasker 绑定失败")
        agent_proc.terminate()
        return 1
    log(f"Tasker inited={tasker.inited}")

    # —— 4. 采集线程：任务跑的同时记录每一次画面变化 ————————
    out_dir = REPO / "debug" / "cloudgame" / "live" / datetime.now().strftime(
        "%H%M%S"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / "ocr.jsonl"
    log(f"采集目录 {out_dir.relative_to(REPO)}")

    stop_flag = threading.Event()

    def ocr_texts(image):
        """整屏 OCR。

        **只在任务结束后调用。** 任务运行期间 tasker 正忙，主线程再调
        ``post_recognition`` 会一直阻塞——实测采集线程就是这样卡死的：
        第一帧图写出来了，``ocr.jsonl`` 却是空的，之后再也没有新帧。
        """
        job = tasker.post_recognition(
            JRecognitionType.OCR,
            # only_rec 必须 False：True 会把整屏当一行读，得到垃圾结果
            JOCR(roi=[0, 0, image.shape[1], image.shape[0]], only_rec=False),
            image,
        )
        detail = job.wait().get()
        items = []
        for node in getattr(detail, "nodes", None) or []:
            reco = getattr(node, "recognition", None)
            for res in getattr(reco, "all_results", None) or []:
                text = getattr(res, "text", "")
                if not text.strip():
                    continue
                items.append(
                    {
                        "text": text,
                        "box": list(getattr(res, "box", []) or []),
                        "score": round(float(getattr(res, "score", 0.0)), 3),
                    }
                )
        return items

    # 采集期间**只存帧**，不跑 OCR（会阻塞在忙碌的 tasker 上）。
    # 帧的元信息先攒着，任务结束后统一补 OCR。
    collected: list[dict] = []

    def collector():
        started = time.monotonic()
        index = 0
        last = None
        while not stop_flag.is_set():
            time.sleep(args.shot_interval)
            try:
                if not controller.post_screencap().wait().succeeded:
                    continue
                frame = controller.cached_image
                if frame is None:
                    continue
                if last is not None and frame.shape == last.shape:
                    diff = float(
                        np.mean(
                            cv2.absdiff(
                                cv2.resize(last, (160, 90)),
                                cv2.resize(frame, (160, 90)),
                            )
                        )
                    )
                    if diff < args.shot_threshold:
                        continue
                else:
                    diff = 255.0
                path = out_dir / f"{index:03d}.png"
                cv2.imwrite(str(path), frame)
                collected.append(
                    {
                        "index": index,
                        "file": path.name,
                        "elapsed": round(time.monotonic() - started, 1),
                        "diff": round(diff, 2),
                        "size": [frame.shape[1], frame.shape[0]],
                    }
                )
                log(
                    f"  采集#{index:03d} diff={diff:.1f} "
                    f"mean={frame.mean():.1f}（OCR 待任务结束后补）"
                )
                index += 1
                last = frame
            except Exception as exc:
                log(f"  采集异常: {exc}")

    if args.no_collect:
        log("已按 --no-collect 关闭采集线程（对照实验：排除并发干扰）")
    else:
        threading.Thread(target=collector, daemon=True).start()

    # —— 5. 跑真实任务 ————————————————————————————
    log("=" * 60)
    log("post_task CloudGameLaunchMain")
    log("=" * 60)
    job = tasker.post_task(
        "CloudGameLaunchMain",
        {
            "CloudGameLaunchMain": {
                "custom_action_param": {
                    "launcher_path": "",
                    "window_timeout": 120,
                    "ready_timeout": int(args.timeout),
                    "confirm_hits": 2,
                    "wait_in_game": True,
                    "auto_enter": True,
                    "max_queue_time": int(args.timeout),
                    "stop_when_no_playtime": True,
                }
            }
        },
    )

    detail = job.wait().get()
    status = job.status
    stop_flag.set()
    time.sleep(args.shot_interval + 0.5)

    log("=" * 60)
    succeeded = bool(getattr(status, "succeeded", False))
    log(f"任务结束 succeeded={succeeded}")
    for node in getattr(detail, "nodes", None) or []:
        log(f"  节点 {getattr(node, 'name', '?')}")

    # 任务已结束，tasker 空闲，现在才能安全地跑 OCR
    if collected:
        log(f"补跑 OCR：{len(collected)} 帧")
        for item in collected:
            frame = cv2.imread(str(out_dir / item["file"]))
            if frame is None:
                continue
            try:
                item["texts"] = ocr_texts(frame)
            except Exception as exc:
                item["texts"] = []
                log(f"  #{item['index']:03d} OCR 失败: {exc}")
            preview = " | ".join(t["text"] for t in item.get("texts", [])[:8])
            log(f"  #{item['index']:03d} t={item['elapsed']:.0f}s {preview[:110]}")
        with jsonl.open("w", encoding="utf-8") as handle:
            for item in collected:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    log(f"采集产物 {out_dir.relative_to(REPO)}")

    try:
        client.disconnect()
    except Exception:
        pass
    agent_proc.terminate()
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
