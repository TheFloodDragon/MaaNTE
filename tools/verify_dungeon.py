"""刷本流程的离线验证。

与 ``verify_combat.py`` 同样的思路：所有外部依赖（步骤执行、传送、战斗、
时钟）都可注入，因此整个刷本循环能在没有游戏的情况下跑闭环仿真。

重点验证的不是"正常情况能跑通"，而是**出错时不会在游戏里乱点**：

- 配置缺 roi / 缺战斗脚本 / 未标定 -> 必须拒绝运行，且一步都不执行
- 传送失败 -> 不进副本
- 战斗失败 -> 不领奖（否则会在没打完的界面上乱点）
- 累计失败达上限 -> 停止，不无限重试
- 收到停止信号 -> 立刻退出

用法：``python tools/verify_dungeon.py``
返回码非 0 表示有问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_fishpro_assets import strip_jsonc  # noqa: E402

from agent.custom.action.Dungeon.config import (  # noqa: E402
    LOOP_MODE_AGAIN,
    LOOP_MODE_REENTER,
    PHASE_ADVANCE,
    PHASE_CONFIRM,
    PHASE_ENTRY,
    PHASE_EXIT,
    PHASE_LOCATE,
    PHASE_RECOVER,
    PHASE_REWARD,
    STEP_CLICK,
    STEP_HOLD,
    STEP_KEY,
    STEP_NODE,
    STEP_OCR,
    STEP_SEARCH,
    STEP_TEMPLATE,
    STEP_WAIT,
    parse_config,
    parse_step,
)
from agent.custom.action.Dungeon.runner import (  # noqa: E402
    REASON_CONFIG_INVALID,
    REASON_DONE,
    REASON_STOPPED,
    REASON_TELEPORT_FAILED,
    REASON_TOO_MANY_FAILURES,
    ROUND_ADVANCE_FAILED,
    ROUND_COMBAT_FAILED,
    ROUND_CONFIRM_FAILED,
    ROUND_ENTRY_FAILED,
    ROUND_LOCATE_FAILED,
    ROUND_SETTLE_TIMEOUT,
    DungeonFarmRunner,
)

_CHECKS = 0
_FAILURES: list[str] = []


def check(condition, label):
    global _CHECKS
    _CHECKS += 1
    if not condition:
        _FAILURES.append(label)


def check_equal(actual, expected, label):
    check(actual == expected, f"{label}（期望 {expected!r}，实际 {actual!r}）")


# ----------------------------------------------------------------------
# 假件
# ----------------------------------------------------------------------


class FakeExecutor:
    """假步骤执行器：按阶段决定成败，记录调用顺序。"""

    def __init__(self, fail_steps=(), settle_hits=True, stop_after=None):
        self.calls: list[str] = []
        self._fail = set(fail_steps)
        self._settle_hits = settle_hits
        self._stop_after = stop_after
        self.stopped = False

    def run_step(self, step):
        label = step.describe()
        self.calls.append(label)
        if self._stop_after is not None and len(self.calls) >= self._stop_after:
            self.stopped = True
        return label not in self._fail

    def recognize(self, step):
        self.calls.append(f"recognize:{step.describe()}")
        return [0, 0, 10, 10] if self._settle_hits else None

    def wait_recognize(self, step):
        return self.recognize(step)


def make_config(**over):
    raw = {
        "name": "验证副本",
        "rounds": 1,
        "combat": {"preset": "basic_attack"},
        "entry": [{"key": "m", "desc": "E1"}],
        "confirm": [{"key": "f", "desc": "C1"}],
        "reward": [{"key": "space", "desc": "R1"}],
        "exit": [{"key": "esc", "desc": "X1"}],
    }
    raw.update(over)
    return parse_config(raw)


def make_runner(config, **over):
    clock = {"t": 0.0}

    def tick():
        return clock["t"]

    def sleep(duration):
        clock["t"] += max(float(duration), 0.001)

    kwargs = {
        "teleport": None,
        "combat": lambda: True,
        "log": lambda *a: None,
        "clock": tick,
        "sleep": sleep,
    }
    kwargs.update(over)
    executor = kwargs.pop("executor", None) or FakeExecutor()
    runner = DungeonFarmRunner(executor, config, **kwargs)
    return runner, executor, clock


# ----------------------------------------------------------------------
# 分组
# ----------------------------------------------------------------------


def group_step_parsing():
    print("[1] 步骤解析：合法写法与非法写法")

    errors: list[str] = []
    issues: list[str] = []

    # 裸字符串 = 节点名
    step = parse_step("SceneAnyEnterWorld", "t", errors, issues)
    check_equal(step.type, STEP_NODE, "裸字符串解析为节点步骤")
    check_equal(step.node, "SceneAnyEnterWorld", "节点名正确")

    # 类型推断
    for raw, expect in (
        ({"ocr": "进入", "roi": [0, 0, 10, 10]}, STEP_OCR),
        ({"template": "a.png", "roi": [0, 0, 10, 10]}, STEP_TEMPLATE),
        ({"node": "N"}, STEP_NODE),
        ({"key": "f"}, STEP_KEY),
        ({"wait": 1.0}, STEP_WAIT),
        ({"click_rect": [1, 2, 3, 4]}, STEP_CLICK),
    ):
        errors.clear()
        step = parse_step(raw, "t", errors, issues)
        check(step is not None, f"可解析: {raw}")
        if step is not None:
            check_equal(step.type, expect, f"类型推断正确: {raw}")
        check_equal(errors, [], f"无错误: {raw}")

    # OCR 多语言文本
    errors.clear()
    step = parse_step(
        {"ocr": ["进入", "Enter"], "roi": [0, 0, 10, 10]}, "t", errors, issues
    )
    check_equal(len(step.text), 2, "OCR 支持多个候选文本")

    # --- 非法写法必须报错 ---
    bad_cases = [
        ({"ocr": "x"}, "OCR 缺 roi"),
        ({"ocr": "x", "roi": [1, 2, 3]}, "roi 不是 4 个数"),
        ({"ocr": "x", "roi": [1, 2, 0, 5]}, "roi 宽为 0"),
        ({"ocr": "x", "roi": [1, 2, 5, -1]}, "roi 高为负"),
        ({"ocr": "x", "roi": ["a", 2, 3, 4]}, "roi 含非数字"),
        ({"ocr": "", "roi": [0, 0, 5, 5]}, "OCR 文本为空"),
        ({"template": "", "roi": [0, 0, 5, 5]}, "模板路径为空"),
        ({"key": "nosuchkey"}, "未知按键"),
        ({"wait": -1}, "wait 为负"),
        ({"wait": "abc"}, "wait 非数字"),
        ({"type": "teleport"}, "未知步骤类型"),
        ({}, "无法判断类型"),
        (123, "步骤不是对象或字符串"),
        ({"click_rect": None}, "click_rect 缺失"),
    ]
    for raw, label in bad_cases:
        errors.clear()
        step = parse_step(raw, "t", errors, issues)
        check(step is None, f"拒绝: {label}")
        check(len(errors) > 0, f"记录错误原因: {label}")

    # ROI 越界只警告不报错
    errors.clear()
    issues.clear()
    step = parse_step(
        {"ocr": "x", "roi": [1200, 700, 200, 100]}, "t", errors, issues
    )
    check(step is not None, "越界 ROI 仍可解析")
    check(any("超出" in i for i in issues), "越界 ROI 有警告")

    # --- hold：按住方向键前进 ---
    errors.clear()
    issues.clear()
    step = parse_step({"hold": "w", "duration": 1.5}, "t", errors, issues)
    check_equal(step.type, STEP_HOLD, "hold 步骤类型推断正确")
    check_equal(step.key, "w", "hold 记住要按住的键")
    check_equal(step.duration, 1.5, "hold 记住时长")

    errors.clear()
    step = parse_step(
        {"type": "hold", "key": "lshift", "duration": 0.4}, "t", errors, issues
    )
    check(step is not None and step.key == "lshift", "hold 支持分开写 key/duration")

    for raw, label in (
        ({"hold": "w"}, "hold 缺 duration"),
        ({"hold": "w", "duration": 0}, "hold duration 为 0"),
        ({"hold": "w", "duration": -1}, "hold duration 为负"),
        ({"hold": "left", "duration": 1}, "hold 用鼠标键（内核不发）"),
        ({"hold": "nosuchkey", "duration": 1}, "hold 用未知键"),
    ):
        errors.clear()
        step = parse_step(raw, "t", errors, issues)
        check(step is None, f"拒绝: {label}")
        check(len(errors) > 0, f"记录错误原因: {label}")

    # 过长的 hold 截断但不拒绝：它只是走得太远，不是配置错
    errors.clear()
    issues.clear()
    step = parse_step({"hold": "w", "duration": 99}, "t", errors, issues)
    check(step is not None and step.duration <= 10.0, "过长 hold 被截断")
    check(any("hold" in i for i in issues), "过长 hold 有提示")

    # --- search：探测循环 ---
    errors.clear()
    issues.clear()
    step = parse_step(
        {
            "until": {"ocr": "领取奖励", "roi": [0, 0, 100, 40]},
            "probe": [{"hold": "w", "duration": 1.0}, {"key": "f"}],
            "on_found": [{"key": "f"}],
            "sweep": [{"hold": "d", "duration": 0.4}],
            "sweep_every": 4,
            "timeout": 90,
        },
        "t",
        errors,
        issues,
    )
    check_equal(step.type, STEP_SEARCH, "有 until 即推断为 search")
    check_equal(step.until.type, STEP_OCR, "until 解析为识别步骤")
    check(
        not step.until.click,
        "until 默认不点击（观测位置点下去会在不确定界面上乱按）",
    )
    check_equal(len(step.probe), 2, "probe 步骤数正确")
    check_equal(len(step.on_found), 1, "on_found 步骤数正确")
    check_equal(step.sweep_every, 4, "sweep_every 正确")
    check_equal(step.timeout, 90.0, "search timeout 正确")

    # until 用裸节点名（复用已验证的敌人血条节点）
    errors.clear()
    step = parse_step(
        {
            "type": "search",
            "until": "PinkPawHeist_CheckMonsterOnce",
            "probe": [{"hold": "w", "duration": 1.0}],
        },
        "t",
        errors,
        issues,
    )
    check(step is not None and not errors, f"until 支持裸节点名: {errors}")
    check(
        step is not None and not step.until.click,
        "裸节点名的 until 也默认不点击",
    )

    for raw, label, keyword in (
        (
            {"type": "search", "probe": [{"hold": "w", "duration": 1}]},
            "search 缺 until",
            "until",
        ),
        (
            {"until": {"ocr": "x", "roi": [0, 0, 5, 5]}},
            "search 缺 probe",
            "probe",
        ),
        (
            {"until": {"key": "f"}, "probe": [{"hold": "w", "duration": 1}]},
            "until 不是识别类步骤",
            "识别",
        ),
        (
            {
                "until": {"ocr": "x", "roi": [0, 0, 5, 5], "click": True},
                "probe": [{"hold": "w", "duration": 1}],
            },
            "until 显式要求点击",
            "click",
        ),
        (
            {
                "until": {"ocr": "x", "roi": [0, 0, 5, 5]},
                "probe": [
                    {
                        "type": "search",
                        "until": {"ocr": "y", "roi": [0, 0, 5, 5]},
                        "probe": [{"hold": "w", "duration": 1}],
                    }
                ],
            },
            "search 嵌套 search",
            "嵌套",
        ),
    ):
        errors.clear()
        step = parse_step(raw, "t", errors, issues)
        check(step is None, f"拒绝: {label}")
        check(
            any(keyword in e for e in errors),
            f"错误信息说明原因（含「{keyword}」）: {label} -> {errors}",
        )

    # 给了 sweep 但没给 sweep_every：补默认值并提示，不能静默丢掉 sweep
    errors.clear()
    issues.clear()
    step = parse_step(
        {
            "until": {"ocr": "x", "roi": [0, 0, 5, 5]},
            "probe": [{"hold": "w", "duration": 1}],
            "sweep": [{"hold": "d", "duration": 0.3}],
        },
        "t",
        errors,
        issues,
    )
    check(step is not None and step.sweep_every > 0, "sweep 缺 sweep_every 时补默认")
    check(any("sweep_every" in i for i in issues), "sweep_every 缺失有提示")

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_config_validation():
    print("[2] 配置校验：致命错误必须拒绝运行")

    cfg = make_config()
    check(cfg.ok, "基准配置合法")
    check_equal(cfg.errors, [], "基准配置无错误")
    check_equal(cfg.rounds, 1, "轮次解析正确")

    # 缺战斗脚本 —— AutoCombat 不提供隐式默认，这里必须提前拦住
    cfg = make_config(combat={})
    check(not cfg.ok, "缺战斗脚本被拒绝")
    check(
        any("script / preset / script_path" in e for e in cfg.errors),
        "错误信息指明要配什么",
    )

    # 没有任何阶段
    cfg = parse_config({"name": "空", "combat": {"preset": "basic_attack"}})
    check(not cfg.ok, "无任何阶段步骤被拒绝")

    # 未标定守卫
    cfg = parse_config(
        {
            "name": "模板",
            "_calibrated": False,
            "combat": {"preset": "basic_attack"},
            "entry": [{"key": "m"}],
        }
    )
    check(not cfg.ok, "未标定配置被拒绝")
    check(
        any("未标定" in e for e in cfg.errors),
        "未标定的原因写清楚了",
    )

    # 标定完成后可用
    cfg = parse_config(
        {
            "name": "已标定",
            "_calibrated": True,
            "combat": {"preset": "basic_attack"},
            "entry": [{"key": "m"}],
        }
    )
    check(cfg.ok, "标定为 true 后可运行")

    # rounds 校验
    check(not parse_config({"rounds": -1}).ok, "负轮次被拒绝")
    check(not parse_config({"rounds": "3"}).ok, "字符串轮次被拒绝")
    cfg = make_config(rounds=0)
    check(cfg.ok, "rounds=0 合法（无限循环）")

    # settle 必须是识别类
    cfg = make_config(settle={"key": "f"})
    check(not cfg.ok, "settle 不能是动作步骤")

    cfg = make_config(settle={"ocr": "结算", "roi": [0, 0, 10, 10]})
    check(cfg.ok, "settle 可以是 OCR")

    # 非对象配置
    check(not parse_config([]).ok, "数组配置被拒绝")
    check(not parse_config("x").ok, "字符串配置被拒绝")

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_farm_loop():
    print("[3] 刷本循环：顺序、失败处理、停止响应")

    # --- 正常三轮 ---
    cfg = make_config(rounds=3)
    teleports = []
    runner, executor, _ = make_runner(
        cfg, teleport=lambda: (teleports.append(1), True)[1]
    )
    result = runner.run()
    check_equal(result.reason, REASON_DONE, "三轮全成功")
    check_equal(result.rounds_done, 3, "完成 3 轮")
    check_equal(result.rounds_failed, 0, "无失败")
    check_equal(len(teleports), 1, "传送只做一次，不是每轮都传")

    # 阶段顺序：entry -> confirm -> reward -> exit
    order = [c for c in executor.calls if c in ("E1", "C1", "R1", "X1")]
    check_equal(order[:4], ["E1", "C1", "R1", "X1"], "阶段顺序正确")

    # --- 战斗失败不得领奖 ---
    cfg = make_config()
    runner, executor, _ = make_runner(cfg, combat=lambda: False)
    result = runner.run()
    check(
        ROUND_COMBAT_FAILED in result.round_reasons,
        "战斗失败被记录",
    )
    check("R1" not in executor.calls, "战斗失败后不执行领奖")
    check("X1" not in executor.calls, "战斗失败后不执行退出")

    # --- entry 失败 ---
    cfg = make_config()
    runner, executor, _ = make_runner(cfg, executor=FakeExecutor(fail_steps=["E1"]))
    result = runner.run()
    check(ROUND_ENTRY_FAILED in result.round_reasons, "entry 失败被记录")
    check("C1" not in executor.calls, "entry 失败后不进入 confirm")

    # --- confirm 失败 ---
    cfg = make_config()
    runner, executor, _ = make_runner(cfg, executor=FakeExecutor(fail_steps=["C1"]))
    result = runner.run()
    check(ROUND_CONFIRM_FAILED in result.round_reasons, "confirm 失败被记录")

    # --- 累计失败达上限则停止 ---
    cfg = make_config(rounds=10, max_failures=2)
    runner, executor, _ = make_runner(cfg, executor=FakeExecutor(fail_steps=["E1"]))
    result = runner.run()
    check_equal(
        result.reason, REASON_TOO_MANY_FAILURES, "失败达上限后停止"
    )
    check_equal(result.rounds_failed, 2, "恰好失败 2 次就停")
    check(not result.success, "全失败时 success=False")

    # --- 传送失败不进副本 ---
    cfg = make_config()
    runner, executor, _ = make_runner(cfg, teleport=lambda: False)
    result = runner.run()
    check_equal(result.reason, REASON_TELEPORT_FAILED, "传送失败即止")
    check_equal(executor.calls, [], "传送失败后一步都不执行")

    # --- advance / locate 阶段的位置与失败语义 ---
    combat_calls = []

    def marking_combat():
        combat_calls.append(len(executor.calls))
        return True

    cfg = make_config(
        advance=[{"key": "w", "desc": "A1"}],
        locate=[{"key": "d", "desc": "L1"}],
    )
    runner, executor, _ = make_runner(cfg, combat=marking_combat)
    result = runner.run()
    check_equal(result.reason, REASON_DONE, "带 advance/locate 的一轮能跑通")
    order = [c for c in executor.calls if c in ("E1", "C1", "A1", "L1", "R1", "X1")]
    check_equal(
        order,
        ["E1", "C1", "A1", "L1", "R1", "X1"],
        "顺序为 entry->confirm->advance->(战斗)->locate->reward->exit",
    )
    check_equal(
        combat_calls[0], 3, "战斗在 advance 之后才开始（前进开战，不是先打后走）"
    )

    # advance 失败：不能进战斗，也不能领奖
    cfg = make_config(
        advance=[{"key": "w", "desc": "A1"}],
        locate=[{"key": "d", "desc": "L1"}],
    )
    fought = []
    runner, executor, _ = make_runner(
        cfg,
        executor=FakeExecutor(fail_steps=["A1"]),
        combat=lambda: (fought.append(1), True)[1],
    )
    result = runner.run()
    check(ROUND_ADVANCE_FAILED in result.round_reasons, "advance 失败被单独记录")
    check_equal(fought, [], "advance 失败后不进战斗（没找到怪就不该乱按技能）")
    check("R1" not in executor.calls, "advance 失败后不领奖")

    # locate 失败：不能领奖（还没走到奖励点，点下去就是乱点）
    cfg = make_config(
        advance=[{"key": "w", "desc": "A1"}],
        locate=[{"key": "d", "desc": "L1"}],
    )
    runner, executor, _ = make_runner(cfg, executor=FakeExecutor(fail_steps=["L1"]))
    result = runner.run()
    check(ROUND_LOCATE_FAILED in result.round_reasons, "locate 失败被单独记录")
    check("R1" not in executor.calls, "locate 失败后不领奖")
    check("X1" not in executor.calls, "locate 失败后不执行退出")

    # --- loop_mode=again：成功轮之后跳过 entry/confirm ---
    cfg = make_config(rounds=3, loop_mode="again", advance=[{"key": "w", "desc": "A1"}])
    runner, executor, _ = make_runner(cfg)
    result = runner.run()
    check_equal(result.rounds_done, 3, "again 模式三轮全成功")
    check_equal(
        executor.calls.count("E1"), 1, "again 模式只在第一轮走 entry"
    )
    check_equal(
        executor.calls.count("C1"), 1, "again 模式只在第一轮走 confirm"
    )
    check_equal(executor.calls.count("A1"), 3, "again 模式每轮都要前进开战")
    check_equal(executor.calls.count("R1"), 3, "again 模式每轮都领奖")

    # reenter（默认）模式每轮都要重新进
    cfg = make_config(rounds=3, advance=[{"key": "w", "desc": "A1"}])
    check_equal(cfg.loop_mode, LOOP_MODE_REENTER, "默认 loop_mode 是 reenter")
    runner, executor, _ = make_runner(cfg)
    runner.run()
    check_equal(executor.calls.count("E1"), 3, "reenter 模式每轮都走 entry")

    # again 模式下某轮失败后，下一轮必须重新走完整入口
    cfg = make_config(rounds=3, loop_mode="again", max_failures=5)
    calls = {"n": 0}

    class FlakyExecutor(FakeExecutor):
        """第一轮的 reward 失败，其余都成功。"""

        def run_step(self, step):
            label = step.describe()
            if label == "R1":
                calls["n"] += 1
                self.calls.append(label)
                return calls["n"] != 1
            return super().run_step(step)

    runner, executor, _ = make_runner(cfg, executor=FlakyExecutor())
    result = runner.run()
    check_equal(result.rounds_failed, 1, "again 模式下失败被记录")
    check(
        executor.calls.count("E1") >= 2,
        "失败后下一轮重新走 entry（界面已不确定，不能再跳过）",
    )

    # --- 结算超时 ---
    cfg = make_config(
        settle={"ocr": "结算", "roi": [0, 0, 10, 10]}, settle_timeout=3.0
    )
    runner, executor, _ = make_runner(
        cfg, executor=FakeExecutor(settle_hits=False)
    )
    result = runner.run()
    check(
        ROUND_SETTLE_TIMEOUT in result.round_reasons, "结算超时被记录"
    )
    check("R1" not in executor.calls, "结算超时后不领奖")

    # --- 结算命中则继续 ---
    cfg = make_config(settle={"ocr": "结算", "roi": [0, 0, 10, 10]})
    runner, executor, _ = make_runner(cfg)
    result = runner.run()
    check_equal(result.reason, REASON_DONE, "结算命中后正常完成")
    check("R1" in executor.calls, "结算命中后领奖")

    # --- 配置无效直接拒绝，且不执行任何步骤 ---
    cfg = make_config(combat={})
    runner, executor, _ = make_runner(cfg)
    result = runner.run()
    check_equal(result.reason, REASON_CONFIG_INVALID, "配置无效被拒绝")
    check_equal(executor.calls, [], "配置无效时一步都不执行")

    # --- 停止信号 ---
    cfg = make_config(rounds=0)  # 无限循环
    executor = FakeExecutor()
    calls = {"n": 0}

    def raise_if_stopped():
        calls["n"] += 1
        if calls["n"] > 12:
            from agent.custom.action.Combat.kernel.errors import (
                TaskerStoppedException,
            )

            raise TaskerStoppedException("test stop")

    runner, _, _ = make_runner(
        cfg, executor=executor, raise_if_stopped=raise_if_stopped
    )
    result = runner.run()
    check_equal(result.reason, REASON_STOPPED, "无限循环能被停止")

    # --- 可选步骤失败不中断 ---
    cfg = make_config(
        entry=[
            {"key": "m", "desc": "E1"},
            {"ocr": "可能有的弹窗", "roi": [0, 0, 10, 10],
             "required": False, "desc": "OPT", "timeout": 0},
        ]
    )
    runner, executor, _ = make_runner(
        cfg, executor=FakeExecutor(fail_steps=["OPT"])
    )
    result = runner.run()
    check_equal(result.reason, REASON_DONE, "可选步骤失败不影响整轮")

    # --- 恢复流程 ---
    cfg = make_config(
        rounds=3,
        max_failures=5,
        **{PHASE_RECOVER: [{"key": "esc", "desc": "RC"}]},
    )
    runner, executor, _ = make_runner(cfg, executor=FakeExecutor(fail_steps=["E1"]))
    result = runner.run()
    check("RC" in executor.calls, "失败后执行恢复流程")

    # --- 恢复流程也失败则停止 ---
    cfg = make_config(
        rounds=5,
        max_failures=5,
        **{PHASE_RECOVER: [{"key": "esc", "desc": "RC"}]},
    )
    runner, executor, _ = make_runner(
        cfg, executor=FakeExecutor(fail_steps=["E1", "RC"])
    )
    result = runner.run()
    check_equal(
        result.reason, REASON_TOO_MANY_FAILURES, "恢复失败则停止刷本"
    )

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_config_dir():
    print("[4] 配置目录：开发布局与发布包布局都要能找到")

    import os
    import tempfile

    from agent.custom.action.Dungeon import action as da

    original_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        # 发布包布局：resource/base/dungeon，没有 assets 层
        release = Path(tmp) / "release"
        (release / "resource" / "base" / "dungeon").mkdir(parents=True)
        (release / "resource" / "base" / "dungeon" / "demo.json").write_text(
            '{"name":"demo","combat":{"preset":"basic_attack"},'
            '"entry":[{"key":"m"}]}',
            encoding="utf-8",
        )
        try:
            os.chdir(release)
            found = da._config_dir()
            check(
                found.is_dir() and found.name == "dungeon",
                "发布包布局能找到配置目录",
            )
            raw, source = da._load_config_source({"dungeon": "demo"})
            check(raw is not None, "发布包布局能加载配置")
            check("demo" in source, "来源标注正确")
        finally:
            os.chdir(original_cwd)

        # 开发布局：assets/resource/base/dungeon
        dev = Path(tmp) / "dev"
        (dev / "assets" / "resource" / "base" / "dungeon").mkdir(parents=True)
        try:
            os.chdir(dev)
            found = da._config_dir()
            check(
                found.is_dir() and found.name == "dungeon",
                "开发布局能找到配置目录",
            )
        finally:
            os.chdir(original_cwd)

    # 目录穿越防护
    for evil in ("../secret", "..\\secret", "/etc/passwd", ".hidden", "a/b"):
        raw, source = da._load_config_source({"dungeon": evil})
        check(raw is None, f"拒绝可疑配置名: {evil!r}")
        check(
            "非法" in source or "不存在" in source,
            f"拒绝原因明确: {evil!r}",
        )

    # 未指定任何来源
    raw, source = da._load_config_source({})
    check(raw is None, "未指定配置时拒绝")
    check("未指定副本配置" in source, "拒绝原因明确")

    # 内联优先级最高
    raw, source = da._load_config_source(
        {"config": {"name": "inline"}, "dungeon": "demo",
         "config_path": "x.json"}
    )
    check("custom_action_param" in source, "内联配置优先级最高")

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_shipped_configs():
    print("[5] 随包配置：能解析，未标定的必须被拦住")

    from agent.custom.action.Dungeon.action import _config_dir

    config_dir = _config_dir()
    if not config_dir.is_dir():
        print("     跳过（配置目录不存在）")
        return

    import json

    files = sorted(config_dir.glob("*.json"))
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            check(False, f"{path.name} 不是合法 JSON: {exc}")
            continue

        cfg = parse_config(raw)
        if raw.get("_calibrated") is False:
            check(
                not cfg.ok,
                f"{path.name} 标记未标定，必须被拒绝",
            )
            print(f"  [ok] {path.name}: 模板配置，已正确拦截")
            # 模板是用户照抄的样板：除了占位 roi，写法本身必须是合法的。
            # 否则用户填完坐标才发现结构不对，只会怀疑是自己抄错了。
            probe = dict(raw)
            probe.pop("_calibrated", None)
            probe_cfg = parse_config(probe)
            check(
                probe_cfg.ok,
                f"{path.name} 去掉未标定标记后结构合法: {probe_cfg.errors}",
            )
            check_equal(
                probe_cfg.issues, [], f"{path.name} 结构上无警告"
            )
        else:
            check(cfg.ok, f"{path.name} 解析无致命错误: {cfg.errors}")
            print(f"  [ok] {path.name}: {cfg.describe()}")

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_option_overrides():
    """界面选项必须真正生效。

    「选了但没生效」是一类很难在实机上察觉的 bug：用户以为改了配置，
    实际跑的还是文件里的旧值，而日志看起来一切正常。必须有断言兜着。
    """
    from agent.custom.action.Dungeon.config import (
        ERROR_COMBAT_NO_SCRIPT,
        apply_overrides,
    )

    print("[6] 界面选项覆盖：选了必须生效")

    def base_raw(**extra):
        raw = {
            "name": "t",
            "rounds": 5,
            "teleport_point_id": "fountain",
            "entry": [{"key": "f"}],
            "combat": {"preset": "skill_rotation"},
        }
        raw.update(extra)
        return raw

    # rounds 覆盖
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {"rounds": 3})
    check_equal(cfg.rounds, 3, "rounds 被界面覆盖")

    # rounds=0 表示无限，不能被当成「没给」而忽略
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {"rounds": 0})
    check_equal(cfg.rounds, 0, "rounds=0（无限）能被覆盖")

    # rounds 非法时保留原值并给提示
    cfg = parse_config(base_raw())
    notes = apply_overrides(cfg, {"rounds": "abc"})
    check_equal(cfg.rounds, 5, "rounds 非法时沿用配置值")
    check(any("rounds" in n for n in notes), "rounds 非法有提示")

    # 跳过传送
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {"skip_teleport": True})
    check_equal(cfg.teleport_point_id, "", "skip_teleport 清空传送点")

    # skip_teleport=False 不应清空
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {"skip_teleport": False})
    check_equal(
        cfg.teleport_point_id, "fountain", "skip_teleport=False 保留传送点"
    )

    # 传送点覆盖
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {"teleport_point_id": "rabbithole"})
    check_equal(cfg.teleport_point_id, "rabbithole", "传送点被覆盖")

    # 战斗预设覆盖，且必须清掉互斥来源
    cfg = parse_config(
        base_raw(combat={"script": {"fallback": ["e"]}, "duration": 90})
    )
    apply_overrides(cfg, {"combat_preset": "basic_attack"})
    check_equal(cfg.combat.get("preset"), "basic_attack", "预设被覆盖")
    check(
        "script" not in cfg.combat,
        "覆盖预设时清掉 script（否则 AutoCombat 优先取 script，界面选择失效）",
    )
    check_equal(cfg.combat.get("duration"), 90, "覆盖预设不影响其它战斗参数")

    # 空预设表示「用配置里的」，不能把配置清掉
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {"combat_preset": ""})
    check_equal(
        cfg.combat.get("preset"), "skill_rotation", "空预设时沿用配置"
    )

    # 关键回归：配置只写了 combat 的其它参数，靠界面选预设补上脚本
    cfg = parse_config(base_raw(combat={"duration": 200}))
    check(
        ERROR_COMBAT_NO_SCRIPT in cfg.errors,
        "只给 duration 时解析期报缺少脚本",
    )
    apply_overrides(cfg, {"combat_preset": "basic_attack"})
    check(
        cfg.ok,
        "界面选了预设后不再被误拒（脚本已就位）",
    )
    check_equal(cfg.combat.get("duration"), 200, "保留用户写的 duration")

    # 没选预设时，缺脚本的错误必须保留
    cfg = parse_config(base_raw(combat={"duration": 200}))
    apply_overrides(cfg, {})
    check(
        not cfg.ok and ERROR_COMBAT_NO_SCRIPT in cfg.errors,
        "没选预设时仍拒绝缺脚本的配置",
    )

    # 撤销只针对「缺脚本」，其它错误不能被顺带清掉
    cfg = parse_config(
        {"name": "t", "entry": [{"ocr": "x"}], "combat": {"duration": 1}}
    )
    before = len(cfg.errors)
    apply_overrides(cfg, {"combat_preset": "basic_attack"})
    check(
        not cfg.ok,
        "缺 roi 等其它错误不会被预设覆盖顺带清掉",
    )
    check(
        len(cfg.errors) == before - 1,
        "只撤掉一条缺脚本错误",
    )

    # log_decisions 透传
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {"log_decisions": True})
    check_equal(cfg.combat.get("log_decisions"), True, "log_decisions 生效")
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {"log_decisions": False})
    check_equal(
        cfg.combat.get("log_decisions"), False, "log_decisions 可显式关闭"
    )

    # 空参数不改动任何东西
    cfg = parse_config(base_raw())
    apply_overrides(cfg, {})
    check_equal(cfg.rounds, 5, "空参数不改 rounds")
    check_equal(cfg.teleport_point_id, "fountain", "空参数不改传送点")

    # --- stage：改写副本列表里要选哪一项 ---
    def stage_raw(**extra):
        raw = base_raw(
            entry=[
                {"ocr": ["钟表把戏"], "roi": [10, 10, 180, 600], "role": "stage"},
                {"click_rect": [210, 671, 34, 36], "role": "difficulty"},
            ],
            difficulty_rects=[
                [16, 671, 34, 36],
                [64, 671, 34, 36],
                [113, 671, 34, 36],
            ],
        )
        raw.update(extra)
        return raw

    cfg = parse_config(stage_raw())
    apply_overrides(cfg, {"stage": "守卫萝卜"})
    stage_step = cfg.steps(PHASE_ENTRY)[0]
    check_equal(list(stage_step.text), ["守卫萝卜"], "stage 覆盖了 OCR 文本")
    check_equal(
        list(stage_step.roi), [10, 10, 180, 600], "stage 覆盖不动 roi（列表位置没变）"
    )

    # 空 stage 表示「用配置里的」
    cfg = parse_config(stage_raw())
    apply_overrides(cfg, {"stage": ""})
    check_equal(
        list(cfg.steps(PHASE_ENTRY)[0].text), ["钟表把戏"], "空 stage 时沿用配置"
    )

    # 配置里没有 stage 挂钩时必须明确提示，不能静默无效
    cfg = parse_config(base_raw())
    notes = apply_overrides(cfg, {"stage": "守卫萝卜"})
    check(
        any("stage" in n for n in notes),
        "配置缺 role=stage 时提示界面选择不生效",
    )

    # --- difficulty：按档位改写点击矩形 ---
    cfg = parse_config(stage_raw())
    apply_overrides(cfg, {"difficulty": 2})
    diff_step = cfg.steps(PHASE_ENTRY)[1]
    check_equal(list(diff_step.roi), [64, 671, 34, 36], "difficulty=2 用第 2 档矩形")

    # 0 表示不动难度：整步移除，避免点一个不确定的坐标
    cfg = parse_config(stage_raw())
    notes = apply_overrides(cfg, {"difficulty": 0})
    check_equal(len(cfg.steps(PHASE_ENTRY)), 1, "difficulty=0 移除难度步骤")
    check(any("难度" in n for n in notes), "difficulty=0 有说明")

    # 超出档位数：保留原值并提示，不能去点一个不存在的按钮
    cfg = parse_config(stage_raw())
    notes = apply_overrides(cfg, {"difficulty": 9})
    check_equal(
        list(cfg.steps(PHASE_ENTRY)[1].roi),
        [210, 671, 34, 36],
        "难度超界时沿用配置矩形",
    )
    check(any("超出" in n for n in notes), "难度超界有提示")

    # 没有 difficulty_rects 时选难度必须提示无效
    cfg = parse_config(
        base_raw(
            entry=[{"click_rect": [1, 2, 3, 4], "role": "difficulty"}]
        )
    )
    notes = apply_overrides(cfg, {"difficulty": 3})
    check(
        any("difficulty_rects" in n for n in notes),
        "缺 difficulty_rects 时提示难度选择不生效",
    )

    # 非法值不改动
    cfg = parse_config(stage_raw())
    notes = apply_overrides(cfg, {"difficulty": "abc"})
    check_equal(
        list(cfg.steps(PHASE_ENTRY)[1].roi),
        [210, 671, 34, 36],
        "difficulty 非法时沿用配置",
    )
    check(any("difficulty" in n for n in notes), "difficulty 非法有提示")

    # 空 difficulty_rects 等价于缺省：模板里写 [] 只是在说明字段存在，
    # 不该因此被拒绝运行
    cfg = parse_config(base_raw(difficulty_rects=[]))
    check(cfg.ok, f"空 difficulty_rects 不算错误: {cfg.errors}")
    check_equal(cfg.difficulty_rects, (), "空 difficulty_rects 解析为空元组")

    # 类型不对才报错
    cfg = parse_config(base_raw(difficulty_rects="abc"))
    check(not cfg.ok, "difficulty_rects 类型不对时拒绝")

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_step_executor():
    """步骤执行器：按住键必须松手，探测循环必须按约定转。

    这一组测的是「在游戏里到底做了什么」，用假内核记录调用序列。
    最关键的两条：

    - ``hold`` 抬手必须发生，哪怕等待被异常打断——否则角色会一直往前走。
    - ``search`` 命中前不能碰 ``on_found``，超时后不能报成功——否则会在
      还没走到奖励点的界面上点「领取」。
    """
    from agent.custom.action.Dungeon.steps import StepExecutor

    print("[7] 步骤执行：按住/探测/ROI 缩放")

    class FakeActionHelper:
        def __init__(self):
            self.stop_after = None
            self.calls = 0

        def raise_if_stopped(self):
            self.calls += 1
            if self.stop_after is not None and self.calls > self.stop_after:
                from agent.custom.action.Combat.kernel.errors import (
                    TaskerStoppedException,
                )

                raise TaskerStoppedException("stopped")

    class FakeKernel:
        def __init__(self, image=None, sleep_raises=False):
            self.ah = FakeActionHelper()
            self.events: list[str] = []
            # 非 None 才会走到识别；用 sentinel 而不是真图，
            # 这一组测的是调用序列，不是图像内容。
            self.image = image if image is not None else object()
            self._sleep_raises = sleep_raises

        def screencap(self):
            return self.image

        def send_key_down(self, key):
            self.events.append(f"down:{key}")

        def send_key_up(self, key):
            self.events.append(f"up:{key}")

        def send_key(self, key):
            self.events.append(f"tap:{key}")

        def sleep(self, duration, **kwargs):
            self.events.append(f"sleep:{duration}")
            if self._sleep_raises:
                raise RuntimeError("interrupted")

        def click(self, x, y):
            self.events.append(f"click:{x},{y}")

    class FakeResult:
        def __init__(self, box):
            self.hit = box is not None
            self.box = box

    class FakeContext:
        """按调用次数决定第几次才命中，用来模拟「走几步才看到」。"""

        def __init__(self, hit_on_call=1, box=(10, 20, 30, 40)):
            self.recognitions: list[tuple] = []
            self.tasks: list[str] = []
            self._hit_on = hit_on_call
            self._box = box

        def run_recognition(self, node, image, pipeline_override=None):
            roi = None
            if pipeline_override:
                param = pipeline_override[node]["recognition"]["param"]
                roi = tuple(param.get("roi", ()))
            self.recognitions.append((node, roi))
            if self._hit_on and len(self.recognitions) >= self._hit_on:
                return FakeResult(self._box)
            return FakeResult(None)

        def run_task(self, node, pipeline_override=None):
            self.tasks.append(node)
            return object()

    def make_step(raw):
        errors: list[str] = []
        issues: list[str] = []
        step = parse_step(raw, "t", errors, issues)
        assert step is not None, errors
        return step

    def fake_clock():
        return clock["t"]

    clock = {"t": 0.0}

    # --- hold 必须成对按下/抬起 ---
    kernel = FakeKernel()
    executor = StepExecutor(kernel, FakeContext(), clock=fake_clock)
    executor.run_step(make_step({"hold": "w", "duration": 1.2}))
    check_equal(
        kernel.events,
        ["down:w", "sleep:1.2", "up:w"],
        "hold 顺序为按下->等待->抬起",
    )

    # --- 等待被打断也必须抬手 ---
    kernel = FakeKernel(sleep_raises=True)
    executor = StepExecutor(kernel, FakeContext(), clock=fake_clock)
    raised = False
    try:
        executor.run_step(make_step({"hold": "w", "duration": 1.0}))
    except RuntimeError:
        raised = True
    check(raised, "hold 期间的异常会往上抛（不吞掉）")
    check(
        "up:w" in kernel.events,
        "hold 期间异常也必须抬手（否则角色会一直往前走）",
    )

    # --- search：一开始就命中，不该先走一步 ---
    kernel = FakeKernel()
    ctx = FakeContext(hit_on_call=1)
    executor = StepExecutor(kernel, ctx, clock=fake_clock)
    step = make_step(
        {
            "until": {"ocr": "领取奖励", "roi": [0, 0, 100, 40]},
            "probe": [{"hold": "w", "duration": 1.0}],
            "on_found": [{"key": "f"}],
            "timeout": 30,
        }
    )
    ok = executor.run_step(step)
    check(ok, "search 首次识别命中即成功")
    check_equal(
        [e for e in kernel.events if e.startswith("down:")],
        [],
        "首次就命中时不执行 probe（不该先莽一步走开）",
    )
    check_equal(kernel.events, ["tap:f"], "命中后执行 on_found")

    # --- search：走三轮才命中 ---
    clock["t"] = 0.0
    kernel = FakeKernel()
    ctx = FakeContext(hit_on_call=3)
    executor = StepExecutor(kernel, ctx, clock=fake_clock)
    ok = executor.run_step(step)
    check(ok, "search 多轮后命中仍算成功")
    check_equal(
        kernel.events.count("down:w"), 2, "命中前走了 2 轮 probe（第 3 次识别命中）"
    )
    check_equal(kernel.events[-1], "tap:f", "命中后才执行 on_found")

    # --- search：超时必须失败，且不执行 on_found ---
    clock["t"] = 0.0
    kernel = FakeKernel()
    ctx = FakeContext(hit_on_call=0)  # 永不命中

    def advancing_sleep(duration, **kwargs):
        kernel.events.append(f"sleep:{duration}")
        clock["t"] += float(duration)

    kernel.sleep = advancing_sleep
    executor = StepExecutor(kernel, ctx, clock=fake_clock)
    step_short = make_step(
        {
            "until": {"ocr": "领取奖励", "roi": [0, 0, 100, 40]},
            "probe": [{"hold": "w", "duration": 2.0}],
            "on_found": [{"key": "f"}],
            "timeout": 5,
        }
    )
    ok = executor.run_step(step_short)
    check(not ok, "search 超时返回失败")
    check(
        "tap:f" not in kernel.events,
        "超时后不执行 on_found（没走到就不能点领取）",
    )

    # --- search：sweep 按 sweep_every 触发 ---
    clock["t"] = 0.0
    kernel = FakeKernel()
    kernel.sleep = advancing_sleep
    ctx = FakeContext(hit_on_call=0)
    executor = StepExecutor(kernel, ctx, clock=fake_clock)
    step_sweep = make_step(
        {
            "until": {"ocr": "x", "roi": [0, 0, 10, 10]},
            "probe": [{"hold": "w", "duration": 1.0}],
            "sweep": [{"hold": "d", "duration": 0.5}],
            "sweep_every": 2,
            "timeout": 6,
        }
    )
    executor.run_step(step_sweep)
    forwards = kernel.events.count("down:w")
    turns = kernel.events.count("down:d")
    check(forwards >= 4, f"探测循环持续前进（实际 {forwards} 次）")
    check_equal(turns, forwards // 2, "每 2 轮转向一次")

    # --- 时钟不推进时，轮数上限必须兜住 ---
    # 这是防死循环的保险：等待被卡住/被替换掉是真实故障，
    # 那种情况下只靠 timeout 判断会让角色一直按着前进键跑下去。
    from agent.custom.action.Dungeon.steps import MAX_SEARCH_ROUNDS

    kernel = FakeKernel()  # sleep 不推进时钟
    ctx = FakeContext(hit_on_call=0)
    executor = StepExecutor(kernel, ctx, clock=lambda: 0.0)
    step_stuck = make_step(
        {
            "until": {"ocr": "x", "roi": [0, 0, 10, 10]},
            "probe": [{"hold": "w", "duration": 1.0}],
            "timeout": 3600,
        }
    )
    ok = executor.run_step(step_stuck)
    check(not ok, "时钟不推进时 search 仍会结束并判失败")
    check_equal(
        kernel.events.count("down:w"),
        MAX_SEARCH_ROUNDS,
        "探测轮数被硬上限截断（不会无限前进）",
    )

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_roi_scaling():
    """ROI 必须按截图尺寸换算。

    720p 之外的窗口很常见（1080p 全屏、云游戏）。不换算的话每个 ROI 都会
    偏到左上角，而日志只会写「没识别到」——这类问题极难从现象反推原因，
    所以必须有断言。
    """
    try:
        import numpy as np
    except ImportError:
        print("[8] ROI 缩放：跳过（缺 numpy）")
        return

    from agent.custom.action.Dungeon.steps import StepExecutor

    print("[8] ROI 缩放：720p 基准换算到实际截图尺寸")

    class Kernel:
        class ah:
            @staticmethod
            def raise_if_stopped():
                return None

        def __init__(self, image):
            self._image = image
            self.clicks: list[tuple] = []

        def screencap(self):
            return self._image

        def click(self, x, y):
            self.clicks.append((x, y))

        def sleep(self, *a, **k):
            return None

    class Ctx:
        def __init__(self):
            self.rois: list[tuple] = []

        def run_recognition(self, node, image, pipeline_override=None):
            param = pipeline_override[node]["recognition"]["param"]
            self.rois.append(tuple(param["roi"]))

            class R:
                hit = False
                box = None

            return R()

    errors: list[str] = []
    issues: list[str] = []
    ocr_step = parse_step(
        {"ocr": "x", "roi": [100, 200, 300, 100], "timeout": 0}, "t", errors, issues
    )
    click_step = parse_step({"click_rect": [200, 400, 40, 20]}, "t", errors, issues)

    for size, expect_roi, expect_click in (
        ((720, 1280), (100, 200, 300, 100), (220, 410)),
        ((1080, 1920), (150, 300, 450, 150), (330, 615)),
    ):
        image = np.zeros((size[0], size[1], 3), dtype=np.uint8)
        kernel = Kernel(image)
        ctx = Ctx()
        executor = StepExecutor(kernel, ctx, clock=lambda: 0.0)
        executor.recognize(ocr_step)
        check_equal(
            ctx.rois[-1], expect_roi, f"{size[1]}x{size[0]} 的 OCR ROI 换算"
        )
        executor.run_step(click_step)
        check_equal(
            kernel.clicks[-1],
            expect_click,
            f"{size[1]}x{size[0]} 的点击矩形换算到中心",
        )

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_rabbit_hole_config():
    """随包的兔子洞配置：流程要完整，引用要真实存在，ROI 不能压到危险按钮。

    这一组是对**具体标定值**的自动核对。配置里的坐标和文案不会因为写错就
    报错，只会在实机上点错地方，所以能自动查的都要查：

    - 引用的传送点、pipeline 节点必须真实存在（否则运行时静默失败）
    - 「领取」的 ROI 不能碰到「双倍领取」（多花一倍代价，且用户很难察觉）
    - 前进开战、定位出口这两段必须真的是探测循环，不是干等
    """
    import json
    import re

    print("[9] 兔子洞配置：流程完整性与标定健全性")
    config_path = (
        REPO / "assets" / "resource" / "base" / "dungeon" / "rabbit_hole.json"
    )
    if not config_path.exists():
        check(False, f"兔子洞配置不存在: {config_path}")
        return

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    cfg = parse_config(raw)
    check(cfg.ok, f"兔子洞配置无致命错误: {cfg.errors}")
    check_equal(cfg.issues, [], "兔子洞配置无警告")

    # --- 传送点必须真实存在 ---
    points_path = (
        REPO / "assets" / "resource" / "base" / "map_teleport" / "teleport_points.json"
    )
    points = json.loads(points_path.read_text(encoding="utf-8"))
    ids = {p.get("id") for p in points.get("teleport_points", [])}
    check(
        cfg.teleport_point_id in ids,
        f"传送点 {cfg.teleport_point_id!r} 在 teleport_points.json 里存在",
    )

    # --- 引用的 pipeline 节点必须真实存在 ---
    # pipeline 文件是 JSONC（带注释，个别文件还有尾随逗号），必须先规整再解析，
    # 否则每个带注释的文件都会被误报成「不是合法 JSON」，把真正要查的
    # 「引用的节点存不存在」淹掉。这里只是为了收集节点名，不承担校验
    # pipeline 格式的职责。
    pipeline_dir = REPO / "assets" / "resource" / "base" / "pipeline"
    trailing_comma = re.compile(r",(\s*[}\]])")
    known_nodes: set[str] = set()
    for path in pipeline_dir.rglob("*.json"):
        text = trailing_comma.sub(
            r"\1", strip_jsonc(path.read_text(encoding="utf-8"))
        )
        try:
            known_nodes.update(json.loads(text).keys())
        except ValueError as exc:
            check(False, f"{path.name} 不是合法 JSON: {exc}")

    def walk(step):
        if step is None:
            return
        if step.type == STEP_SEARCH:
            walk(step.until)
            for group in (step.probe, step.on_found, step.sweep):
                for item in group:
                    walk(item)
            return
        if step.type == STEP_NODE:
            check(
                step.node in known_nodes,
                f"引用的 pipeline 节点存在: {step.node}",
            )

    for phase in (
        PHASE_ENTRY,
        PHASE_CONFIRM,
        PHASE_ADVANCE,
        PHASE_LOCATE,
        PHASE_REWARD,
        PHASE_EXIT,
        PHASE_RECOVER,
    ):
        for step in cfg.steps(phase):
            walk(step)

    # --- entry：走过去按 F 进入 ---
    entry = cfg.steps(PHASE_ENTRY)
    check(bool(entry), "entry 阶段非空")
    check_equal(entry[0].type, STEP_SEARCH, "entry 第一步是探测循环（走过去找入口）")
    check_equal(entry[0].until.type, STEP_OCR, "入口判据是 OCR 交互提示")
    check(
        "兔子洞" in entry[0].until.text,
        f"入口判据认「兔子洞」（实际 {list(entry[0].until.text)}）",
    )
    check(
        any(s.type == STEP_HOLD and s.key == "w" for s in entry[0].probe),
        "entry 探测靠按住 W 前进",
    )
    check(
        any(s.type == STEP_KEY and s.key == "f" for s in entry[0].on_found),
        "看到提示后按 F 交互",
    )
    check(
        any(s.role == "stage" for s in entry),
        "entry 里有 role=stage 的副本选择步骤（界面能改要刷哪个）",
    )

    # --- advance：前进开战，判据必须是已验证的敌人血条节点 ---
    advance = cfg.steps(PHASE_ADVANCE)
    searches = [s for s in advance if s.type == STEP_SEARCH]
    check_equal(len(searches), 1, "advance 阶段有且只有一个探测循环")
    if searches:
        check_equal(
            searches[0].until.type, STEP_NODE, "开战判据用 pipeline 节点而不是 OCR"
        )
        check_equal(
            searches[0].until.node,
            "PinkPawHeist_CheckMonsterOnce",
            "开战判据复用已验证的敌人血条节点",
        )
        check(
            any(s.type == STEP_HOLD and s.key == "w" for s in searches[0].probe),
            "advance 靠按住 W 推进",
        )

    # --- locate：打完找出口 ---
    locate = cfg.steps(PHASE_LOCATE)
    locate_searches = [s for s in locate if s.type == STEP_SEARCH]
    check_equal(len(locate_searches), 1, "locate 阶段有且只有一个探测循环")
    if locate_searches:
        found = locate_searches[0]
        check(
            "领取奖励" in found.until.text,
            f"出口判据认「领取奖励」弹窗（实际 {list(found.until.text)}）",
        )
        check(
            any(s.type == STEP_HOLD and s.key == "w" for s in found.probe),
            "locate 边走边找",
        )
        check(
            any(s.type == STEP_KEY and s.key == "f" for s in found.probe),
            "locate 边走边按 F（出口交互点位置不固定）",
        )
        check(found.timeout >= 60, f"locate 超时够长（实际 {found.timeout}s）")

    # --- reward：绝不能碰到「双倍领取」 ---
    # 「领取」在弹窗左半，「双倍领取」在右半（720p 下大约从 x=755 开始）。
    # ROI 压过去会让 OCR 命中右边那个按钮，白花一倍本性像素。
    DOUBLE_CLAIM_LEFT = 720
    reward = cfg.steps(PHASE_REWARD)
    check(bool(reward), "reward 阶段非空")
    for index, step in enumerate(reward):
        if step.type != STEP_OCR:
            continue
        x, _, w, _ = step.roi
        check(
            x + w <= DOUBLE_CLAIM_LEFT,
            f"reward[{index}] 的 roi 右边界 {x + w} 未越过双倍领取区域"
            f"（上限 {DOUBLE_CLAIM_LEFT}）",
        )
        check(
            "双倍领取" not in step.text,
            f"reward[{index}] 不主动去点双倍领取",
        )

    # --- exit：again 模式必须真的点「再次挑战」 ---
    exit_steps = cfg.steps(PHASE_EXIT)
    if cfg.loop_mode == LOOP_MODE_AGAIN:
        check(
            any("再次挑战" in s.text for s in exit_steps if s.type == STEP_OCR),
            "loop_mode=again 时 exit 必须点「再次挑战」",
        )

    # --- combat：必须开清怪收兵，否则每轮白等满 duration ---
    check_equal(
        cfg.combat.get("stop_when_no_enemy"),
        True,
        "combat 开启 stop_when_no_enemy（清怪即收兵）",
    )
    check(
        float(cfg.combat.get("duration", 0)) > 0,
        "combat 仍保留 duration 作为兜底上限",
    )

    # --- 难度矩形：档位齐全且互不重叠 ---
    rects = cfg.difficulty_rects
    check_equal(len(rects), 5, "难度矩形 5 档齐全（I~V）")
    for i in range(len(rects) - 1):
        left = rects[i]
        right = rects[i + 1]
        check(
            left[0] + left[2] <= right[0],
            f"难度矩形 {i + 1} 与 {i + 2} 不重叠（点第 N 档不会点到第 N+1 档）",
        )

    # --- 所有 ROI 必须落在 720p 内 ---
    def check_bounds(step, label):
        if step is None:
            return
        if step.type == STEP_SEARCH:
            check_bounds(step.until, f"{label}.until")
            for name, group in (
                ("probe", step.probe),
                ("on_found", step.on_found),
                ("sweep", step.sweep),
            ):
                for index, item in enumerate(group):
                    check_bounds(item, f"{label}.{name}[{index}]")
            return
        if not step.roi:
            return
        x, y, w, h = step.roi
        check(
            0 <= x and 0 <= y and x + w <= 1280 and y + h <= 720,
            f"{label} 的 roi {list(step.roi)} 在 720p 范围内",
        )

    for phase in (
        PHASE_ENTRY,
        PHASE_CONFIRM,
        PHASE_ADVANCE,
        PHASE_LOCATE,
        PHASE_REWARD,
        PHASE_EXIT,
    ):
        for index, step in enumerate(cfg.steps(phase)):
            check_bounds(step, f"{phase}[{index}]")

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_task_options():
    """任务界面定义：参数必须走载体节点，不能挤在 custom_action_param 里。

    ## 这一组守的是一个真实事故

    实机日志：

        [DungeonFarm] 收到参数: '{"log_decisions":true}'
        [DungeonFarm][ERROR] 未指定副本配置...

    七个选项都覆盖 ``DungeonFarmMain`` 的 ``custom_action_param``，而 GUI 合并
    多个 option 的 ``pipeline_override`` 时这个字段是**整体替换**的：最后一个
    选项（日志开关）把前面全部冲掉，连 pipeline 里写死的 ``dungeon`` 默认值
    一起没了，任务直接退出。

    修法是让每个选项改**各自独立的载体节点**的 ``attach``，字段路径不同就不会
    互相覆盖。所以这里断言两件事：

    1. 选项的 ``pipeline_override`` 不得再碰 ``DungeonFarmMain``；
    2. 每个被引用的载体节点都要在 pipeline 里真实存在。
    """
    import json

    print("[10] 任务界面定义：参数下发方式")

    task_path = REPO / "assets" / "resource" / "tasks" / "DungeonFarm.json"
    data = json.loads(strip_jsonc(task_path.read_text(encoding="utf-8")))
    options = data.get("option") or {}
    tasks = data.get("task") or []
    check_equal(len(tasks), 1, "DungeonFarm.json 只定义一个任务")
    referenced = tasks[0].get("option") or []

    undefined = [name for name in referenced if name not in options]
    check_equal(undefined, [], f"任务引用的 option 都有定义（缺: {undefined}）")

    pipe_path = (
        REPO / "assets" / "resource" / "base" / "pipeline" / "Dungeon" / "DungeonFarm.json"
    )
    pipe = json.loads(strip_jsonc(pipe_path.read_text(encoding="utf-8")))

    from agent.custom.action.Dungeon.action import OPTION_NODES

    # --- 载体节点必须存在且不参与流程 ---
    for node in OPTION_NODES:
        check(node in pipe, f"载体节点 {node} 在 pipeline 里定义了")
        if node not in pipe:
            continue
        check_equal(
            pipe[node].get("enabled"), False, f"{node} 不参与流程（enabled: false）"
        )
        check_equal(
            pipe[node].get("attach"),
            {},
            f"{node} 的 attach 默认为空（有值的那份来自用户选择）",
        )

    def blocks_of(opt):
        yield opt.get("pipeline_override")
        for case in opt.get("cases", []):
            yield case.get("pipeline_override")

    # --- 选项不得再碰 DungeonFarmMain，且只能写已声明的载体节点 ---
    provided = set()
    for name in referenced + [
        sub for opt in options.values() for c in opt.get("cases", []) for sub in c.get("option", [])
    ]:
        opt = options.get(name)
        if opt is None:
            continue
        for block in blocks_of(opt):
            for node, patch in (block or {}).items():
                check(
                    node != "DungeonFarmMain",
                    f"{name} 不再覆盖 DungeonFarmMain 的 custom_action_param"
                    "（会被 GUI 整体替换，多个选项互相冲掉）",
                )
                check(
                    node in OPTION_NODES,
                    f"{name} 覆盖的节点 {node} 是已声明的载体节点",
                )
                attach = patch.get("attach")
                check(
                    isinstance(attach, dict) and bool(attach),
                    f"{name} 对 {node} 的覆盖写在非空 attach 里",
                )
                if isinstance(attach, dict):
                    provided.update(attach.keys())

    # --- 刷本跑不起来就缺的那几个参数都要有下发路径 ---
    for key in ("dungeon", "stage", "difficulty", "rounds", "skip_teleport", "log_decisions"):
        check(key in provided, f"参数 {key} 有界面下发路径")

    # --- default_case 必须真实存在 ---
    for name, opt in options.items():
        default = opt.get("default_case")
        if default is None:
            continue
        case_names = [c.get("name") for c in opt.get("cases", [])]
        check(
            default in case_names,
            f"{name} 的 default_case {default!r} 在 cases 里（cases={case_names}）",
        )

    # --- 入口节点自带的默认配置名：所有选项都没下发时的最后兜底 ---
    node = pipe.get("DungeonFarmMain") or {}
    check_equal(
        node.get("custom_action"), "DungeonFarm", "入口节点绑定 DungeonFarm 动作"
    )
    default_param = node.get("custom_action_param") or {}
    check(
        bool(default_param.get("dungeon")),
        f"入口节点自带默认配置名（实际 {default_param.get('dungeon')!r}）",
    )
    config_dir = REPO / "assets" / "resource" / "base" / "dungeon"
    check(
        (config_dir / f"{default_param.get('dungeon')}.json").exists(),
        f"入口节点默认配置 {default_param.get('dungeon')!r} 的文件存在",
    )

    # --- 副本名 select 的取值必须与游戏内文字一致且不重复 ---
    stage_values = []
    for case in (options.get("DungeonFarmStage") or {}).get("cases", []):
        patch = (case.get("pipeline_override") or {}).get("DungeonFarm_StageOption") or {}
        value = (patch.get("attach") or {}).get("stage")
        if value is not None:
            stage_values.append(value)
    check(len(stage_values) >= 6, f"副本名至少给出 6 个可选项（实际 {len(stage_values)}）")
    check(
        all(isinstance(v, str) and v.strip() for v in stage_values),
        "每个副本名都是非空字符串",
    )
    check_equal(len(set(stage_values)), len(stage_values), "副本名没有重复项")

    cfg = parse_config(
        json.loads((config_dir / "rabbit_hole.json").read_text(encoding="utf-8"))
    )
    default_stage = [
        list(s.text) for s in cfg.steps(PHASE_ENTRY) if s.role == "stage"
    ]
    check_equal(len(default_stage), 1, "兔子洞配置里有且只有一个 role=stage 步骤")
    if default_stage:
        check(
            default_stage[0][0] in stage_values,
            f"配置默认副本名 {default_stage[0][0]!r} 在界面可选项里",
        )

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_option_collection():
    """载体节点 attach -> 参数字典：空值必须跳过，读不到不能崩。

    输入框类选项的占位符替换失败时下发的是 ``null``。若把它当成有效值，
    ``dungeon`` 就成了 ``None``，报出来的仍然是「未指定副本配置」——
    修了下发方式却没修合并语义，等于没修。
    """
    from agent.custom.action.Dungeon.action import (
        OPTION_NODES,
        _collect_option_params,
    )

    print("[11] 载体节点参数收集：空值与缺失的处理")

    class Ctx:
        def __init__(self, table):
            self.table = table

        def get_node_data(self, node):
            if node not in self.table:
                raise RuntimeError("node not found")
            return self.table[node]

    def table(**attaches):
        data = {node: {"enabled": False, "attach": {}} for node in OPTION_NODES}
        for node, attach in attaches.items():
            data[node] = {"enabled": False, "attach": attach}
        return data

    full = table(
        DungeonFarm_ConfigOption={"dungeon": "rabbit_hole"},
        DungeonFarm_StageOption={"stage": "守卫萝卜"},
        DungeonFarm_DifficultyOption={"difficulty": 5},
        DungeonFarm_RoundsOption={"rounds": 3},
        DungeonFarm_TeleportOption={"skip_teleport": True},
        DungeonFarm_LogDecisionsOption={"log_decisions": True},
    )
    got = _collect_option_params(Ctx(full))
    check_equal(got.get("dungeon"), "rabbit_hole", "收集到配置名")
    check_equal(got.get("stage"), "守卫萝卜", "收集到副本名")
    check_equal(got.get("difficulty"), 5, "收集到难度")
    check_equal(got.get("rounds"), 3, "收集到轮数")
    check_equal(got.get("skip_teleport"), True, "收集到跳过传送")
    check_equal(got.get("log_decisions"), True, "收集到日志开关")

    # 关键回归：占位符替换失败下发 null / 空串，必须当作「没给」
    for bad, label in ((None, "null"), ("", "空串"), ("   ", "全空格")):
        broken = table(
            DungeonFarm_ConfigOption={"dungeon": bad},
            DungeonFarm_RoundsOption={"rounds": 3},
        )
        got = _collect_option_params(Ctx(broken))
        check(
            "dungeon" not in got,
            f"dungeon={label} 时不产生该键（否则会拿着空值当配置名）",
        )
        check_equal(got.get("rounds"), 3, f"dungeon={label} 不影响其它选项")
        merged = {**{"dungeon": "rabbit_hole"}, **got}
        check_equal(
            merged["dungeon"],
            "rabbit_hole",
            f"dungeon={label} 时回落到入口节点的默认配置",
        )

    # false / 0 是有效值，不能被当成空值丢掉
    got = _collect_option_params(
        Ctx(
            table(
                DungeonFarm_TeleportOption={"skip_teleport": False},
                DungeonFarm_DifficultyOption={"difficulty": 0},
                DungeonFarm_RoundsOption={"rounds": 0},
                DungeonFarm_LogDecisionsOption={"log_decisions": False},
            )
        )
    )
    check_equal(got.get("skip_teleport"), False, "skip_teleport=False 被保留")
    check_equal(got.get("difficulty"), 0, "difficulty=0（不改难度）被保留")
    check_equal(got.get("rounds"), 0, "rounds=0（一直刷）被保留")
    check_equal(got.get("log_decisions"), False, "log_decisions=False 被保留")

    # 全空 / 读不到都不能抛异常，交给 custom_action_param 兜底
    check_equal(_collect_option_params(Ctx(table())), {}, "选项全空时返回空字典")
    check_equal(_collect_option_params(Ctx({})), {}, "节点读不到时返回空字典")

    # attach 不是对象时也要容错
    weird = {node: {"attach": "oops"} for node in OPTION_NODES}
    check_equal(_collect_option_params(Ctx(weird)), {}, "attach 类型不对时忽略")

    print(f"     完成，累计断言 {_CHECKS} 项")


def main():
    print("=" * 68)
    print("刷本流程离线验证")
    print("=" * 68)
    group_step_parsing()
    group_config_validation()
    group_farm_loop()
    group_config_dir()
    group_shipped_configs()
    group_option_overrides()
    group_step_executor()
    group_roi_scaling()
    group_rabbit_hole_config()
    group_task_options()
    group_option_collection()
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
