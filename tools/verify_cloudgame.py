"""云异环启动任务的离线验证。

不依赖真实云客户端、不启动任何进程、不联网。用假 spawn、假时钟与假
context 覆盖四个层面：

1. 探测层：白名单防线、注册表/目录/用户输入三种来源、失败提示；
2. 启动层：显式配置不得静默回退、拒绝时不得启动进程、已在运行不重复拉起；
3. 就绪层：连续命中确认、识别异常不得放行、超时与停止；
4. 接线层：任务定义、Pipeline 节点、五语言 locale、CustomAction 注册。

用法：``python tools/verify_cloudgame.py``
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))

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
        _FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


# ---------------------------------------------------------------------------
# 假环境
# ---------------------------------------------------------------------------


class FakeClock:
    """可手动推进的时钟，避免测试真实等待。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds), 0.0)


class FakeController:
    def __init__(self, frames):
        self._frames = list(frames)
        self.screencap_calls = 0

    def post_screencap(self):
        self.screencap_calls += 1
        frame = self._frames.pop(0) if self._frames else "LAST"
        return _Wrap(frame)


class _Wrap:
    def __init__(self, value):
        self._value = value

    def wait(self):
        return self

    def get(self):
        return self._value


class _Hit:
    def __init__(self, ok):
        self.hit = ok


class FakeTasker:
    def __init__(self, controller):
        self.controller = controller
        self.stopping = False


class FakeContext:
    """按脚本回答识别请求。

    ``script`` 是「帧内容 -> 命中的节点名或 None」的映射；
    ``raise_nodes`` 中的节点一律抛异常，用于验证异常不得放行。
    """

    def __init__(self, frames, script, raise_nodes=()):
        self.tasker = FakeTasker(FakeController(frames))
        self._script = script
        self._raise = set(raise_nodes)
        self.reco_calls: list[tuple[str, object]] = []

    def run_recognition(self, node, image):
        self.reco_calls.append((node, image))
        if node in self._raise:
            raise RuntimeError(f"boom:{node}")
        return _Hit(self._script.get(image) == node)


# ---------------------------------------------------------------------------
# [1] 探测层
# ---------------------------------------------------------------------------


def group_locator():
    print("[1] 探测层：白名单防线与来源优先级")
    from agent.custom.action.CloudGame.locator import (
        TRUSTED_LAUNCHER_NAMES,
        describe_failure,
        discover_launchers,
        is_trusted_launcher,
        resolve_launcher,
    )

    # 白名单必须拒绝所有非云异环可执行文件。这是最关键的安全边界：
    # 若失守，本模块就变成了"任意进程启动器"。
    for bad in (
        r"C:\Windows\System32\cmd.exe",
        r"C:\Windows\System32\calc.exe",
        r"C:\Windows\notepad.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "definitely_not_here.exe",
        "",
        None,
    ):
        check(not is_trusted_launcher(bad), f"白名单拒绝 {bad!r}")

    # 目录遍历/相对路径也不能绕过白名单。
    for tricky in (
        r"E:\NevernessToEvernessCloudGame\..\Windows\System32\cmd.exe",
        r"./cmd.exe",
        r"NTECloudLauncher.exe.txt",
    ):
        check(not is_trusted_launcher(tricky), f"白名单拒绝可疑路径 {tricky!r}")

    # 白名单只认这两个名字。
    check_equal(
        set(TRUSTED_LAUNCHER_NAMES),
        {"ntecloudlauncher.exe", "ntecloudgame.exe"},
        "白名单内容",
    )

    # 卸载程序与启动器同目录，朴素的"扫目录取 exe"会误伤，白名单必须挡住。
    check(
        not is_trusted_launcher(r"E:\NevernessToEvernessCloudGame\uninst.exe"),
        "白名单拒绝同目录卸载程序",
    )

    # 探测结果（若本机装了云异环）必须全部通过白名单，且不重复。
    found = discover_launchers()
    for cand in found:
        check(is_trusted_launcher(cand.path), f"探测结果可信: {cand.path.name}")
        check(bool(cand.source), "探测结果带来源说明")
    paths = [str(c.path).lower() for c in found]
    check_equal(len(paths), len(set(paths)), "探测结果不重复")

    # 首选应当就是候选列表的第一项。
    preferred = resolve_launcher()
    if found:
        check_equal(preferred.path, found[0].path, "首选=候选首项")
    else:
        check_equal(preferred, None, "无候选时首选为 None")

    # 失败提示必须可操作：要含具体路径示例或白名单名字。
    msg_no_input = describe_failure(None)
    check("路径" in msg_no_input, "未配置时提示含路径指引")
    msg_bad = describe_failure(r"C:\Windows\System32\cmd.exe")
    check("cmd.exe" in msg_bad, "错配置提示回显用户输入")
    check(
        any(name in msg_bad for name in TRUSTED_LAUNCHER_NAMES),
        "错配置提示给出正确文件名",
    )

    print(f"     完成，累计断言 {_CHECKS} 项")


