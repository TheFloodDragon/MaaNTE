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

    # --- 窗口类名匹配必须真的能命中实机类名 ---
    #
    # 这一段直接调用真实的 _match_class_name，不做替换。上面几条都把
    # find_windows_by_process 整个换掉了，因此类名匹配逻辑从未被覆盖——
    # 实机 bug 正是从这个缺口漏出去的：
    #
    #   CLOUD_WINDOW_CLASS 曾写成字符串 ("Qt",)，而 _match_class_name 对
    #   字符串做的是**精确相等**比较，只有非字符串 pattern 才走 re.search。
    #   实机类名是 Qt51517QWindowOwnDC，于是窗口明明已经出现却永远匹配不到，
    #   CloudGameLaunch 必然卡到「等待窗口超时（120s）」。
    from utils.win32_process import _match_class_name

    # 实机采集到的真实类名（云·异环，1600x900 窗口）
    for real in ("Qt51517QWindowOwnDC", "Qt5152QWindowIcon", "Qt6111QWindowOwnDC"):
        check(
            _match_class_name(real, L.CLOUD_WINDOW_CLASS),
            f"能匹配实机窗口类名: {real}",
        )
    for other in ("UnrealWindow", "CabinetWClass", "Progman", "NotQtAtAll"):
        check(
            not _match_class_name(other, L.CLOUD_WINDOW_CLASS),
            f"不误匹配其它窗口类名: {other}",
        )
    # 守住根因本身：字符串 pattern 在这里是无效写法
    check(
        not any(isinstance(p, str) for p in L.CLOUD_WINDOW_CLASS),
        "CLOUD_WINDOW_CLASS 用编译好的正则，不用字符串"
        "（字符串会被当成精确相等，永远匹配不到 Qt<版本> 类名）",
    )
    check(
        not _match_class_name("Qt51517QWindowOwnDC", ("Qt",)),
        "反证：字符串 'Qt' 确实匹配不到实机类名（这就是原 bug 的根因）",
    )

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

    # --- 界面选项必须走载体节点 ---
    #
    # 三个选项原本全部覆盖 CloudGameLaunchMain.custom_action_param。GUI 合并
    # 多个 option 的 pipeline_override 时这个字段是整体替换的，只有最后一个
    # （等待超时）能活下来：用户填的启动器路径被忽略、「等待进入游戏」关不掉。
    from custom.action.CloudGame.action import OPTION_NODES

    for node in OPTION_NODES:
        check(node in pipe, f"载体节点存在: {node}")
        if node not in pipe:
            continue
        check_equal(pipe[node].get("enabled"), False, f"{node} 不参与流程")
        check_equal(pipe[node].get("attach"), {}, f"{node} 的 attach 默认为空")

    provided = set()
    for opt_name in task.get("option", []):
        body = task_data["option"].get(opt_name) or {}
        blocks = [body.get("pipeline_override")]
        for case in body.get("cases", []) or []:
            blocks.append(case.get("pipeline_override"))
        for block in blocks:
            for node, patch in (block or {}).items():
                check(
                    node != task["entry"],
                    f"{opt_name} 不再覆盖 {task['entry']} 的 custom_action_param"
                    "（会被 GUI 整体替换，多个选项互相冲掉）",
                )
                check(node in OPTION_NODES, f"{opt_name} 只写载体节点（{node}）")
                attach = patch.get("attach")
                check(
                    isinstance(attach, dict) and bool(attach),
                    f"{opt_name} 对 {node} 的覆盖写在非空 attach 里",
                )
                if isinstance(attach, dict):
                    provided.update(attach.keys())

    for key in ("launcher_path", "wait_in_game", "ready_timeout"):
        check(key in provided, f"参数 {key} 有界面下发路径")

    # --- 载体节点 attach -> 参数字典 ---
    from custom.action.CloudGame.action import _collect_option_params

    class Ctx:
        def __init__(self, table):
            self.table = table

        def get_node_data(self, node):
            if node not in self.table:
                raise RuntimeError("node not found")
            return self.table[node]

    def table(**attaches):
        data = {n: {"enabled": False, "attach": {}} for n in OPTION_NODES}
        for node, attach in attaches.items():
            data[node] = {"enabled": False, "attach": attach}
        return data

    got = _collect_option_params(
        Ctx(
            table(
                CloudGameLaunch_PathOption={"launcher_path": "D:/cloud/game.exe"},
                CloudGameLaunch_WaitInGameOption={"wait_in_game": False},
                CloudGameLaunch_ReadyTimeoutOption={"ready_timeout": 900},
            )
        )
    )
    # 核心回归：三个选项同时生效。原实现下这里只会剩最后一个键。
    check_equal(len(got), 3, f"三个选项同时生效，互不覆盖（实际 {sorted(got)}）")
    check_equal(got.get("launcher_path"), "D:/cloud/game.exe", "收集到启动器路径")
    check_equal(got.get("wait_in_game"), False, "wait_in_game=False 被保留")
    check_equal(got.get("ready_timeout"), 900, "收集到等待上限")

    # 路径留空是常态（走自动探测），必须回落到 custom_action_param
    default_param = entry.get("custom_action_param") or {}
    for bad, label in ((None, "null"), ("", "空串"), ("   ", "全空格")):
        got = _collect_option_params(
            Ctx(
                table(
                    CloudGameLaunch_PathOption={"launcher_path": bad},
                    CloudGameLaunch_ReadyTimeoutOption={"ready_timeout": 900},
                )
            )
        )
        check("launcher_path" not in got, f"launcher_path={label} 时不产生该键")
        check_equal(got.get("ready_timeout"), 900, f"{label} 不影响其它选项")
        merged = {**default_param, **got}
        check_equal(
            merged.get("launcher_path"), "", f"{label} 时回落到入口默认（自动探测）"
        )

    check_equal(_collect_option_params(Ctx(table())), {}, "选项全空时返回空字典")
    check_equal(_collect_option_params(Ctx({})), {}, "节点读不到时返回空字典")
    check_equal(
        _collect_option_params(Ctx({n: {"attach": "oops"} for n in OPTION_NODES})),
        {},
        "attach 类型不对时忽略",
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


def group_launcher_ui():
    """启动器界面层：文本锚点、阶段判断、时长解析。

    ## 这一组守的是一个真实阻断

    61a07a0 的实现从「窗口出现」直接跳到「等 InWorld」，中间**没有任何点击**。
    但实机启动器停在主页需要点「开始游戏」，随后还有一个带 30 秒倒计时的确认
    弹窗（超时自动「退出启动」）。所以原实现在实机上永远进不了游戏。

    这里的断言全部基于实机 OCR 结果（``debug/cloudgame/023714_restored.png``），
    不是编出来的字符串。
    """
    print("[5] 启动器界面：文本锚点与时长解析")

    from custom.action.CloudGame.launcher_ui import (
        LauncherScreen,
        TextHit,
        parse_duration_minutes,
        read_playtime,
    )

    def screen(*items) -> LauncherScreen:
        return LauncherScreen(
            hits=[TextHit(text=t, box=b, score=s) for t, b, s in items]
        )

    # —— 实机主页：19 条文本里与判断相关的那些，box 与分数照实机填 ——
    home = screen(
        ("UID:171099121", (877, 144, 127, 18), 0.98),
        ("畅玩卡", (878, 169, 76, 28), 1.00),
        ("未开通", (958, 173, 51, 18), 1.00),
        ("月卡特权：", (818, 236, 72, 19), 0.98),
        ("剩余时长", (801, 328, 84, 22), 0.99),
        ("免费时长：", (822, 378, 86, 19), 0.99),
        ("11小时23分钟", (845, 406, 132, 22), 1.00),
        # 实机这条被 OCR 读成带 emoji 的样子，这正是不能用相等匹配的原因
        ("😄付费时长：", (820, 460, 88, 23), 0.90),
        ("0小时0分钟", (844, 489, 105, 23), 0.99),
        ("充值", (1128, 477, 42, 25), 1.00),
        ("开始游戏", (973, 566, 75, 24), 1.00),
    )

    check(home.is_home, "实机主页应判定为 is_home")
    check(not home.is_confirm_dialog, "实机主页不应误判为确认弹窗")
    check(home.is_logged_in, "UID 可见应判定为已登录")
    start = home.find("开始游戏")
    check(start is not None, "主页应能找到「开始游戏」")
    check_equal(start.center, (1010, 578), "「开始游戏」点击中心")

    free, paid = read_playtime(home)
    check_equal(free, 11 * 60 + 23, "免费时长解析为分钟")
    # 付费标签带 emoji 前缀，仍必须能配到下方的值
    check_equal(paid, 0, "付费时长解析为分钟（标签含 emoji 也要能配对）")

    # —— 确认弹窗：文案来自用户实机截图 ——
    dialog = screen(
        ("本次游戏将使用您的免费时长或计费时长", (445, 303, 396, 22), 0.99),
        ("不再提醒", (615, 345, 88, 22), 0.99),
        ("退出启动 (29S)", (447, 421, 152, 28), 0.98),
        ("进入游戏", (727, 423, 80, 24), 1.00),
    )
    check(dialog.is_confirm_dialog, "确认弹窗应判定为 is_confirm_dialog")
    check(not dialog.is_home, "确认弹窗不应判定为主页（没有「开始游戏」）")
    check_equal(dialog.confirm_countdown, 29, "应能读出倒计时秒数")
    enter = dialog.find("进入游戏")
    check(enter is not None, "弹窗应能找到「进入游戏」")

    # 标题被 OCR 拆开时，靠两个按钮同时在也要能判定
    dialog_split = screen(
        ("本次游戏将使用您的", (445, 303, 200, 22), 0.95),
        ("退出启动 (12S)", (447, 421, 152, 28), 0.97),
        ("进入游戏", (727, 423, 80, 24), 1.00),
    )
    check(
        dialog_split.is_confirm_dialog,
        "标题被拆行时仍应靠两个按钮判定为确认弹窗",
    )

    # 弹窗与主页同时可见时（弹窗盖在主页上），必须判成弹窗——它有倒计时
    overlay = screen(
        ("开始游戏", (973, 566, 75, 24), 1.00),
        ("退出启动 (30S)", (447, 421, 152, 28), 0.97),
        ("进入游戏", (727, 423, 80, 24), 1.00),
    )
    check(overlay.is_confirm_dialog, "弹窗盖在主页上时应判成弹窗")
    check(
        not overlay.is_home,
        "弹窗盖在主页上时不得判成主页，否则会去点被遮住的「开始游戏」",
    )

    # —— 时长文本解析的边界 ——
    check_equal(parse_duration_minutes("11小时23分钟"), 683, "标准时长文本")
    check_equal(
        parse_duration_minutes("0 小时 0 分钟"), 0, "OCR 插空格也要能解析"
    )
    check_equal(parse_duration_minutes("剩余时长"), None, "非时长文本返回 None")
    check_equal(parse_duration_minutes(""), None, "空文本返回 None")
    check_equal(parse_duration_minutes(None), None, "None 输入不得抛异常")

    # 读不到时长必须是 None 而不是 0：两者语义完全不同，
    # 把「没看清」当成「时长耗尽」会让 OCR 抖一帧就中止任务。
    #
    # 这里要覆盖**两种**读不到：
    #   (a) 连标签都没有（还在加载）
    #   (b) 标签在、但值没读出来（值尚未渲染，或 OCR 漏了那一条）
    # 只测 (a) 是不够的——那种情况在 anchor 检查处就提前返回了，
    # 走不到真正的配对逻辑，等于没测到。这个盲区是变异测试暴露出来的。
    blank = screen(("加载中", (600, 350, 80, 22), 0.95))
    free2, paid2 = read_playtime(blank)
    check_equal(free2, None, "连标签都没有时免费时长应为 None")
    check_equal(paid2, None, "连标签都没有时付费时长应为 None")

    label_only = screen(
        ("UID:171099121", (877, 144, 127, 18), 0.98),
        ("免费时长：", (822, 378, 86, 19), 0.99),
        ("付费时长：", (820, 460, 88, 23), 0.90),
        ("开始游戏", (973, 566, 75, 24), 1.00),
    )
    free3, paid3 = read_playtime(label_only)
    check_equal(free3, None, "标签在但值没读到时免费时长应为 None 而非 0")
    check_equal(paid3, None, "标签在但值没读到时付费时长应为 None 而非 0")

    # 值离标签太远（属于另一栏）不得被错配过来
    far_value = screen(
        ("免费时长：", (822, 378, 86, 19), 0.99),
        ("3小时0分钟", (120, 406, 132, 22), 1.00),
    )
    free4, _ = read_playtime(far_value)
    check_equal(free4, None, "水平相距过远的时长值不得被错配到该标签")

    print(f"     完成，累计断言 {_CHECKS} 项")


class FakeClickController(FakeController):
    """记录点击的假控制器。

    只实现 ``post_touch_move / post_touch_down / post_touch_up``，**故意不提供**
    ``post_click``：实机在 AgentServer 上下文里调 ``post_click`` 会抛
    ``access violation reading 0xFFFFFFFFFFFFFFFF``（跨进程代理不支持该 API），
    所以实现必须走 touch 三段式。假控制器不提供 post_click，实现一旦退回去用它
    就会立刻 AttributeError 而不是静默通过。

    同时记录调用顺序：必须是 move -> down -> up，缺一步或顺序错都算失败。
    """

    def __init__(self, frames):
        super().__init__(frames)
        self.clicks: list[tuple[int, int]] = []
        self.calls: list[str] = []
        self._pending: tuple[int, int] | None = None

    def post_touch_move(self, x, y, contact=0, pressure=1):
        self.calls.append("move")
        self._pending = (int(x), int(y))
        return _Wrap(True)

    def post_touch_down(self, x, y, contact=0, pressure=1):
        self.calls.append("down")
        self._pending = (int(x), int(y))
        return _Wrap(True)

    def post_touch_up(self, contact=0):
        self.calls.append("up")
        # 一次完整点击以抬起为准，避免把 move 也计成一次点击
        if self._pending is not None:
            self.clicks.append(self._pending)
            self._pending = None
        return _Wrap(True)


class ExpiringControllerTasker:
    """模拟实机行为：每次访问 ``.controller`` 都返回**新的**代理对象，
    且上一次取到的那个立刻失效。

    这是实机测出来的真实语义——在 AgentServer 上下文里
    ``context.tasker.controller`` 每次访问都是新对象、新句柄（实测
    ``同一对象=False 同一句柄=False``）。谁把它取出来跨调用缓存，
    输入 API 就会抛 ``access violation``。

    假环境必须复现这个语义，否则「缓存 controller」这个缺陷永远测不出来：
    实机上它表现为截图正常、点击全炸且偶发成功，极难定位。
    """

    def __init__(self, backend):
        self._backend = backend
        self._issued: list["_ExpiringController"] = []
        self.stopping = False

    @property
    def controller(self):
        for old in self._issued:
            old._expired = True
        fresh = _ExpiringController(self._backend)
        self._issued.append(fresh)
        return fresh


class _ExpiringController:
    """一次性 controller 代理：被下一次取用后即失效。"""

    def __init__(self, backend):
        self._backend = backend
        self._expired = False

    def _guard(self):
        if self._expired:
            # 对应实机的 OSError: access violation
            raise OSError(
                "exception: access violation reading 0xFFFFFFFFFFFFFFFF"
            )

    def post_screencap(self):
        self._guard()
        return self._backend.post_screencap()

    def post_touch_move(self, x, y, contact=0, pressure=1):
        self._guard()
        return self._backend.post_touch_move(x, y, contact, pressure)

    def post_touch_down(self, x, y, contact=0, pressure=1):
        self._guard()
        return self._backend.post_touch_down(x, y, contact, pressure)

    def post_touch_up(self, contact=0):
        self._guard()
        return self._backend.post_touch_up(contact)


class FakeEnterContext:
    """按帧标签驱动进入流程的假 context。

    ``frames`` 是帧标签序列；``in_game`` 里的标签视为已在游戏内；
    ``screens`` 把标签映射到 ``LauncherScreen``。

    tasker 用 :class:`ExpiringControllerTasker`，复现实机「controller 句柄
    每次访问都变」的语义。
    """

    def __init__(self, frames, in_game=(), screens=None, raise_nodes=()):
        self._backend = FakeClickController(frames)
        self.tasker = ExpiringControllerTasker(self._backend)
        self._in_game = set(in_game)
        self._screens = screens or {}
        self._raise = set(raise_nodes)
        self.ocr_calls = 0

    def run_recognition(self, node, image):
        if node in self._raise:
            raise RuntimeError(f"boom:{node}")
        return _Hit(image in self._in_game and node == "InWorld")

    def ocr_screen(self, image):
        self.ocr_calls += 1
        from custom.action.CloudGame.launcher_ui import LauncherScreen

        return self._screens.get(image) or LauncherScreen(hits=[])

    @property
    def clicks(self):
        # 读后端而不是 tasker.controller：后者每次访问都会新建代理并让旧的失效
        return self._backend.clicks

    @property
    def calls(self):
        return self._backend.calls


def group_enter():
    """进入流程状态机：点按钮、处理倒计时弹窗、排队上限、时长为 0 即停。"""
    print("[6] 进入流程：状态机与放弃条件")

    from custom.action.CloudGame.enter import CLICK_COOLDOWN, enter_cloud_game
    from custom.action.CloudGame.launcher_ui import LauncherScreen, TextHit

    def screen(*items) -> LauncherScreen:
        return LauncherScreen(
            hits=[TextHit(text=t, box=b, score=1.0) for t, b in items]
        )

    home = screen(
        ("UID:171099121", (877, 144, 127, 18)),
        ("免费时长：", (822, 378, 86, 19)),
        ("11小时23分钟", (845, 406, 132, 22)),
        ("付费时长：", (820, 460, 88, 23)),
        ("0小时0分钟", (844, 489, 105, 23)),
        ("开始游戏", (973, 566, 75, 24)),
    )
    dialog = screen(
        ("本次游戏将使用您的免费时长或计费时长", (445, 303, 396, 22)),
        ("退出启动 (29S)", (447, 421, 152, 28)),
        ("进入游戏", (727, 423, 80, 24)),
    )
    queueing = screen(("正在排队", (600, 350, 80, 22)))
    no_time = screen(
        ("UID:171099121", (877, 144, 127, 18)),
        ("免费时长：", (822, 378, 86, 19)),
        ("0小时0分钟", (845, 406, 132, 22)),
        ("付费时长：", (820, 460, 88, 23)),
        ("0小时0分钟", (844, 489, 105, 23)),
        ("开始游戏", (973, 566, 75, 24)),
    )

    # —— 正常路径：主页 -> 点开始 -> 弹窗 -> 点进入 -> 排队 -> 进游戏 ——
    clock = FakeClock()
    ctx = FakeEnterContext(
        frames=["home", "dialog", "queue", "queue", "game", "game"],
        in_game={"game"},
        screens={"home": home, "dialog": dialog, "queue": queueing},
    )
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        confirm_hits=2,
    )
    check(result.ok, "正常路径应成功进入游戏")
    check_equal(result.stage, "in_game", "成功时阶段应为 in_game")
    check_equal(
        ctx.clicks[0], (1010, 578), "第一次点击应落在「开始游戏」中心"
    )
    check_equal(
        ctx.clicks[1], (767, 435), "第二次点击应落在「进入游戏」中心"
    )
    check_equal(len(ctx.clicks), 2, "正常路径只应点两次，不得在排队页乱点")

    # 点击必须是完整的 move -> down -> up 三段式。
    # 实机在 AgentServer 上下文里 post_click 会抛访问违例（跨进程代理不支持），
    # 而同一 controller 的 post_screencap 正常，所以只能走 touch 三段式。
    # 缺 move 会让依赖 hover 的控件收不到事件；缺 up 会一直按住。
    check_equal(
        ctx.calls,
        ["move", "down", "up"] * 2,
        "每次点击都应是完整的 move -> down -> up",
    )

    # —— 时长为 0 必须立即停，且一次都不能点 ——
    clock = FakeClock()
    ctx = FakeEnterContext(
        frames=["home"] * 6, screens={"home": no_time}
    )
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
    )
    check(not result.ok, "时长为 0 应失败退出")
    check_equal(result.stage, "no_playtime", "阶段应为 no_playtime")
    check_equal(
        len(ctx.clicks), 0, "时长为 0 时一次都不该点——点了也进不去"
    )
    check("剩余时长为 0" in result.message, "失败原因应说明时长为 0")

    # 关掉这个开关后就不该因时长为 0 而停
    clock = FakeClock()
    ctx = FakeEnterContext(frames=["home"] * 4, screens={"home": no_time})
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        enter_timeout=3.0,
        stop_when_no_playtime=False,
    )
    check_equal(
        result.stage, "timeout", "关掉时长检查后应继续尝试直到超时"
    )
    check(len(ctx.clicks) >= 1, "关掉时长检查后仍应点「开始游戏」")

    # —— 读不到时长（None）不得当作时长耗尽 ——
    # 两种形态都要覆盖：连标签都没有，以及标签在但值没读出来。
    # 后者才是真实风险：值那一条 OCR 偶尔会漏，若把它当成 0，
    # 任务就会以「剩余时长为 0」中止，而用户账号里其实还有时长。
    blank_home = screen(("开始游戏", (973, 566, 75, 24)))
    label_only_home = screen(
        ("UID:171099121", (877, 144, 127, 18)),
        ("免费时长：", (822, 378, 86, 19)),
        ("付费时长：", (820, 460, 88, 23)),
        ("开始游戏", (973, 566, 75, 24)),
    )
    for label, variant in (
        ("无标签", blank_home),
        ("标签在但值读不到", label_only_home),
    ):
        clock = FakeClock()
        ctx = FakeEnterContext(frames=["home"] * 4, screens={"home": variant})
        result = enter_cloud_game(
            ctx,
            ocr_screen=ctx.ocr_screen,
            clock=clock,
            sleeper=clock.sleep,
            poll_interval=1.0,
            enter_timeout=3.0,
        )
        check(
            result.stage != "no_playtime",
            f"{label}：读不到时长不得判成时长耗尽（OCR 抖一帧就停任务不可接受）",
        )
        check(
            len(ctx.clicks) >= 1, f"{label}：读不到时长仍应尝试点「开始游戏」"
        )

    # —— 确认弹窗优先于主页：弹窗盖在主页上时必须点「进入游戏」 ——
    overlay = screen(
        ("开始游戏", (973, 566, 75, 24)),
        ("退出启动 (30S)", (447, 421, 152, 28)),
        ("进入游戏", (727, 423, 80, 24)),
    )
    clock = FakeClock()
    ctx = FakeEnterContext(
        frames=["ov", "ov", "game", "game"],
        in_game={"game"},
        screens={"ov": overlay},
    )
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        confirm_hits=2,
    )
    check(result.ok, "弹窗覆盖主页时也应能进入游戏")
    check_equal(
        ctx.clicks[0],
        (767, 435),
        "弹窗覆盖主页时应点「进入游戏」而非被遮住的「开始游戏」",
    )

    # —— 点击冷却：同一按钮不得每轮连点 ——
    clock = FakeClock()
    ctx = FakeEnterContext(frames=["dialog"] * 10, screens={"dialog": dialog})
    enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        enter_timeout=9.0,
    )
    check(
        len(ctx.clicks) <= 4,
        f"9 秒内点击次数应受 {CLICK_COOLDOWN}s 冷却限制，实际 {len(ctx.clicks)}",
    )
    check(len(ctx.clicks) >= 2, "冷却期过后应重试点击")

    # —— 排队超上限：放弃并报错（用户选定的行为）——
    clock = FakeClock()
    ctx = FakeEnterContext(frames=["queue"] * 200, screens={"queue": queueing})
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        max_queue_time=10.0,
        enter_timeout=600.0,
    )
    check(not result.ok, "排队超上限应失败")
    check_equal(result.stage, "queue_timeout", "阶段应为 queue_timeout")
    check(
        clock.now < 600.0,
        "排队上限应先于总超时触发，不该白等到 enter_timeout",
    )
    check("排队" in result.message, "失败原因应提到排队")

    # 排队计时从离开主页起算，不含在主页/弹窗停留的时间：
    # 否则等登录、等弹窗的时间会被算进排队，提前误判超时。
    clock = FakeClock()
    ctx = FakeEnterContext(
        frames=["home"] * 8 + ["queue"] * 8 + ["game", "game"],
        in_game={"game"},
        screens={"home": home, "dialog": dialog, "queue": queueing},
    )
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        max_queue_time=10.0,
        confirm_hits=2,
    )
    check(
        result.ok,
        "在主页停留 8 轮后再排队 8 轮，不应因排队上限 10s 而失败",
    )

    # —— 识别异常不得放行 ——
    clock = FakeClock()
    ctx = FakeEnterContext(
        frames=["queue"] * 6,
        screens={"queue": queueing},
        raise_nodes=("InWorld", "InMiniWorld"),
    )
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        enter_timeout=5.0,
    )
    check(
        not result.ok, "游戏内识别抛异常时不得判定为已进入游戏"
    )

    # —— OCR 抛异常时不得崩溃，应继续轮询 ——
    class BoomContext(FakeEnterContext):
        def ocr_screen(self, image):
            raise RuntimeError("ocr boom")

    clock = FakeClock()
    ctx = BoomContext(frames=["x"] * 6)
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        enter_timeout=4.0,
    )
    check(not result.ok, "OCR 持续异常应以超时失败而不是抛出")
    check_equal(len(ctx.clicks), 0, "OCR 异常时不得盲点")

    # —— 任务被停止应立即返回 ——
    clock = FakeClock()
    ctx = FakeEnterContext(frames=["home"] * 4, screens={"home": home})
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        should_stop=lambda: True,
    )
    check(not result.ok, "被停止应返回失败")
    check_equal(result.stage, "stopped", "阶段应为 stopped")
    check_equal(len(ctx.clicks), 0, "被停止后不得再点击")

    # —— 已在游戏内时不该去点任何按钮 ——
    clock = FakeClock()
    ctx = FakeEnterContext(
        frames=["game", "game"], in_game={"game"}, screens={}
    )
    result = enter_cloud_game(
        ctx,
        ocr_screen=ctx.ocr_screen,
        clock=clock,
        sleeper=clock.sleep,
        poll_interval=1.0,
        confirm_hits=2,
    )
    check(result.ok, "已在游戏内应直接成功")
    check_equal(len(ctx.clicks), 0, "已在游戏内不得点击任何按钮")
    check_equal(ctx.ocr_calls, 0, "已在游戏内不必跑启动器 OCR")

    print(f"     完成，累计断言 {_CHECKS} 项")


def main():
    print("=" * 68)
    print("云异环启动任务离线验证")
    print("=" * 68)
    group_locator()
    group_launcher()
    group_ready()
    group_wiring()
    group_launcher_ui()
    group_enter()
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
