"""用**真实截图 + 真实 OCR** 校验启动器界面判断。

## 这个校验补的是什么盲区

``verify_cloudgame.py`` 的第 5、6 组用的是**手抄**的 OCR 文本：我照着实机
输出把 ``TextHit`` 一条条打进代码。那能验证判断逻辑，但验不到两件事：

1. OCR 模型的真实输出是否还和我抄的一致（换模型、换缩放都可能漂移）；
2. 我当初有没有抄错。

只要这两条出问题，手抄断言全绿而实机全崩——正是这次实机暴露 controller
句柄 bug 之前的处境。

所以这里改成：读 ``debug/cloudgame/`` 下的真实 PNG，跑**真实 OCR 模型**，
把结果喂给**真实的** ``launcher_ui``，再断言它的判断。

## 前提

OCR 模型不在仓库里，需要先：

    git submodule update --init assets/MaaCommonAssets
    python -c "import sys; sys.path.insert(0,'tools/ci'); \
from configure import configure_all_models; configure_all_models()"

缺模型时 OCR 静默返回 0 条，本脚本会报「模型缺失」而不是假装通过。

## 期望

每张图按文件名归类到预期阶段（见 ``EXPECTATIONS``）。新采到的截图放进
``debug/cloudgame/`` 后，在这里补一条期望即可纳入回归。

用法：``python tools/replay_cloudgame.py``
"""

from __future__ import annotations

import glob
import importlib.util
import io
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLOUD_DIR = REPO / "agent" / "custom" / "action" / "CloudGame"


def _load_cloudgame_modules():
    """按文件路径加载 ``launcher_ui`` 与 ``ocr_bridge``，绕开包初始化。

    不能直接 ``import custom.action.CloudGame.launcher_ui``：那会执行
    ``custom/action/__init__.py``，把进程带进 AgentServer 上下文，之后
    ``Resource()`` 直接抛 ``Failed to create resource.``。

    这里造一个最小的合成包，让 ``ocr_bridge`` 的相对导入
    ``from .launcher_ui import ...`` 能解析。测的仍是仓库里的真实实现，
    没有复制任何逻辑。
    """
    pkg_name = "_cg_replay"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(CLOUD_DIR)]
    sys.modules[pkg_name] = pkg

    loaded = {}
    for mod_name in ("launcher_ui", "ocr_bridge"):
        full = f"{pkg_name}.{mod_name}"
        spec = importlib.util.spec_from_file_location(
            full, CLOUD_DIR / f"{mod_name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
        loaded[mod_name] = module
    return loaded["launcher_ui"], loaded["ocr_bridge"]


launcher_ui, ocr_bridge = _load_cloudgame_modules()
LauncherScreen = launcher_ui.LauncherScreen
TextHit = launcher_ui.TextHit
TEXT_START_GAME = launcher_ui.TEXT_START_GAME
read_playtime = launcher_ui.read_playtime
MIN_SCORE = ocr_bridge.MIN_SCORE
_normalize_box = ocr_bridge._normalize_box

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

_CHECKS = 0
_FAILURES: list[str] = []


def check(cond, label):
    global _CHECKS
    _CHECKS += 1
    if not cond:
        _FAILURES.append(label)


def check_equal(actual, expected, label):
    global _CHECKS
    _CHECKS += 1
    if actual != expected:
        _FAILURES.append(f"{label}: 期望 {expected!r}，实际 {actual!r}")


# 每条期望：(阶段, 是否已登录, 免费分钟, 付费分钟)
# 阶段取 "home" / "dialog" / "other"；分钟为 None 表示「不校验具体值」。
#
# 这些值来自实机 OCR，不是编的：023714_restored.png 读到「11小时23分钟」，
# 后续几张读到「11小时38分钟」（期间免费时长自然增长）。
EXPECTATIONS = {
    "023714_restored.png": ("home", True, 11 * 60 + 23, 0),
    "now_FramePool.png": ("home", True, None, 0),
    "now_GDI.png": ("home", True, None, 0),
    "now_PrintWindow.png": ("home", True, None, 0),
    "click_Seize.png": ("home", True, None, 0),
    "click_PostMessageWithWindowPos.png": ("home", True, None, 0),
    "launcher01_t00.4.png": ("home", True, None, 0),
    "000_start.png": ("home", True, None, 0),
    "001_chg.png": ("home", True, None, 0),
    "000.png": ("home", True, None, 0),
}

# 全白的坏帧：窗口最小化时截到的（mean=255/std=0，尺寸也异常）。
# 它必须**既不是主页也不是弹窗**，否则最小化时会去乱点。
BLANK_FRAMES = {"023531_current.png"}


def build_ocr():
    """返回 ``ocr(image) -> list[TextHit]``，用真实 OCR 模型。"""
    from maa.controller import Win32Controller
    from maa.define import MaaWin32InputMethodEnum as Input
    from maa.define import MaaWin32ScreencapMethodEnum as Cap
    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.toolkit import Toolkit

    Toolkit.init_option("./debug")

    model_dir = REPO / "assets" / "resource" / "base" / "model" / "ocr"
    if not (model_dir / "rec.onnx").exists():
        print(f"[FAIL] OCR 模型缺失: {model_dir}")
        print("       先跑 tools/ci/configure.py 的 configure_all_models()")
        return None

    resource = Resource()
    if not resource.post_bundle(
        str(REPO / "assets" / "resource" / "base")
    ).wait().succeeded:
        print("[FAIL] 资源加载失败")
        return None

    # Tasker.bind 要求同时给控制器，识别本身不用它。借任意可见窗口满足形参。
    target = next(
        (w for w in Toolkit.find_desktop_windows() if w.hwnd and w.window_name),
        None,
    )
    if target is None:
        print("[FAIL] 找不到任何窗口用于绑定控制器")
        return None
    controller = Win32Controller(
        target.hwnd,
        screencap_method=Cap.GDI,
        mouse_method=Input.Seize,
        keyboard_method=Input.Seize,
    )
    if not controller.post_connection().wait().succeeded:
        print("[FAIL] 控制器连接失败")
        return None

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FAIL] Tasker 绑定失败")
        return None

    def ocr(image):
        job = tasker.post_recognition(
            JRecognitionType.OCR,
            # only_rec 必须 False：True 会把整屏当一行读，得到垃圾结果
            JOCR(roi=[0, 0, image.shape[1], image.shape[0]], only_rec=False),
            image,
        )
        detail = job.wait().get()
        hits = []
        for node in getattr(detail, "nodes", None) or []:
            reco = getattr(node, "recognition", None)
            for res in getattr(reco, "all_results", None) or []:
                text = getattr(res, "text", "") or ""
                if not text.strip():
                    continue
                score = float(getattr(res, "score", 0.0) or 0.0)
                if score < MIN_SCORE:
                    continue
                hits.append(
                    TextHit(
                        text=text,
                        box=_normalize_box(getattr(res, "box", None)),
                        score=score,
                    )
                )
        return hits

    return ocr