# ---------------------------------------------------------------------------
# [2] 启动层
# ---------------------------------------------------------------------------


def group_launcher():
    print("[2] 启动层：拒绝即失败，绝不静默替换或误启动")
    from agent.custom.action.CloudGame import launcher as L

    real = None
    from agent.custom.action.CloudGame.locator import resolve_launcher

    found = resolve_launcher()
    if found is not None:
        real = str(found.path)

    # --- resolve_executable: 显式配置无效必须报错，不得回退自动探测 ---
    # 若静默回退，用户填错路径却启动了别的客户端，且毫无提示。
    for bad in (
        r"C:\Windows\System32\cmd.exe",
        r"E:\NevernessToEvernessCloudGame\uninst.exe",
        r"Z:\nope\NTECloudLauncher.exe",
    ):
        path, source, reason = L.resolve_executable(bad)
        check_equal(path, None, f"显式无效配置被拒: {Path(bad).name}")
        check(bool(reason), "拒绝时给出原因")
        check(
            "自动" not in source,
            f"拒绝时不标记为自动探测: {source!r}",
        )

    # 空白串视为"未配置"，走自动探测。
    for blank in ("", "   ", None):
        path, source, reason = L.resolve_executable(blank)
        if real is not None:
            check(path is not None, f"空配置 {blank!r} 走自动探测")
        else:
            check_equal(path, None, "无安装时自动探测失败")

    if real is not None:
        path, source, _ = L.resolve_executable(real)
        check(path is not None, "正确配置被接受")
        check("用户指定" in source, f"来源标记为用户指定: {source!r}")

    # --- launch_cloud_game: 被拒时绝不能启动任何进程 ---
    spawned: list[str] = []
    clock = FakeClock()
    result = L.launch_cloud_game(
        user_path=r"C:\Windows\System32\cmd.exe",
        window_timeout=5.0,
        poll_interval=1.0,
        clock=clock,
        sleeper=clock.sleep,
        spawn=lambda p: spawned.append(str(p)),
        process_names=("__none__.exe",),
    )
    check_equal(len(spawned), 0, "配置被拒时未启动任何进程")
    check_equal(result.started, False, "被拒时 started=False")
    check_equal(result.hwnd, None, "被拒时无窗口")
    check(bool(result.message), "被拒时有说明")

    # --- 合法配置：启动一次，窗口未出现则超时 ---
    if real is not None:
        spawned.clear()
        clock = FakeClock()
        result = L.launch_cloud_game(
            user_path=real,
            window_timeout=3.0,
            poll_interval=1.0,
            clock=clock,
            sleeper=clock.sleep,
            spawn=lambda p: spawned.append(str(p)),
            process_names=("__none__.exe",),
        )
        check_equal(len(spawned), 1, "合法配置只启动一次")
        check_equal(result.hwnd, None, "窗口未出现")
        check("超时" in result.message, f"报超时: {result.message!r}")

    # --- 任务停止时立即退出等待 ---
    if real is not None:
        spawned.clear()
        clock = FakeClock()
        result = L.launch_cloud_game(
            user_path=real,
            window_timeout=60.0,
            poll_interval=1.0,
            should_stop=lambda: True,
            clock=clock,
            sleeper=clock.sleep,
            spawn=lambda p: spawned.append(str(p)),
            process_names=("__none__.exe",),
        )
        check("停止" in result.message, "停止时给出说明")
        check(clock.now < 5.0, "停止后不再等满超时")

    # --- 已有窗口：不重复拉起，避免多客户端争抢串流 ---
    spawned.clear()
    original = L._find_cloud_window
    try:
        L._find_cloud_window = lambda names: {
            "hwnd": 4242,
            "client_size": (1280, 720),
        }
        clock = FakeClock()
        result = L.launch_cloud_game(
            user_path=real,
            clock=clock,
            sleeper=clock.sleep,
            spawn=lambda p: spawned.append(str(p)),
        )
        check_equal(len(spawned), 0, "窗口已存在时不启动")
        check_equal(result.already_running, True, "标记为已在运行")
        check_equal(result.hwnd, 4242, "返回已有窗口句柄")
    finally:
        L._find_cloud_window = original

    # --- 窗口太小不算就绪（云客户端启动初期会有小窗/splash）---
    spawned.clear()
    original = L.find_windows_by_process
    try:
        L.find_windows_by_process = lambda name, hwnd_class=None, require_title=False: [
            {"hwnd": 1, "client_size": (80, 60)}
        ]
        got = L._find_cloud_window(("x.exe",))
        check_equal(got, None, "过小窗口不算就绪")
    finally:
        L.find_windows_by_process = original

    print(f"     完成，累计断言 {_CHECKS} 项")


