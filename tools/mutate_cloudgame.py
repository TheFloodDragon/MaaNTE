"""变异测试：确认 verify_cloudgame.py 的断言真能抓到缺陷。

断言全过但抓不到任何东西，比没有断言更糟——它会给出虚假的安全感。
这里逐个把源码改坏，跑一次验证，要求它**必须失败**。

每个变异都对应一个真实会犯的错误，不是为了凑数：

1. 弹窗判断失效 -> 会去点被弹窗遮住的「开始游戏」，30 秒后启动被放弃
2. 主页优先于弹窗 -> 同上
3. 去掉点击冷却 -> 每轮都点，在弹窗上乱点
4. 把「读不到时长」当成 0 -> OCR 抖一帧就中止任务
5. 时长为 0 仍继续点 -> 点了也进不去，白占时长
6. 排队计时从入口起算 -> 等登录的时间被算进排队，提前误判超时
7. 不先判断是否已在游戏内 -> 在游戏里还去找启动器按钮
8. 识别异常当作命中 -> 加载页就放行
9. 时长解析用相等匹配 -> 实机「😄付费时长：」必然失配

用法：``python tools/mutate_cloudgame.py``
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLOUD = REPO / "agent" / "custom" / "action" / "CloudGame"

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )

# (说明, 相对 CloudGame 的文件名, 原文, 改成)
MUTATIONS = [
    (
        "弹窗判断永远为假（会去点被遮住的开始游戏）",
        "launcher_ui.py",
        "        if self.has(TEXT_CONFIRM_TITLE):\n            return True\n        return self.has(TEXT_CONFIRM_ENTER) and self.has(TEXT_CONFIRM_QUIT)",
        "        return False",
    ),
    (
        "主页判断不再排除弹窗（弹窗覆盖时误判成主页）",
        "launcher_ui.py",
        "        return self.has(TEXT_START_GAME) and not self.is_confirm_dialog",
        "        return self.has(TEXT_START_GAME)",
    ),
    (
        "去掉点击冷却（每轮都点，在弹窗上乱点）",
        "enter.py",
        "        if now - self._last.get(key, -1e9) < CLICK_COOLDOWN:\n            return False",
        "        if False:\n            return False",
    ),
    (
        "把读不到时长当成 0（OCR 抖一帧就中止任务）",
        "launcher_ui.py",
        "    return best[1] if best else None",
        "    return best[1] if best else 0",
    ),
    (
        "时长为 0 仍继续点（点了也进不去，白占时长）",
        "enter.py",
        "            if stop_when_no_playtime and free_minutes == 0 and paid_minutes == 0:",
        "            if False:",
    ),
    (
        "排队计时从入口起算（等登录的时间被算进排队）",
        "enter.py",
        "        if queue_started is None:\n            queue_started = clock()",
        "        if queue_started is None:\n            queue_started = started",
    ),
    (
        "不先判断是否已在游戏内（在游戏里还去找启动器按钮）",
        "enter.py",
        "        if matched:\n            if matched != hit_node:",
        "        if False:\n            if matched != hit_node:",
    ),
    (
        "识别异常当作命中（加载页就放行）",
        "enter.py",
        '            except Exception as exc:\n                # 识别异常不能当作「已进入游戏」，否则会在加载页就放行。\n                log(f"识别 {node} 异常: {exc}")',
        '            except Exception as exc:\n                matched = node\n                log(f"识别 {node} 异常: {exc}")',
    ),
    (
        "时长标签改用相等匹配（实机带 emoji 必然失配）",
        "launcher_ui.py",
        "        for hit in self.hits:\n            if needle in hit.text:\n                return hit",
        "        for hit in self.hits:\n            if needle == hit.text:\n                return hit",
    ),
    (
        "改用 post_click（偏离仓库 click_rect 约定）",
        "enter.py",
        '        self._step = "touch_move"\n        controller.post_touch_move(x, y).wait()',
        '        self._step = "post_click"\n        controller.post_click(x, y).wait()\n        return',
    ),
    (
        "点击省略 move 一步（依赖 hover 的控件收不到事件）",
        "enter.py",
        '        self._step = "touch_move"\n        controller.post_touch_move(x, y).wait()\n        self._sleep(0.05)\n',
        "",
    ),
    (
        "缓存 controller 跨调用复用（句柄失效 -> 实机 access violation）",
        "enter.py",
        "        controller = _controller_of(self._context)\n        if controller is None:\n            raise RuntimeError(\"拿不到 controller\")",
        "        if not hasattr(self, '_cached_ctrl'):\n            self._cached_ctrl = _controller_of(self._context)\n        controller = self._cached_ctrl\n        if controller is None:\n            raise RuntimeError(\"拿不到 controller\")",
    ),
]


def _run(script: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, str(REPO / "tools" / script)],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_verify() -> tuple[bool, str]:
    return _run("verify_cloudgame.py")


def run_replay() -> tuple[bool, str]:
    """跑 replay 校验（真实截图 + 真实 OCR）。

    与 verify 互补：verify 用手抄的 OCR 文本验判断逻辑，replay 用真实模型
    在真实像素上验同一套判断。两者都该能抓到 launcher_ui 的判断退化——
    只有 verify 能抓到时，说明手抄 fixture 与真实 OCR 已经脱节。
    """
    return _run("replay_cloudgame.py")


# 这些变异，**现有真实帧**足以让 replay 也抓到。
#
# 名单要按磁盘上实际有的帧来定，不能按「理论上该覆盖」来定。目前
# debug/cloudgame/ 下的 24 帧全是启动器主页，因此以下三条 replay 抓不到，
# 不是 fixture 与真实 OCR 脱节，而是**没有能触发那条路径的真实帧**：
#
#   - 弹窗判断永远为假        -> 没有任何弹窗帧，无从分辨
#   - 主页判断不再排除弹窗    -> 同上，主页帧本来就不含弹窗文本
#   - 把读不到时长当成 0      -> 所有真实帧的时长都读得出，走不到
#                               「标签在、值缺失」那条分支
#
# 采到弹窗帧后，把前两条移进来；采到时长值缺失的帧后，把第三条移进来。
REPLAY_SENSITIVE = {
    "时长标签改用相等匹配（实机带 emoji 必然失配）",
}


def main() -> int:
    print("=" * 72)
    print("变异测试：断言必须能抓到每一个植入的缺陷")
    print("=" * 72)

    ok, _ = run_verify()
    if not ok:
        print("[FAIL] 基线就没通过，先修好 verify_cloudgame.py 再来")
        return 1
    print("[ok] verify 基线通过")

    # replay 需要 OCR 模型与一个可见窗口，环境不满足时降级为只跑 verify，
    # 但必须说清楚，不能让人误以为真实像素也验过了。
    replay_ok, replay_out = run_replay()
    replay_enabled = replay_ok
    if replay_ok:
        print("[ok] replay 基线通过（真实截图 + 真实 OCR）\n")
    else:
        print("[warn] replay 基线未通过，本次跳过 replay 交叉校验")
        for line in replay_out.splitlines():
            if "FAIL" in line or "模型" in line:
                print(f"       {line.strip()[:110]}")
        print()

    caught = 0
    missed: list[str] = []
    replay_missed: list[str] = []

    for index, (desc, filename, old, new) in enumerate(MUTATIONS, 1):
        path = CLOUD / filename
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"[{index:>2}] 跳过：在 {filename} 里找不到锚点 -> {desc}")
            missed.append(f"{desc}（锚点失效，变异未生效）")
            continue

        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            passed, output = run_verify()
            replay_caught = None
            if replay_enabled and desc in REPLAY_SENSITIVE:
                replay_passed, _ = run_replay()
                replay_caught = not replay_passed
        finally:
            path.write_text(original, encoding="utf-8")

        if passed:
            print(f"[{index:>2}] 未抓到 <- {desc}")
            missed.append(desc)
        else:
            first = ""
            for line in output.splitlines():
                if line.strip().startswith("- "):
                    first = line.strip()[:88]
                    break
            caught += 1
            tail = ""
            if replay_caught is True:
                tail = "（replay 也抓到）"
            elif replay_caught is False:
                tail = "（replay 未抓到）"
                replay_missed.append(desc)
            print(f"[{index:>2}] 抓到   -> {desc} {tail}")
            if first:
                print(f"      {first}")

    print("-" * 72)
    print(f"抓到 {caught}/{len(MUTATIONS)}")
    if missed:
        print("未抓到的缺陷（说明断言有盲区）：")
        for item in missed:
            print(f"  - {item}")
        return 1
    if replay_missed:
        print("verify 抓到但 replay 没抓到的（手抄 fixture 与真实 OCR 已脱节）：")
        for item in replay_missed:
            print(f"  - {item}")
        return 1

    # 变异测试跑完必须恢复原状，否则会把改坏的代码留在工作区
    leftover, _ = run_verify()
    if not leftover:
        print("[FAIL] 变异后源码未正确恢复")
        return 1
    print("源码已恢复，基线仍通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