def main() -> int:
    import cv2

    print("=" * 72)
    print("replay 校验：真实截图 + 真实 OCR + 真实 launcher_ui 判断")
    print("=" * 72)

    ocr = build_ocr()
    if ocr is None:
        return 1

    paths = sorted(glob.glob("debug/cloudgame/**/*.png", recursive=True))
    if not paths:
        print("[FAIL] debug/cloudgame/ 下没有任何截图")
        return 1

    matched = 0
    for path in paths:
        name = os.path.basename(path)
        expectation = EXPECTATIONS.get(name)
        is_blank = name in BLANK_FRAMES
        if expectation is None and not is_blank:
            print(f"  [skip] {path}（没有登记期望）")
            continue

        image = cv2.imread(path)
        if image is None:
            _FAILURES.append(f"{path}: 读不出图")
            continue

        screen = LauncherScreen(hits=ocr(image))
        matched += 1

        if is_blank:
            # 全白坏帧：既不是主页也不是弹窗，否则最小化时会乱点
            check(
                not screen.is_home,
                f"{name}: 全白帧不得判成主页（会在最小化时乱点）",
            )
            check(
                not screen.is_confirm_dialog,
                f"{name}: 全白帧不得判成确认弹窗",
            )
            print(f"  [ok] {name} 全白帧：非主页非弹窗，共 {len(screen.hits)} 条文本")
            continue

        stage, logged_in, free_expected, paid_expected = expectation

        if stage == "home":
            check(screen.is_home, f"{name}: 应判定为主页")
            check(
                not screen.is_confirm_dialog,
                f"{name}: 不应误判为确认弹窗",
            )
            start = screen.find(TEXT_START_GAME)
            check(start is not None, f"{name}: 应能找到「开始游戏」")
            if start is not None:
                x, y = start.center
                check(
                    0 < x < image.shape[1] and 0 < y < image.shape[0],
                    f"{name}: 「开始游戏」中心 ({x},{y}) 应落在画面内",
                )
        elif stage == "dialog":
            check(screen.is_confirm_dialog, f"{name}: 应判定为确认弹窗")
            check(not screen.is_home, f"{name}: 不应判成主页")

        check_equal(screen.is_logged_in, logged_in, f"{name}: 登录判定")

        free, paid = read_playtime(screen)
        if free_expected is not None:
            check_equal(free, free_expected, f"{name}: 免费时长分钟")
        else:
            # 不校验具体值时至少要读出来，不能是 None
            check(free is not None, f"{name}: 免费时长应能读出（当前 None）")
        if paid_expected is not None:
            check_equal(paid, paid_expected, f"{name}: 付费时长分钟")

        print(
            f"  [ok] {name} 阶段={stage} 文本={len(screen.hits)} "
            f"免费={free} 付费={paid}"
        )

    print("-" * 72)
    if matched == 0:
        print("[FAIL] 没有任何截图参与校验（期望表可能全都对不上文件名）")
        return 1
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} / {_CHECKS} 项：")
        for item in _FAILURES:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{matched} 张真实截图，{_CHECKS} 项断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