# ---------------------------------------------------------------------------
# [3] 就绪层
# ---------------------------------------------------------------------------


def group_ready():
    print("[3] 就绪层：连续命中确认，识别异常不得放行")
    from agent.custom.action.CloudGame.ready import (
        IN_GAME_NODES,
        wait_until_in_game,
    )

    check_equal(IN_GAME_NODES, ("InWorld", "InMiniWorld"), "只依赖已验证的公共节点")

    # 需要连续命中 2 次才确认，避免加载途中单帧误命中。
    frames = ["load", "world", "world", "world"]
    ctx = FakeContext(frames, {"world": "InWorld"})
    clock = FakeClock()
    res = wait_until_in_game(
        ctx,
        ready_timeout=60.0,
        poll_interval=1.0,
        confirm_hits=2,
        clock=clock,
        sleeper=clock.sleep,
    )
    check_equal(res.ready, True, "连续命中后确认进入游戏")
    check_equal(res.node, "InWorld", "报告命中的节点")

    # 单帧命中不足以确认。
    ctx = FakeContext(["world", "load", "load", "load"], {"world": "InWorld"})
    clock = FakeClock()
    res = wait_until_in_game(
        ctx,
        ready_timeout=4.0,
        poll_interval=1.0,
        confirm_hits=2,
        clock=clock,
        sleeper=clock.sleep,
    )
    check_equal(res.ready, False, "单帧命中不确认")

    # 命中节点中途切换（InWorld -> InMiniWorld）应重新计数。
    ctx = FakeContext(
        ["w", "m", "m"],
        {"w": "InWorld", "m": "InMiniWorld"},
    )
    clock = FakeClock()
    res = wait_until_in_game(
        ctx,
        ready_timeout=60.0,
        poll_interval=1.0,
        confirm_hits=2,
        clock=clock,
        sleeper=clock.sleep,
    )
    check_equal(res.ready, True, "小世界同样算进入游戏")
    check_equal(res.node, "InMiniWorld", "切换后按新节点计数")

    # 识别抛异常时绝不能当作已进入游戏 —— 否则会在登录页就放行去跑任务。
    ctx = FakeContext(
        ["x"] * 5,
        {"x": "InWorld"},
        raise_nodes=("InWorld", "InMiniWorld"),
    )
    clock = FakeClock()
    res = wait_until_in_game(
        ctx,
        ready_timeout=3.0,
        poll_interval=1.0,
        clock=clock,
        sleeper=clock.sleep,
    )
    check_equal(res.ready, False, "识别异常不得放行")

    # 截图失败同样不能放行。
    class NoCap(FakeContext):
        def __init__(self):
            super().__init__([], {})
            self.tasker.controller = None

    clock = FakeClock()
    res = wait_until_in_game(
        NoCap(),
        ready_timeout=3.0,
        poll_interval=1.0,
        clock=clock,
        sleeper=clock.sleep,
    )
    check_equal(res.ready, False, "截图失败不得放行")

    # 超时消息要能指向可能原因，而不是只说"超时"。
    ctx = FakeContext(["load"] * 10, {})
    clock = FakeClock()
    res = wait_until_in_game(
        ctx,
        ready_timeout=5.0,
        poll_interval=1.0,
        clock=clock,
        sleeper=clock.sleep,
    )
    check_equal(res.ready, False, "一直加载则超时")
    check("排队" in res.message or "登录" in res.message, "超时说明可能原因")

    # 停止请求应立即返回。
    ctx = FakeContext(["load"] * 10, {})
    clock = FakeClock()
    res = wait_until_in_game(
        ctx,
        ready_timeout=600.0,
        poll_interval=1.0,
        should_stop=lambda: True,
        clock=clock,
        sleeper=clock.sleep,
    )
    check("停止" in res.message, "停止时给出说明")
    check(clock.now < 5.0, "停止后不再等满超时")

    print(f"     完成，累计断言 {_CHECKS} 项")


# ---------------------------------------------------------------------------
# [4] 接线层
# ---------------------------------------------------------------------------

LOCALES = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")


def _load_jsonc(path: Path):
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return json.loads(text)


def group_wiring():
    print("[4] 接线层：任务、Pipeline、五语言、注册")

    task_path = REPO / "assets" / "resource" / "tasks" / "CloudGameLaunch.json"
    check(task_path.exists(), "任务定义存在")
    task_data = _load_jsonc(task_path)
    task = task_data["task"][0]

    pipe_path = (
        REPO / "assets" / "resource" / "base" / "pipeline" / "CloudGame"
        / "CloudGameLaunch.json"
    )
    check(pipe_path.exists(), "Pipeline 文件存在")
    pipe = _load_jsonc(pipe_path)

    # entry 必须指向真实存在的节点。
    check(task["entry"] in pipe, f"entry 节点存在: {task['entry']}")

    # 云异环任务只应在云控制器下可用 —— 本地控制器下启动云客户端没有意义。
    check_equal(task["controller"], ["CloudGame-Front"], "仅限云异环控制器")

    # option 声明与定义必须一致。
    declared = set(task.get("option", []))
    defined = set(task_data.get("option", {}))
    check_equal(declared - defined, set(), "无未定义的 option")

    # group 必须在 interface.json 中存在。
    iface = _load_jsonc(REPO / "assets" / "interface.json")
    groups = {g["name"] for g in iface.get("group", [])}
    for g in task.get("group", []):
        check(g in groups, f"group 存在: {g}")

    # 必须已注册到 interface.json 的 import。
    check(
        "resource/tasks/CloudGameLaunch.json" in iface.get("import", []),
        "已在 interface.json 注册",
    )

    # 收集所有 $key 并校验五语言齐全。
    keys: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node.startswith("$"):
            keys.add(node[1:])

    walk(task_data)
    check(len(keys) > 0, "任务定义使用了 i18n key")

    for lang in LOCALES:
        data = json.loads(
            (REPO / "assets" / "resource" / "locales" / "interface" / f"{lang}.json")
            .read_text(encoding="utf-8")
        )
        for key in sorted(keys):
            check(key in data, f"{lang} 含 key {key}")
            if key in data:
                check(bool(str(data[key]).strip()), f"{lang}/{key} 非空")

    # locale 必须是 LF（曾因 CRLF 造成整文件 diff）。
    for lang in LOCALES:
        raw = (
            REPO / "assets" / "resource" / "locales" / "interface" / f"{lang}.json"
        ).read_bytes()
        check_equal(raw.count(b"\r\n"), 0, f"{lang}.json 无 CRLF")

    # Pipeline 入口必须是纯 Custom（不含任何凭空编写的识别）。
    entry = pipe[task["entry"]]
    check_equal(entry.get("action"), "Custom", "入口为 Custom 动作")
    check_equal(entry.get("custom_action"), "CloudGameLaunch", "custom_action 名一致")
    check(
        "recognition" not in entry,
        "入口不含识别（无界面信息时不得凭空编写）",
    )

    # --- CloudDaily 预设：定时每日流程的载体 ---
    preset_path = (
        REPO / "assets" / "resource" / "tasks" / "preset" / "CloudDaily.json"
    )
    check(preset_path.exists(), "CloudDaily 预设存在")
    preset_data = _load_jsonc(preset_path)
    entry_preset = preset_data["preset"][0]

    # 收集全仓库的任务与 option 定义，用于交叉核对预设引用。
    all_tasks: dict = {}
    all_opts: dict = {}
    for path in sorted((REPO / "assets" / "resource" / "tasks").glob("*.json")):
        try:
            data = _load_jsonc(path)
        except (OSError, ValueError):
            continue
        for item in data.get("task", []):
            all_tasks[item["name"]] = item
        for name, body in (data.get("option") or {}).items():
            all_opts[name] = body

    steps = entry_preset.get("task", [])
    check(len(steps) >= 2, "预设包含启动 + 每日任务")
    # 启动必须在最前面：否则每日任务会在客户端还没起来时就开跑。
    check_equal(
        steps[0]["name"], "CloudGameLaunch", "CloudGameLaunch 位于首位"
    )

    for step in steps:
        name = step["name"]
        check(name in all_tasks, f"预设任务存在: {name}")
        if name not in all_tasks:
            continue
        # 预设里的每个任务都必须支持云异环控制器，否则整条链跑不起来。
        controllers = all_tasks[name].get("controller", [])
        check(
            "CloudGame-Front" in controllers,
            f"{name} 支持 CloudGame-Front: {controllers}",
        )
        declared = set(all_tasks[name].get("option", []))
        for opt_name, opt_value in (step.get("option") or {}).items():
            check(opt_name in all_opts, f"{name}: option 已定义 {opt_name}")
            check(
                opt_name in declared,
                f"{name}: option 被该任务声明 {opt_name}",
            )
            body = all_opts.get(opt_name, {})
            kind = body.get("type")
            # 选项值写错不会报错但会静默失效，必须静态核对。
            if kind in {"switch", "select"} and isinstance(opt_value, str):
                cases = [c.get("name") for c in body.get("cases", [])]
                check(
                    opt_value in cases,
                    f"{name}/{opt_name}: case {opt_value} 有效",
                )
            elif kind == "input" and isinstance(opt_value, dict):
                names = [i.get("name") for i in body.get("inputs", [])]
                for field in opt_value:
                    check(
                        field in names,
                        f"{name}/{opt_name}: input 字段 {field} 有效",
                    )

    # 预设的 i18n key 五语言齐全。
    preset_keys: set[str] = set()
    for value in (entry_preset.get("label"), entry_preset.get("description")):
        if isinstance(value, str) and value.startswith("$"):
            preset_keys.add(value[1:])
    check(len(preset_keys) >= 2, "预设使用了 label/description key")
    for lang in LOCALES:
        data = json.loads(
            (REPO / "assets" / "resource" / "locales" / "interface" / f"{lang}.json")
            .read_text(encoding="utf-8")
        )
        for key in sorted(preset_keys):
            check(key in data, f"{lang} 含预设 key {key}")

    check(
        "resource/tasks/preset/CloudDaily.json" in iface.get("import", []),
        "预设已在 interface.json 注册",
    )

    # CustomAction 必须已注册。
    import custom.action  # noqa: F401
    from maa.agent.agent_server import AgentServer

    check(
        "CloudGameLaunch" in AgentServer._custom_action_holder,
        "CloudGameLaunch 已注册",
    )
    check(
        "CloudGameLaunch" in custom.action.__all__,
        "CloudGameLaunch 在 __all__ 中",
    )
    # 既有动作不得被破坏。
    for name in ("AutoCombat", "PinkPawHeistScheme3Action", "auto_fish_pro"):
        check(
            name in AgentServer._custom_action_holder,
            f"既有动作仍在: {name}",
        )

    print(f"     完成，累计断言 {_CHECKS} 项")


def main():
    print("=" * 68)
    print("云异环启动任务离线验证")
    print("=" * 68)
    group_locator()
    group_launcher()
    group_ready()
    group_wiring()
    print("-" * 68)
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} / {_CHECKS} 项：")
        for item in _FAILURES[:40]:
            print(f"  - {item}")
        if len(_FAILURES) > 40:
            print(f"  ...(另有 {len(_FAILURES) - 40} 项)")
        return 1
    print(f"全部通过：{_CHECKS} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
