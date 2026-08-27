"""战斗内核与编排层的离线验证。

不依赖游戏、不依赖 MaaFramework 控制器。当前已实现的分组：

11. 内核等价性：用合成图逐项比对「提取前的 core3 原实现」与「新内核实现」，
    覆盖队伍 UI、黑屏、四槽位打分（大区域 + 小核心）、ROI 裁剪缩放、
    按键名规范化与参数解析。

其余分组随后续阶段补齐（见 docs/zh_cn/develop/combat-engine.md）。

用法：``python tools/verify_combat.py``
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "tools"))

from agent.custom.action.Combat.kernel import team as kteam  # noqa: E402
from agent.custom.action.Combat.kernel.constants import (  # noqa: E402
    CURRENT_CHAR_MARKER_CORE_ROI,
    CURRENT_CHAR_MARKER_ROI,
    CURRENT_CHAR_SLOT_SPACING,
    TEAM_HEALTH_SLASH_ROI,
)
from agent.custom.action.Combat.kernel.frames import (  # noqa: E402
    as_bgr_image,
    crop_roi,
    fast_color_match,
    is_hit,
    scale_roi,
)
from agent.custom.action.Combat.kernel.input import (  # noqa: E402
    norm_key,
    normalize_key_sequence,
)
from fixtures.baseline_loader import BASELINE_PATH, load_baseline  # noqa: E402

BASE = load_baseline()
BASELINE_SOURCE = BASELINE_PATH.read_text(encoding="utf-8")

_FAILURES: list[str] = []
_CHECKS = 0


def check(condition, label):
    """记录一个断言结果，失败不中断，便于一次跑完看全部问题。"""
    global _CHECKS
    _CHECKS += 1
    if not condition:
        _FAILURES.append(label)
        print(f"  [FAIL] {label}")
    return bool(condition)


def check_equal(actual, expected, label):
    return check(
        actual == expected, f"{label}: expected {expected!r}, got {actual!r}"
    )


# ----------------------------------------------------------------------
# 合成图工具
# ----------------------------------------------------------------------


def blank_frame(width=1280, height=720, value=30):
    """生成一张均匀底色的 BGR 图，模拟无 UI 的游戏画面。"""
    return np.full((height, width, 3), value, dtype=np.uint8)


def paint(frame, roi, color):
    """在指定 ROI 内填充颜色（BGR）。"""
    x, y, w, h = roi
    frame[y : y + h, x : x + w] = np.asarray(color, dtype=np.uint8)
    return frame


def frame_with_team_ui(value=30, slash_color=(215, 215, 215), fill=1.0):
    """生成含底部队伍血条斜杠的画面。

    ``fill`` 控制斜杠区域被点亮的比例，用来构造刚好跨过 10 像素阈值的边界。
    """
    frame = blank_frame(value=value)
    x, y, w, h = TEAM_HEALTH_SLASH_ROI
    lit = max(0, int(round(w * h * fill)))
    if lit <= 0:
        return frame
    flat = frame[y : y + h, x : x + w].reshape(-1, 3)
    flat[:lit] = np.asarray(slash_color, dtype=np.uint8)
    frame[y : y + h, x : x + w] = flat.reshape(h, w, 3)
    return frame


def slot_roi(index, core=False):
    """按槽位索引算出高亮 ROI（与内核相同的 88px 槽距）。"""
    roi = list(CURRENT_CHAR_MARKER_CORE_ROI if core else CURRENT_CHAR_MARKER_ROI)
    roi[1] += CURRENT_CHAR_SLOT_SPACING * index
    return roi


def frame_with_slot_highlight(index, color=(230, 230, 230), core_only=False, fill=1.0):
    """生成"某个槽位被高亮"的画面。

    ``core_only`` 用于模拟二号位暗头像：只点亮小核心区域，大区域保持暗。
    """
    frame = frame_with_team_ui()
    roi = slot_roi(index, core=core_only)
    x, y, w, h = roi
    lit_h = max(1, int(round(h * fill)))
    paint(frame, [x, y, w, lit_h], color)
    return frame


def frame_with_dim_slot2(lit_pixels=3):
    """构造真正的"二号位暗头像"：大区域分数不达标，只能靠小核心兜底。

    二号位大区域门槛是 12（含 +4 bonus），核心门槛是 6（权重 3，即 2 像素）。
    因此只点亮核心区里极少数像素，就能让大区域判定失败而核心判定成立——
    这正是 ``is_slot2_core_accepted`` 存在的理由，必须单独覆盖。
    """
    frame = frame_with_team_ui()
    x, y, w, h = slot_roi(1, core=True)
    flat = frame[y : y + h, x : x + w].reshape(-1, 3)
    flat[:lit_pixels] = np.asarray((235, 235, 235), dtype=np.uint8)
    frame[y : y + h, x : x + w] = flat.reshape(h, w, 3)
    return frame


# ----------------------------------------------------------------------
# 第 11 组：内核等价性
# ----------------------------------------------------------------------


def build_equivalence_corpus():
    """构造覆盖各种判定分支的合成图集合。"""
    frames = {}

    frames["blank_dark"] = blank_frame(value=30)
    frames["pure_black"] = blank_frame(value=0)
    frames["near_black"] = blank_frame(value=17)
    frames["black_threshold_edge"] = blank_frame(value=18)
    frames["just_above_black"] = blank_frame(value=19)

    # 黑屏但有少量亮点（例如加载图标），像素数低于 300 仍算黑屏
    dim = blank_frame(value=5)
    paint(dim, [640, 360, 8, 8], (200, 200, 200))
    frames["black_with_small_light"] = dim

    # 黑屏底色但亮点过多 -> 不算黑屏
    dim2 = blank_frame(value=5)
    paint(dim2, [400, 200, 200, 200], (200, 200, 200))
    frames["dark_with_big_light"] = dim2

    frames["team_ui"] = frame_with_team_ui()
    frames["team_ui_dim_slash"] = frame_with_team_ui(slash_color=(170, 170, 170))
    frames["team_ui_saturated_slash"] = frame_with_team_ui(slash_color=(240, 60, 60))
    frames["team_ui_tiny_slash"] = frame_with_team_ui(fill=0.001)
    frames["no_team_ui"] = blank_frame(value=60)

    for index in range(4):
        frames[f"slot{index}_white"] = frame_with_slot_highlight(index)
        frames[f"slot{index}_colored"] = frame_with_slot_highlight(
            index, color=(200, 60, 200)
        )
        frames[f"slot{index}_partial"] = frame_with_slot_highlight(index, fill=0.15)
    frames["slot1_core_only"] = frame_with_slot_highlight(1, core_only=True)
    frames["slot1_core_only_dim"] = frame_with_slot_highlight(
        1, core_only=True, color=(195, 195, 195)
    )
    # 真正的暗头像兜底路径：大区域不达标，仅核心达标
    frames["slot1_dim_core_fallback"] = frame_with_dim_slot2(lit_pixels=3)
    frames["slot1_dim_core_below"] = frame_with_dim_slot2(lit_pixels=1)

    # 两个槽位同时偏亮，考察"领先差值"判定
    ambiguous = frame_with_team_ui()
    paint(ambiguous, slot_roi(0), (215, 215, 215))
    paint(ambiguous, slot_roi(2), (215, 215, 215))
    frames["slot0_and_slot2"] = ambiguous

    # 非 720p：缩放路径
    small = np.asarray(
        np.clip(
            np.kron(frame_with_slot_highlight(0)[::2, ::2], np.ones((1, 1, 1))), 0, 255
        ),
        dtype=np.uint8,
    )
    frames["slot0_half_res"] = small

    rng = random.Random(20240527)
    noisy = frame_with_slot_highlight(3)
    noise = np.asarray(
        [[[rng.randint(-12, 12) for _ in range(3)] for _ in range(1280)]],
        dtype=np.int16,
    )
    noisy = np.clip(noisy.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    frames["slot3_noisy"] = noisy

    # 非法输入
    frames["empty"] = np.zeros((0, 0, 3), dtype=np.uint8)
    frames["gray_2d"] = np.full((720, 1280), 40, dtype=np.uint8)

    return frames


# ----------------------------------------------------------------------
# 第 1 组：脚本解析
# ----------------------------------------------------------------------


def group_script_parsing():
    from agent.custom.action.Combat.script.schema import (
        ACTION_CLICK,
        ACTION_HOLD,
        ACTION_KEY,
        ACTION_RELEASE,
        ACTION_WAIT,
        MAX_ACTIONS_PER_RULE,
        MAX_RULES,
        parse_script,
    )

    print("[1] 脚本解析：合法输入")

    script = parse_script(
        {
            "name": "basic",
            "rules": [
                {
                    "name": "burst",
                    "when": {"hp_below": 0.5},
                    "actions": ["e", "wait:0.25", {"type": "hold", "key": "w"}],
                    "cooldown": 8,
                    "priority": 10,
                }
            ],
            "fallback": ["space"],
        }
    )
    check_equal(script.issues, (), "解析合法脚本无 issue")
    check_equal(script.name, "basic", "脚本名")
    check_equal(len(script.rules), 1, "规则数")
    rule = script.rules[0]
    check_equal(rule.name, "burst", "规则名")
    check_equal(rule.cooldown, 8.0, "冷却")
    check_equal(rule.priority, 10, "优先级")
    check_equal(
        [a.type for a in rule.actions],
        [ACTION_KEY, ACTION_WAIT, ACTION_HOLD],
        "动作类型序列",
    )
    check_equal(rule.actions[1].duration, 0.25, "wait 时长")
    check_equal([a.type for a in script.fallback], [ACTION_KEY], "兜底动作")

    # 简写形式
    for text, expected_type, expected_key in (
        ("e", ACTION_KEY, "e"),
        ("hold:w", ACTION_HOLD, "w"),
        ("release:w", ACTION_RELEASE, "w"),
        ("click:left", ACTION_CLICK, "left"),
        ("LSHIFT", ACTION_KEY, "lshift"),
    ):
        s = parse_script({"rules": [{"actions": [text]}]})
        ok = check_equal(s.issues, (), f"简写 {text!r} 无 issue")
        if ok and s.rules:
            action = s.rules[0].actions[0]
            check_equal(action.type, expected_type, f"简写 {text!r} 类型")
            check_equal(action.key, expected_key, f"简写 {text!r} 键名")

    # 数组条件 = all；多键对象 = all
    s = parse_script(
        {"rules": [{"when": [{"hp_below": 0.5}, "always"], "actions": ["e"]}]}
    )
    check_equal(s.issues, (), "数组条件无 issue")
    check(
        s.rules and s.rules[0].condition.describe().startswith("all("),
        "数组条件解析为 all",
    )
    s = parse_script(
        {"rules": [{"when": {"hp_below": 0.5, "enemy_visible": True}, "actions": ["e"]}]}
    )
    check(
        s.rules and s.rules[0].condition.describe().startswith("all("),
        "多键条件对象解析为 all",
    )

    # 缺条件视为 always
    s = parse_script({"rules": [{"actions": ["e"]}]})
    check_equal(
        s.rules[0].condition.describe() if s.rules else None,
        "always",
        "缺条件默认 always",
    )

    print("[2] 脚本解析：非法输入必须被拒绝且记录原因")

    bad_cases = [
        (None, "空脚本"),
        ("not json at all {", "非法 JSON 字符串"),
        (123, "根节点是数字"),
        ([], "根节点是数组"),
        ({"rules": "nope"}, "rules 不是数组"),
        ({"rules": [{"actions": []}]}, "空动作列表"),
        ({"rules": [{"actions": ["zzz"]}]}, "未知按键"),
        ({"rules": [{"actions": ["click:middle_wrong"]}]}, "非法鼠标键"),
        ({"rules": [{"actions": ["wait:abc"]}]}, "wait 时长非法"),
        ({"rules": [{"actions": ["wait:-1"]}]}, "wait 负数"),
        ({"rules": [{"actions": [{"type": "nope", "key": "e"}]}]}, "未知动作类型"),
        ({"rules": [{"actions": [{"type": "key"}]}]}, "key 动作缺 key"),
        ({"rules": [{"actions": [123]}]}, "动作是数字"),
        ({"rules": [{"when": "nonsense", "actions": ["e"]}]}, "未知条件字符串"),
        ({"rules": [{"when": {"bogus_key": 1}, "actions": ["e"]}]}, "未知条件键"),
        ({"rules": [{"when": {"slot": 9}, "actions": ["e"]}]}, "槽位越界"),
        ({"rules": [{"when": {"every": 0}, "actions": ["e"]}]}, "every 非正数"),
        ({"rules": [{"when": {"all": []}, "actions": ["e"]}]}, "all 空数组"),
        ({"rules": [{"when": {"enemy_count_at_least": -3}, "actions": ["e"]}]}, "负数量"),
        ({"rules": ["not an object"]}, "规则不是对象"),
        ({}, "既无规则也无兜底"),
    ]
    for raw, label in bad_cases:
        s = parse_script(raw)
        check(len(s.issues) > 0, f"非法输入被记录: {label}")
        # 关键：非法输入绝不能产出"看起来能跑但实际是空壳"的规则。
        # 注意不能只写 all(...)——空 rules 会让它恒真，那是自证式断言。
        for rule in s.rules:
            check(
                len(rule.actions) > 0,
                f"非法输入未产出空动作规则: {label}/{rule.name}",
            )
            check(
                rule.condition is not None,
                f"非法输入未产出无条件规则: {label}/{rule.name}",
            )

    # 明确验证"被拒绝"的语义：这些用例必须一条规则都不产出
    must_be_empty = [
        ({"rules": [{"actions": ["zzz"]}]}, "未知按键"),
        ({"rules": [{"actions": []}]}, "空动作列表"),
        ({"rules": [{"when": {"bogus_key": 1}, "actions": ["e"]}]}, "未知条件键"),
        ({"rules": [{"when": {"slot": 9}, "actions": ["e"]}]}, "槽位越界"),
        ({"rules": ["not an object"]}, "规则不是对象"),
    ]
    for raw, label in must_be_empty:
        s = parse_script(raw)
        check_equal(len(s.rules), 0, f"非法规则被完全丢弃: {label}")

    # 越界值被钳制而非崩溃
    s = parse_script(
        {"rules": [{"actions": [{"type": "key", "key": "e", "duration": 999}]}]}
    )
    check(
        s.rules and s.rules[0].actions[0].duration <= 5.0,
        "超长 duration 被钳制",
    )
    s = parse_script({"rules": [{"actions": ["e"], "cooldown": -5}]})
    check(s.rules and s.rules[0].cooldown == 0.0, "负冷却被钳制到 0")

    # 数量上限
    many_actions = ["e"] * (MAX_ACTIONS_PER_RULE + 10)
    s = parse_script({"rules": [{"actions": many_actions}]})
    check(
        s.rules and len(s.rules[0].actions) == MAX_ACTIONS_PER_RULE,
        "动作数被截断到上限",
    )
    check(any("上限" in i for i in s.issues), "动作截断有 issue")

    s = parse_script({"rules": [{"actions": ["e"]}] * (MAX_RULES + 5)})
    check(len(s.rules) <= MAX_RULES, "规则数被截断到上限")

    # 百分数写法
    s = parse_script({"rules": [{"when": {"hp_below": 50}, "actions": ["e"]}]})
    check(
        s.rules and abs(s.rules[0].condition.threshold - 0.5) < 1e-9,
        "百分数 50 被转成 0.5",
    )

    # JSON 字符串输入
    s = parse_script('{"rules":[{"actions":["e"]}]}')
    check_equal(s.issues, (), "JSON 字符串输入可解析")
    check_equal(len(s.rules), 1, "JSON 字符串输入规则数")

    print(f"     完成，累计断言 {_CHECKS} 项")


# ----------------------------------------------------------------------
# 第 3 组：条件求值
# ----------------------------------------------------------------------


def group_conditions():
    from agent.custom.action.Combat.script.conditions import (
        CombatState,
        parse_condition,
    )

    print("[3] 条件求值")

    def cond(raw):
        issues: list[str] = []
        parsed = parse_condition(raw, issues, "test")
        check_equal(issues, [], f"条件 {raw!r} 解析无 issue")
        return parsed

    # 血量：识别不到时必须为假（不能误触发大招）
    c = cond({"hp_below": 0.5})
    check(c.evaluate(CombatState(self_hp=0.3)), "hp_below 命中")
    check(not c.evaluate(CombatState(self_hp=0.7)), "hp_below 不命中")
    check(not c.evaluate(CombatState(self_hp=None)), "hp 未知时 hp_below 为假")
    check(not c.evaluate(CombatState(self_hp=0.5)), "hp_below 边界不含等于")

    c = cond({"hp_above": 0.5})
    check(c.evaluate(CombatState(self_hp=0.7)), "hp_above 命中")
    check(not c.evaluate(CombatState(self_hp=None)), "hp 未知时 hp_above 为假")

    c = cond({"boss_hp_below": 0.2})
    check(c.evaluate(CombatState(boss_hp=0.1)), "boss_hp_below 命中")
    check(
        not c.evaluate(CombatState(self_hp=0.1)),
        "boss_hp 未知时不受自身血量影响",
    )

    # 敌情
    c = cond({"enemy_visible": True})
    check(c.evaluate(CombatState(enemy_visible=True)), "enemy_visible 命中")
    check(not c.evaluate(CombatState(enemy_visible=False)), "enemy_visible 不命中")
    c = cond({"enemy_visible": False})
    check(c.evaluate(CombatState(enemy_visible=False)), "enemy_visible=false 命中")

    c = cond({"enemy_count_at_least": 3})
    check(c.evaluate(CombatState(enemy_count=3)), "count>=3 边界含等于")
    check(not c.evaluate(CombatState(enemy_count=2)), "count>=3 不命中")
    c = cond({"enemy_count_at_most": 1})
    check(c.evaluate(CombatState(enemy_count=1)), "count<=1 边界含等于")

    # 时间
    c = cond({"elapsed_above": 3.0})
    check(c.evaluate(CombatState(elapsed=3.5)), "elapsed_above 命中")
    check(not c.evaluate(CombatState(elapsed=2.0)), "elapsed_above 不命中")
    c = cond({"elapsed_below": 3.0})
    check(c.evaluate(CombatState(elapsed=1.0)), "elapsed_below 命中")

    # 角色身份
    c = cond({"character": "Mint"})
    check(c.evaluate(CombatState(character="mint")), "character 大小写无关")
    check(not c.evaluate(CombatState(character="zero")), "character 不匹配")
    check(not c.evaluate(CombatState(character=None)), "character 未知时为假")
    c = cond({"character_in": ["Mint", "Zero"]})
    check(c.evaluate(CombatState(character="Zero")), "character_in 命中")

    c = cond({"slot": 2})
    check(c.evaluate(CombatState(slot=2)), "slot 命中")
    check(not c.evaluate(CombatState(slot=-1)), "slot 未知时为假")

    c = cond({"in_team": True})
    check(c.evaluate(CombatState(in_team=True)), "in_team 命中")

    # 布尔字面量
    check(cond(True).evaluate(CombatState()), "字面量 true 恒真")
    check(not cond(False).evaluate(CombatState()), "字面量 false 恒假")
    check(cond("always").evaluate(CombatState()), "always")
    check(not cond("never").evaluate(CombatState()), "never")

    # 组合逻辑
    c = cond({"all": [{"hp_below": 0.5}, {"enemy_visible": True}]})
    check(
        c.evaluate(CombatState(self_hp=0.3, enemy_visible=True)), "all 全真"
    )
    check(
        not c.evaluate(CombatState(self_hp=0.3, enemy_visible=False)),
        "all 一假即假",
    )
    c = cond({"any": [{"hp_below": 0.2}, {"enemy_count_at_least": 5}]})
    check(c.evaluate(CombatState(self_hp=0.1, enemy_count=0)), "any 一真即真")
    check(
        not c.evaluate(CombatState(self_hp=0.9, enemy_count=1)), "any 全假为假"
    )
    c = cond({"not": {"enemy_visible": True}})
    check(c.evaluate(CombatState(enemy_visible=False)), "not 取反")

    # 嵌套
    c = cond(
        {
            "all": [
                {"any": [{"hp_below": 0.3}, {"boss_hp_below": 0.2}]},
                {"not": {"enemy_count_at_least": 5}},
            ]
        }
    )
    check(
        c.evaluate(CombatState(self_hp=0.2, enemy_count=1)), "嵌套条件命中"
    )
    check(
        not c.evaluate(CombatState(self_hp=0.2, enemy_count=9)),
        "嵌套条件被 not 否决",
    )

    # describe 必须可读（日志与诊断依赖它）
    for raw in (
        {"hp_below": 0.5},
        {"all": [{"hp_below": 0.5}, "always"]},
        {"not": {"enemy_visible": True}},
        {"every": 2.0},
        {"character": "mint"},
    ):
        text = cond(raw).describe()
        check(isinstance(text, str) and len(text) > 0, f"describe 非空: {raw}")

    print(f"     完成，累计断言 {_CHECKS} 项")


# ----------------------------------------------------------------------
# 第 4 组：决策引擎
# ----------------------------------------------------------------------


def group_engine():
    from agent.custom.action.Combat.script import (
        REASON_FALLBACK,
        REASON_IDLE,
        REASON_RULE,
        CombatEngine,
        CombatState,
        parse_script,
    )

    print("[4] 决策引擎：优先级、冷却、once、兜底")

    script = parse_script(
        {
            "rules": [
                {
                    "name": "ult",
                    "when": {"hp_below": 0.3},
                    "actions": ["q"],
                    "priority": 5,
                    "cooldown": 30,
                },
                {
                    "name": "skill",
                    "when": {"enemy_visible": True},
                    "actions": ["e"],
                    "priority": 20,
                    "cooldown": 5,
                },
                {"name": "poke", "actions": ["space"], "priority": 90},
            ],
            "fallback": ["click:left"],
        }
    )
    check_equal(script.issues, (), "引擎测试脚本无 issue")

    engine = CombatEngine(script)

    # 优先级：血量低时选 ult 而不是 skill
    state = CombatState(now=10.0, self_hp=0.2, enemy_visible=True)
    d = engine.decide(state)
    check_equal(d.reason, REASON_RULE, "选中规则")
    check_equal(d.rule_name, "ult", "按优先级选中 ult")

    # 记账后进入冷却，改选 skill
    engine.commit(d, 10.0)
    d = engine.decide(CombatState(now=11.0, self_hp=0.2, enemy_visible=True))
    check_equal(d.rule_name, "skill", "ult 冷却中改选 skill")

    # skill 也冷却后落到 poke
    engine.commit(d, 11.0)
    d = engine.decide(CombatState(now=12.0, self_hp=0.2, enemy_visible=True))
    check_equal(d.rule_name, "poke", "两者冷却后落到 poke")

    # 冷却到期后恢复
    d = engine.decide(CombatState(now=45.0, self_hp=0.2, enemy_visible=True))
    check_equal(d.rule_name, "ult", "冷却到期后恢复 ult")

    # 条件都不满足 -> 兜底
    only_conditional = parse_script(
        {
            "rules": [{"name": "r", "when": {"hp_below": 0.1}, "actions": ["e"]}],
            "fallback": ["space"],
        }
    )
    engine2 = CombatEngine(only_conditional)
    d = engine2.decide(CombatState(now=1.0, self_hp=0.9))
    check_equal(d.reason, REASON_FALLBACK, "无可用规则时走兜底")

    # 无兜底 -> idle
    no_fallback = parse_script(
        {"rules": [{"name": "r", "when": {"hp_below": 0.1}, "actions": ["e"]}]}
    )
    engine3 = CombatEngine(no_fallback)
    d = engine3.decide(CombatState(now=1.0, self_hp=0.9))
    check_equal(d.reason, REASON_IDLE, "无兜底时空闲")

    # 兜底间隔
    throttled = parse_script(
        {
            "rules": [{"name": "r", "when": "never", "actions": ["e"]}],
            "fallback": ["space"],
            "fallback_interval": 2.0,
        }
    )
    engine4 = CombatEngine(throttled)
    d = engine4.decide(CombatState(now=100.0))
    check_equal(d.reason, REASON_FALLBACK, "首次兜底可执行")
    engine4.commit(d, 100.0)
    d = engine4.decide(CombatState(now=101.0))
    check_equal(d.reason, REASON_IDLE, "兜底间隔内不重复")
    d = engine4.decide(CombatState(now=102.5))
    check_equal(d.reason, REASON_FALLBACK, "兜底间隔到期后恢复")

    # once：只触发一次
    once_script = parse_script(
        {"rules": [{"name": "opener", "actions": ["r"], "once": True}]}
    )
    engine5 = CombatEngine(once_script)
    d = engine5.decide(CombatState(now=1.0))
    check_equal(d.rule_name, "opener", "once 规则首次可用")
    engine5.commit(d, 1.0)
    d = engine5.decide(CombatState(now=2.0))
    check_equal(d.reason, REASON_IDLE, "once 规则不再触发")

    # commit 是显式的：不 commit 就不该进冷却
    engine6 = CombatEngine(
        parse_script({"rules": [{"name": "r", "actions": ["e"], "cooldown": 10}]})
    )
    d1 = engine6.decide(CombatState(now=1.0))
    d2 = engine6.decide(CombatState(now=1.0))
    check_equal(d2.rule_name, d1.rule_name, "未 commit 时规则仍可用")

    # every 条件：由账本驱动
    every_script = parse_script(
        {"rules": [{"name": "tick", "when": {"every": 3.0}, "actions": ["e"]}]}
    )
    engine7 = CombatEngine(every_script)
    d = engine7.decide(CombatState(now=100.0))
    check_equal(d.rule_name, "tick", "every 首次可用")
    engine7.commit(d, 100.0)
    d = engine7.decide(CombatState(now=101.0))
    check_equal(d.reason, REASON_IDLE, "every 间隔内不可用")
    d = engine7.decide(CombatState(now=104.0))
    check_equal(d.rule_name, "tick", "every 间隔到期后可用")

    # reset 清空状态
    engine7.reset()
    d = engine7.decide(CombatState(now=104.5))
    check_equal(d.rule_name, "tick", "reset 后 every 重新可用")

    # eligible_rules 与 explain
    eligible = engine.eligible_rules(
        CombatState(now=200.0, self_hp=0.2, enemy_visible=True)
    )
    check_equal(
        [r.name for r in eligible], ["ult", "skill", "poke"], "eligible 按优先级排序"
    )
    lines = engine.explain(CombatState(now=200.0, self_hp=0.9, enemy_visible=False))
    check_equal(len(lines), 3, "explain 覆盖所有规则")
    check(
        any("条件不满足" in line for line in lines), "explain 说明条件不满足"
    )

    # 优先级相同时保持声明顺序（稳定排序）
    same_prio = parse_script(
        {
            "rules": [
                {"name": "first", "actions": ["a"], "priority": 50},
                {"name": "second", "actions": ["s"], "priority": 50},
            ]
        }
    )
    engine8 = CombatEngine(same_prio)
    d = engine8.decide(CombatState(now=1.0))
    check_equal(d.rule_name, "first", "同优先级保持声明顺序")

    print(f"     完成，累计断言 {_CHECKS} 项")


# ----------------------------------------------------------------------
# 第 5 组：动作执行与按键释放保证
# ----------------------------------------------------------------------


class FakeKernel:
    """记录所有内核调用的假内核，用于验证"动作 -> 调用"的映射。"""

    def __init__(self, fail_on_release=()):
        self.calls: list[tuple] = []
        self._fail_on_release = set(fail_on_release)

    def sleep(self, timeout, allow_slow_poll=True, scaled=True):
        self.calls.append(("sleep", round(float(timeout), 4)))
        return True

    def send_key(self, key, down_time=0.02, interval=-1, after_sleep=0,
                 action_name=None):
        self.calls.append(("send_key", key, round(float(down_time or 0), 4)))
        return True

    def send_key_down(self, key, after_sleep=0):
        self.calls.append(("key_down", key))
        return True

    def send_key_up(self, key, after_sleep=0):
        if key in self._fail_on_release:
            self.calls.append(("key_up_failed", key))
            raise RuntimeError(f"simulated release failure for {key}")
        self.calls.append(("key_up", key))
        return True

    def click(self, x=-1, y=-1, name=None, interval=-1, key="left",
              down_time=0.01, after_sleep=0):
        self.calls.append(("click", key, round(float(down_time or 0), 4)))
        return True

    def mouse_down(self, key="left"):
        self.calls.append(("mouse_down", key))

    def mouse_up(self, key="left"):
        if key in self._fail_on_release:
            self.calls.append(("mouse_up_failed", key))
            raise RuntimeError(f"simulated mouse release failure for {key}")
        self.calls.append(("mouse_up", key))


def group_primitives():
    from agent.custom.action.Combat.script import ActionRunner, parse_script

    print("[5] 动作执行：调用映射与释放保证")

    script = parse_script(
        {
            "rules": [
                {
                    "name": "combo",
                    "actions": [
                        "e",
                        "wait:0.3",
                        {"type": "key", "key": "q", "duration": 0.2},
                        "hold:w",
                        "release:w",
                        "click:left",
                        {"type": "mouse_down", "key": "right"},
                        {"type": "mouse_up", "key": "right"},
                    ],
                }
            ]
        }
    )
    check_equal(script.issues, (), "动作执行脚本无 issue")

    kernel = FakeKernel()
    runner = ActionRunner(kernel)
    count = runner.run(script.rules[0].actions)
    check_equal(count, 8, "执行动作条数")
    check_equal(
        kernel.calls,
        [
            ("send_key", "e", 0.02),
            ("sleep", 0.3),
            ("send_key", "q", 0.2),
            ("key_down", "w"),
            ("key_up", "w"),
            ("click", "left", 0.01),
            ("mouse_down", "right"),
            ("mouse_up", "right"),
        ],
        "动作到内核调用的映射",
    )
    check_equal(runner.held_keys, set(), "配对的 hold/release 不残留")
    check_equal(runner.held_mouse, set(), "配对的鼠标按下不残留")

    # 只 hold 不 release -> release_all 必须补上
    kernel = FakeKernel()
    runner = ActionRunner(kernel)
    s = parse_script(
        {"rules": [{"actions": ["hold:w", "hold:lshift",
                                {"type": "mouse_down", "key": "left"}]}]}
    )
    runner.run(s.rules[0].actions)
    check_equal(runner.held_keys, {"w", "lshift"}, "记录按住的键")
    check_equal(runner.held_mouse, {"left"}, "记录按住的鼠标键")
    runner.release_all()
    check(("key_up", "w") in kernel.calls, "release_all 释放 w")
    check(("key_up", "lshift") in kernel.calls, "release_all 释放 lshift")
    check(("mouse_up", "left") in kernel.calls, "release_all 释放鼠标")
    check_equal(runner.held_keys, set(), "release_all 后无残留键")
    check_equal(runner.held_mouse, set(), "release_all 后无残留鼠标")

    # 关键：一个键释放失败不能连累其它键
    kernel = FakeKernel(fail_on_release={"w"})
    logs: list[str] = []
    runner = ActionRunner(kernel, log=logs.append)
    s = parse_script(
        {"rules": [{"actions": ["hold:w", "hold:a", "hold:d"]}]}
    )
    runner.run(s.rules[0].actions)
    runner.release_all()
    check(("key_up", "a") in kernel.calls, "w 释放失败后仍释放 a")
    check(("key_up", "d") in kernel.calls, "w 释放失败后仍释放 d")
    check_equal(runner.held_keys, set(), "失败后仍清空按住集合")
    check(any("释放按键 w 失败" in m for m in logs), "释放失败被记录")

    # 鼠标释放失败同理
    kernel = FakeKernel(fail_on_release={"left"})
    logs = []
    runner = ActionRunner(kernel, log=logs.append)
    s = parse_script(
        {
            "rules": [
                {
                    "actions": [
                        {"type": "mouse_down", "key": "left"},
                        {"type": "mouse_down", "key": "right"},
                    ]
                }
            ]
        }
    )
    runner.run(s.rules[0].actions)
    runner.release_all()
    check(("mouse_up", "right") in kernel.calls, "left 失败后仍释放 right")
    check(any("释放鼠标键 left 失败" in m for m in logs), "鼠标释放失败被记录")

    # release_all 幂等
    kernel = FakeKernel()
    runner = ActionRunner(kernel)
    runner.release_all()
    before = len(kernel.calls)
    runner.release_all()
    check_equal(len(kernel.calls), before, "空 runner 重复 release_all 无副作用")

    print(f"     完成，累计断言 {_CHECKS} 项")


# ----------------------------------------------------------------------
# 第 6 组：角色名录与队伍编成
# ----------------------------------------------------------------------


def group_identity_catalog():
    from agent.custom.action.Combat.identity import (
        CHARACTERS,
        available_portraits,
        display_name,
        parse_roster,
        portrait_template_list,
        resolve,
        resolve_identity,
    )

    print("[6] 角色名录与队伍编成")

    # 名录必须与已上线的 SyncCharacterAbilityCityAbility 保持一致，
    # 否则同一个角色在两处会有不同解释。这里直接读那份源码做交叉校验。
    sync_src = (
        REPO / "agent" / "custom" / "action" / "SyncCharacterAbilityCityAbility.py"
    ).read_text(encoding="utf-8")
    import ast

    sync_map = {}
    for node in ast.walk(ast.parse(sync_src)):
        if isinstance(node, ast.AnnAssign) and getattr(
            node.target, "id", ""
        ) == "_TEMPLATE_TO_NAME":
            for key, value in zip(node.value.keys, node.value.values):
                sync_map[key.value] = value.value
    check(len(sync_map) > 0, "成功读取 SyncCharacterAbility 的角色映射")
    for portrait, chinese in sync_map.items():
        entry = resolve(portrait)
        check(entry is not None, f"名录包含 {portrait}")
        if entry is not None:
            check_equal(
                entry.display, chinese, f"{portrait} 中文名与已上线实现一致"
            )
    check_equal(
        len(CHARACTERS), len(sync_map), "名录角色数与已上线实现一致"
    )

    # 立绘文件确实存在（has_portrait=True 的必须有文件）
    portrait_dir = (
        REPO / "assets" / "resource" / "base" / "image" / "Character_UI" / "Character_Pic"
    )
    for entry in available_portraits():
        check(
            (portrait_dir / entry.portrait).exists(),
            f"立绘文件存在: {entry.portrait}",
        )
    # 反向：目录里的文件都应在名录中
    for path in sorted(portrait_dir.glob("*.png")):
        check(
            resolve(path.name) is not None, f"目录中的立绘在名录内: {path.name}"
        )
    check_equal(
        len(portrait_template_list()),
        len(list(portrait_dir.glob("*.png"))),
        "模板列表长度与实际文件数一致",
    )

    # 多种写法都能解析到同一条目
    for alias in ("mint", "Mint", "MINT", " mint ", "薄荷", "Mint.png"):
        entry = resolve(alias)
        check(
            entry is not None and entry.key == "mint", f"别名解析: {alias!r}"
        )
    for bad in ("", "   ", None, "nonexistent", "Mint.jpg", 123):
        check(resolve(bad) is None, f"非法角色名返回 None: {bad!r}")

    check_equal(display_name("mint"), "薄荷", "display_name 中文")
    check_equal(display_name("unknown_x"), "unknown_x", "未知名原样返回")

    # --- 队伍编成 ---
    r = parse_roster({"1": "mint", "2": "skia", "3": "zero", "4": "hotori"})
    check_equal(r.issues, (), "合法 roster 无 issue")
    check(r.configured, "roster 已配置")
    check_equal(r.character_at(0), "mint", "槽位 0 = mint")
    check_equal(r.character_at(3), "hotori", "槽位 3 = hotori")
    check_equal(r.key_for("zero"), "3", "zero 的按键是 3")
    check_equal(r.slot_of("翳"), 1, "中文名查槽位")
    check_equal(r.key_for("nonexistent"), None, "未声明角色无按键")

    # 数组写法等价
    r2 = parse_roster(["mint", "skia", "zero", "hotori"])
    check_equal(r2.slots, r.slots, "数组写法与对象写法等价")

    # 部分声明
    r3 = parse_roster({"2": "mint"})
    check(r3.configured, "部分声明也算已配置")
    check_equal(r3.character_at(0), None, "未声明槽位为 None")
    check_equal(r3.character_at(1), "mint", "已声明槽位正确")

    # 未配置
    r4 = parse_roster(None)
    check(not r4.configured, "未配置 roster")
    check_equal(resolve_identity(r4, 0), None, "未配置时身份未知")

    # 非法输入
    bad_rosters = [
        ({"0": "mint"}, "槽位号 0 越界"),
        ({"5": "mint"}, "槽位号 5 越界"),
        ({"x": "mint"}, "槽位号非数字"),
        ({"1": "nonexistent"}, "未知角色名"),
        ("not a dict", "类型错误"),
        (["mint", "mint"], "同一角色重复"),
        (["a", "b", "c", "d", "e"], "数组超长"),
    ]
    for raw, label in bad_rosters:
        parsed = parse_roster(raw)
        check(len(parsed.issues) > 0, f"非法 roster 记录 issue: {label}")

    # 重复角色必须被指出（这是真实易犯的配置错误）
    dup = parse_roster({"1": "mint", "3": "mint"})
    check(
        any("同时出现在槽位" in i for i in dup.issues), "重复角色被指出"
    )

    # resolve_identity 依赖槽位有效
    check_equal(resolve_identity(r, 2), "zero", "槽位 2 解析为 zero")
    check_equal(resolve_identity(r, -1), None, "槽位 -1 身份未知")
    check_equal(resolve_identity(r, 99), None, "越界槽位身份未知")

    print(f"     完成，累计断言 {_CHECKS} 项")


# ----------------------------------------------------------------------
# 第 7 组：身份识别的能力边界
# ----------------------------------------------------------------------


def group_identity_recognition():
    from agent.custom.action.Combat.identity import (
        PortraitIdentifier,
        SlotIdentifier,
        resolve_identity,
    )
    from agent.custom.action.Combat.script import (
        CombatEngine,
        CombatState,
        parse_script,
    )

    print("[7] 身份识别：槽位可靠、头像缺失时必须报未知")

    # --- 槽位识别：复用内核已验证逻辑，用第 11 组的合成图 ---
    identifier = SlotIdentifier()
    frames = build_equivalence_corpus()
    for index in range(4):
        image = frames[f"slot{index}_white"]
        check_equal(
            identifier.current_slot(image), index, f"槽位识别: slot{index}"
        )
        check(
            identifier.is_slot_active(image, index),
            f"is_slot_active 命中 slot{index}",
        )
        for other in range(4):
            if other != index:
                check(
                    not identifier.is_slot_active(image, other),
                    f"slot{index} 图不应命中 slot{other}",
                )

    check_equal(
        identifier.current_slot(frames["team_ui"]), -1, "无高亮时返回 -1"
    )
    check_equal(identifier.current_slot(None), -1, "空图返回 -1")
    check_equal(
        identifier.current_slot(frames["slot0_and_slot2"]),
        -1,
        "两槽同亮时拒绝判断",
    )
    check_equal(len(identifier.slot_scores(None)), 4, "空图打分长度仍为 4")

    # --- 头像识别：模板缺失时必须明确不可用 ---
    portrait = PortraitIdentifier()
    check(not portrait.available, "未配置目录时头像识别不可用")
    reason = portrait.missing_reason()
    check(
        isinstance(reason, str) and "character" in reason,
        "不可用原因说明了对 character 条件的影响",
    )
    check_equal(portrait.known_characters, (), "无模板时已知角色为空")

    # 不可用时识别必须返回未知，绝不猜测
    for index in range(4):
        result = portrait.identify(frames[f"slot{index}_white"], index)
        check_equal(
            result.character, None, f"模板缺失时 slot{index} 身份为 None"
        )
        check_equal(result.confidence, 0.0, "模板缺失时置信度为 0")
        check_equal(result.display, "未知", "显示名为未知")

    # 指向不存在的目录
    missing_dir = PortraitIdentifier(image_dir=REPO / "no_such_dir_xyz")
    check(not missing_dir.available, "目录不存在时不可用")
    check(
        "不存在" in (missing_dir.missing_reason() or ""),
        "目录不存在的原因明确",
    )

    # 指向存在但无模板的目录
    empty_dir = PortraitIdentifier(image_dir=REPO / "tools" / "fixtures")
    check(not empty_dir.available, "空目录时不可用")

    # --- 关键：身份未知时 character 条件必须恒假，脚本退回兜底 ---
    script = parse_script(
        {
            "rules": [
                {
                    "name": "mint_only",
                    "when": {"character": "mint"},
                    "actions": ["e"],
                }
            ],
            "fallback": ["space"],
        }
    )
    check(
        any("roster" in i for i in script.issues),
        "用 character 但无 roster 时提醒用户",
    )
    engine = CombatEngine(script)
    decision = engine.decide(CombatState(now=1.0, character=None))
    check_equal(
        decision.reason,
        "fallback",
        "身份未知时退回兜底而非误触发 character 规则",
    )

    # 声明 roster 后，character 条件可以基于槽位求值
    script2 = parse_script(
        {
            "rules": [
                {
                    "name": "mint_only",
                    "when": {"character": "mint"},
                    "actions": ["e"],
                }
            ],
            "fallback": ["space"],
            "roster": {"1": "mint", "2": "zero"},
        }
    )
    check_equal(script2.issues, (), "声明 roster 后无 issue")
    engine2 = CombatEngine(script2)
    roster = script2.roster

    # 槽位 0 -> mint -> 规则命中
    identity = resolve_identity(roster, 0)
    check_equal(identity, "mint", "槽位 0 解析为 mint")
    decision = engine2.decide(CombatState(now=1.0, character=identity, slot=0))
    check_equal(decision.rule_name, "mint_only", "身份为 mint 时规则命中")

    # 槽位 1 -> zero -> 规则不命中，走兜底
    identity = resolve_identity(roster, 1)
    check_equal(identity, "zero", "槽位 1 解析为 zero")
    decision = engine2.decide(CombatState(now=2.0, character=identity, slot=1))
    check_equal(decision.reason, "fallback", "身份为 zero 时不触发 mint 规则")

    # 槽位识别失败（-1）-> 身份未知 -> 兜底
    identity = resolve_identity(roster, -1)
    check_equal(identity, None, "槽位识别失败时身份未知")
    decision = engine2.decide(CombatState(now=3.0, character=identity, slot=-1))
    check_equal(
        decision.reason, "fallback", "槽位识别失败时退回兜底"
    )

    # 端到端：合成图 -> 槽位 -> 身份 -> 决策
    for slot_index, expected_rule in ((0, "mint_only"), (1, None)):
        image = frames[f"slot{slot_index}_white"]
        detected = identifier.current_slot(image)
        check_equal(detected, slot_index, f"端到端槽位识别 slot{slot_index}")
        who = resolve_identity(roster, detected)
        decision = engine2.decide(
            CombatState(now=10.0 + slot_index, character=who, slot=detected)
        )
        if expected_rule:
            check_equal(
                decision.rule_name, expected_rule, f"端到端决策 slot{slot_index}"
            )
        else:
            check_equal(
                decision.reason, "fallback", f"端到端兜底 slot{slot_index}"
            )

    print(f"     完成，累计断言 {_CHECKS} 项")


# ----------------------------------------------------------------------
# 第 8-10 组：感知、会话闭环、入口
# ----------------------------------------------------------------------


class ScriptedKernel:
    """假内核：喂入预设帧序列，记录所有输入调用。

    用它可以在没有游戏的情况下跑完整会话循环，验证 tick 时序、
    脱战判定、异常安全与按键释放。
    """

    def __init__(self, frames, recognize=None, clock=None, stop_after=None,
                 raise_at_tick=None, raise_type=None):
        self._frames = list(frames)
        self._frame_index = 0
        self.calls: list[tuple] = []
        self.logs: list[str] = []
        self._recognize = recognize or (lambda node, image: False)
        self._clock_value = 1000.0
        self._external_clock = clock
        self._stop_after = stop_after
        self._raise_at_tick = raise_at_tick
        self._raise_type = raise_type
        self._screencaps = 0
        self.ctx = self  # perception 通过 kernel.ctx.run_recognition 调用
        self.ah = self

    # --- 时钟 ---
    def clock(self):
        if self._external_clock is not None:
            return self._external_clock()
        return self._clock_value

    def advance(self, delta):
        self._clock_value += float(delta)

    # --- ActionHelper 兼容面 ---
    def raise_if_stopped(self):
        if self._stop_after is not None and self._screencaps > self._stop_after:
            from agent.custom.action.Combat.kernel.errors import (
                TaskerStoppedException,
            )

            raise TaskerStoppedException("scripted stop")

    def is_stopping(self):
        return self._stop_after is not None and self._screencaps > self._stop_after

    def run_task(self, name, pipeline_override=None):
        self.calls.append(("run_task", name))
        return None

    # --- 识别 ---
    def run_recognition(self, node, image):
        hit = self._recognize(node, image)
        self.calls.append(("recognize", node, bool(hit)))

        class _R:
            def __init__(self, ok):
                self.hit = ok

        return _R(hit)

    # --- 截图 ---
    def screencap(self):
        self._screencaps += 1
        if self._raise_at_tick is not None and self._screencaps == self._raise_at_tick:
            raise self._raise_type("scripted failure")
        if not self._frames:
            return None
        if self._frame_index < len(self._frames) - 1:
            frame = self._frames[self._frame_index]
            self._frame_index += 1
            return frame
        return self._frames[-1]

    # --- 输入 ---
    def sleep(self, timeout, allow_slow_poll=True, scaled=True):
        self.calls.append(("sleep", round(float(timeout), 4)))
        self.advance(max(float(timeout), 0.001))
        return True

    def send_key(self, key, down_time=0.02, interval=-1, after_sleep=0,
                 action_name=None):
        self.calls.append(("send_key", key))
        return True

    def send_key_down(self, key, after_sleep=0):
        self.calls.append(("key_down", key))
        return True

    def send_key_up(self, key, after_sleep=0):
        self.calls.append(("key_up", key))
        return True

    def click(self, x=-1, y=-1, name=None, interval=-1, key="left",
              down_time=0.01, after_sleep=0):
        self.calls.append(("click", key))
        return True

    def mouse_down(self, key="left"):
        self.calls.append(("mouse_down", key))

    def mouse_up(self, key="left"):
        self.calls.append(("mouse_up", key))

    def release_held_keys(self):
        self.calls.append(("release_held_keys",))

    # --- 日志 ---
    def log_info(self, *a):
        self.logs.append("INFO " + " ".join(str(x) for x in a))

    def log_warning(self, *a):
        self.logs.append("WARN " + " ".join(str(x) for x in a))

    def log_error(self, *a):
        self.logs.append("ERROR " + " ".join(str(x) for x in a))


def group_perception():
    from agent.custom.action.Combat.identity import parse_roster
    from agent.custom.action.Combat.perception import ENEMY_NODE, Perception

    print("[8] 感知：状态构建与降级")

    frames = build_equivalence_corpus()
    roster = parse_roster({"1": "mint", "2": "zero", "3": "skia", "4": "hotori"})

    # 有敌人
    kernel = ScriptedKernel([], recognize=lambda node, img: node == ENEMY_NODE)
    p = Perception(kernel, roster=roster)
    state = p.observe(frames["slot0_white"], now=100.0, elapsed=1.0, tick=0)
    check(state.enemy_visible, "识别到敌人")
    check_equal(state.enemy_count, 1, "敌人数量")
    check_equal(state.slot, 0, "槽位识别")
    check_equal(state.character, "mint", "身份由 roster 解析")
    check(state.in_team, "在队伍界面")
    check(not state.black_screen, "非黑屏")
    check_equal(state.self_hp, None, "血量未实现，保持 None")
    check_equal(state.boss_hp, None, "boss 血量未实现，保持 None")

    # 无敌人
    kernel = ScriptedKernel([], recognize=lambda node, img: False)
    p = Perception(kernel, roster=roster)
    state = p.observe(frames["slot1_white"], now=100.0, elapsed=1.0, tick=0)
    check(not state.enemy_visible, "无敌人")
    check_equal(state.character, "zero", "槽位 1 -> zero")

    # 截图失败 -> 全部未知，但不崩
    state = p.observe(None, now=100.0, elapsed=1.0, tick=0)
    check_equal(state.slot, -1, "截图失败时槽位未知")
    check_equal(state.character, None, "截图失败时身份未知")
    check(not state.enemy_visible, "截图失败时不报敌人")

    # 黑屏：不做敌人检测（画面无内容）
    kernel = ScriptedKernel([], recognize=lambda node, img: True)
    p = Perception(kernel, roster=roster)
    state = p.observe(frames["pure_black"], now=100.0, elapsed=1.0, tick=0)
    check(state.black_screen, "识别到黑屏")
    check(not state.enemy_visible, "黑屏时不报敌人")
    check_equal(state.slot, -1, "黑屏时不判槽位")
    check(
        not any(c[0] == "recognize" for c in kernel.calls),
        "黑屏时根本不调用敌人识别",
    )

    # 节流：间隔内不重复识别
    calls = {"n": 0}

    def counting(node, img):
        calls["n"] += 1
        return True

    kernel = ScriptedKernel([], recognize=counting)
    p = Perception(kernel, roster=roster)
    p.observe(frames["slot0_white"], now=100.0, elapsed=0.0, tick=0)
    check_equal(calls["n"], 1, "首帧执行识别")
    p.observe(frames["slot0_white"], now=100.05, elapsed=0.05, tick=1)
    check_equal(calls["n"], 1, "节流间隔内不重复识别")
    p.observe(frames["slot0_white"], now=100.5, elapsed=0.5, tick=2)
    check_equal(calls["n"], 2, "超过间隔后重新识别")

    # 识别抛异常 -> 沿用上次结果，不崩
    def flaky(node, img):
        raise RuntimeError("recognition exploded")

    kernel = ScriptedKernel([], recognize=flaky)
    p = Perception(kernel, roster=roster)
    state = p.observe(frames["slot0_white"], now=100.0, elapsed=0.0, tick=0)
    check(not state.enemy_visible, "识别异常时默认无敌人")
    check(any("敌人检测失败" in m for m in kernel.logs), "识别异常被记录")

    # 无 roster -> 身份未知但槽位仍可用
    kernel = ScriptedKernel([], recognize=lambda n, i: False)
    p = Perception(kernel, roster=None)
    state = p.observe(frames["slot2_white"], now=1.0, elapsed=0.0, tick=0)
    check_equal(state.slot, 2, "无 roster 时槽位仍识别")
    check_equal(state.character, None, "无 roster 时身份未知")

    # reset 清空节流缓存
    calls["n"] = 0
    kernel = ScriptedKernel([], recognize=counting)
    p = Perception(kernel, roster=roster)
    p.observe(frames["slot0_white"], now=100.0, elapsed=0.0, tick=0)
    p.reset()
    p.observe(frames["slot0_white"], now=100.01, elapsed=0.01, tick=1)
    check_equal(calls["n"], 2, "reset 后重新识别")

    # --- ESC 菜单检测 ---
    from agent.custom.action.Combat.perception import MENU_NODE

    kernel = ScriptedKernel([], recognize=lambda node, img: node == MENU_NODE)
    p = Perception(kernel, roster=roster)
    state = p.observe(frames["slot0_white"], now=200.0, elapsed=0.0, tick=0)
    check(state.menu_open, "识别到 ESC 菜单打开")
    # 菜单打开时不应再做敌人检测与槽位判定（画面被菜单遮挡）
    check(not state.enemy_visible, "菜单打开时不报敌人")
    check_equal(state.slot, -1, "菜单打开时不判槽位")
    check_equal(state.character, None, "菜单打开时身份未知")
    check(
        not any(c[0] == "recognize" and c[1] == ENEMY_NODE for c in kernel.calls),
        "菜单打开时不调用敌人识别",
    )

    # 菜单未打开时一切正常
    kernel = ScriptedKernel([], recognize=lambda node, img: node == ENEMY_NODE)
    p = Perception(kernel, roster=roster)
    state = p.observe(frames["slot0_white"], now=200.0, elapsed=0.0, tick=0)
    check(not state.menu_open, "菜单未打开")
    check(state.enemy_visible, "菜单未打开时正常检测敌人")
    check_equal(state.slot, 0, "菜单未打开时正常判槽位")

    # 黑屏时连菜单都不检测
    kernel = ScriptedKernel([], recognize=lambda node, img: True)
    p = Perception(kernel, roster=roster)
    state = p.observe(frames["pure_black"], now=200.0, elapsed=0.0, tick=0)
    check(not state.menu_open, "黑屏时不报菜单")
    check(
        not any(c[0] == "recognize" for c in kernel.calls),
        "黑屏时不做任何节点识别",
    )

    # 菜单检测异常 -> 视为未打开（不能因识别抖动中断战斗）
    def menu_explodes(node, img):
        if node == MENU_NODE:
            raise RuntimeError("menu recognition failed")
        return False

    kernel = ScriptedKernel([], recognize=menu_explodes)
    p = Perception(kernel, roster=roster)
    state = p.observe(frames["slot0_white"], now=200.0, elapsed=0.0, tick=0)
    check(not state.menu_open, "菜单识别异常时视为未打开")
    check(any("菜单检测失败" in m for m in kernel.logs), "菜单识别异常被记录")

    # 菜单检测节流
    menu_calls = {"n": 0}

    def counting_menu(node, img):
        if node == MENU_NODE:
            menu_calls["n"] += 1
        return False

    kernel = ScriptedKernel([], recognize=counting_menu)
    p = Perception(kernel, roster=roster)
    p.observe(frames["slot0_white"], now=300.0, elapsed=0.0, tick=0)
    check_equal(menu_calls["n"], 1, "菜单检测首帧执行")
    p.observe(frames["slot0_white"], now=300.2, elapsed=0.2, tick=1)
    check_equal(menu_calls["n"], 1, "菜单检测节流间隔内不重复")
    p.observe(frames["slot0_white"], now=301.0, elapsed=1.0, tick=2)
    check_equal(menu_calls["n"], 2, "菜单检测超过间隔后重新执行")

    # 可关闭菜单检测
    from agent.custom.action.Combat.perception import PerceptionConfig

    kernel = ScriptedKernel([], recognize=lambda node, img: node == MENU_NODE)
    p = Perception(
        kernel, roster=roster, config=PerceptionConfig(detect_menu=False)
    )
    state = p.observe(frames["slot0_white"], now=400.0, elapsed=0.0, tick=0)
    check(not state.menu_open, "关闭菜单检测后恒为未打开")

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_session_loop():
    from agent.custom.action.Combat.perception import ENEMY_NODE, Perception
    from agent.custom.action.Combat.runtime import (
        REASON_ABORTED,
        REASON_ERROR,
        REASON_MAX_TICKS,
        REASON_NOT_IN_COMBAT,
        REASON_STOPPED,
        REASON_TIMEOUT,
        CombatSession,
        SessionConfig,
    )
    from agent.custom.action.Combat.script import parse_script

    print("[9] 会话闭环：tick 时序、脱战、异常安全、按键释放")

    frames = build_equivalence_corpus()
    in_combat = frames["slot0_white"]

    def make_session(script_raw, kernel, **cfg):
        script = parse_script(script_raw)
        check_equal(script.issues, (), f"会话脚本无 issue: {script.name}")
        config = SessionConfig(**cfg)
        perception = Perception(
            kernel, roster=getattr(script, "roster", None)
        )
        session = CombatSession(
            kernel, script, config=config, perception=perception,
            clock=kernel.clock,
        )
        return session

    # --- 基本闭环：规则按冷却触发，兜底填空隙 ---
    kernel = ScriptedKernel(
        [in_combat], recognize=lambda node, img: node == ENEMY_NODE
    )
    session = make_session(
        {
            "name": "loop",
            "rules": [
                {
                    "name": "skill",
                    "when": {"enemy_visible": True},
                    "actions": ["e"],
                    "cooldown": 0.5,
                }
            ],
            "fallback": ["click:left"],
        },
        kernel,
        duration=2.0,
        tick_interval=0.1,
    )
    result = session.run()
    check(result.success, "正常结束 success=True")
    check_equal(result.reason, REASON_TIMEOUT, "到时结束")
    check(result.ticks > 0, "跑了若干 tick")
    check(result.rule_fires.get("skill", 0) > 0, "规则被触发")
    check(result.fallback_fires > 0, "兜底被触发（规则冷却期间）")
    # 冷却生效：2 秒 / 0.5s 冷却 => 至多 5 次
    check(
        result.rule_fires.get("skill", 0) <= 5,
        f"冷却限制了触发次数（实际 {result.rule_fires.get('skill')}）",
    )
    check(("send_key", "e") in kernel.calls, "实际发出了 E 键")
    check(("click", "left") in kernel.calls, "实际发出了左键")

    # --- commit 在执行后：验证顺序 ---
    order = [c for c in kernel.calls if c[0] in ("send_key", "sleep")]
    check(len(order) > 0, "调用序列非空")

    # --- 脱战判定：离开队伍界面超过宽限期 ---
    kernel = ScriptedKernel(
        [in_combat, in_combat, frames["no_team_ui"]],
        recognize=lambda n, i: False,
    )
    session = make_session(
        {"rules": [{"name": "r", "actions": ["e"]}]},
        kernel,
        duration=30.0,
        tick_interval=0.5,
        leave_grace=1.0,
    )
    result = session.run()
    check_equal(result.reason, REASON_NOT_IN_COMBAT, "脱战结束")
    check(result.success, "脱战属于正常结束")
    check(result.elapsed < 30.0, "脱战后提前退出，未等到超时")

    # --- 黑屏不算脱战 ---
    kernel = ScriptedKernel(
        [in_combat, frames["pure_black"], frames["pure_black"],
         frames["pure_black"], in_combat],
        recognize=lambda n, i: False,
    )
    session = make_session(
        {"rules": [{"name": "r", "actions": ["e"]}]},
        kernel,
        duration=1.5,
        tick_interval=0.2,
        leave_grace=0.3,
    )
    result = session.run()
    check_equal(
        result.reason, REASON_TIMEOUT, "黑屏期间不误判脱战"
    )

    # --- tasker 停止：必须释放按键且 success=False ---
    kernel = ScriptedKernel(
        [in_combat], recognize=lambda n, i: False, stop_after=3
    )
    session = make_session(
        {"rules": [{"name": "hold_w", "actions": ["hold:w"]}]},
        kernel,
        duration=10.0,
        tick_interval=0.1,
    )
    result = session.run()
    check_equal(result.reason, REASON_STOPPED, "被 tasker 停止")
    check(not result.success, "被停止时 success=False")
    check(("key_up", "w") in kernel.calls, "停止时释放了按住的 W")
    check(
        ("release_held_keys",) in kernel.calls, "停止时调用内核释放"
    )

    # --- 截图抛异常：走 error 分支但仍释放按键 ---
    kernel = ScriptedKernel(
        [in_combat], recognize=lambda n, i: False,
        raise_at_tick=3, raise_type=RuntimeError,
    )
    session = make_session(
        {"rules": [{"name": "hold_w", "actions": ["hold:w"]}]},
        kernel,
        duration=10.0,
        tick_interval=0.1,
    )
    result = session.run()
    check_equal(result.reason, REASON_ERROR, "截图异常 -> error")
    check(not result.success, "异常时 success=False")
    check(("key_up", "w") in kernel.calls, "异常时仍释放按住的 W")
    check(any("战斗异常" in m for m in kernel.logs), "异常被记录")

    # --- AbortException 走 aborted 分支 ---
    from agent.custom.action.Combat.kernel.errors import AbortException

    kernel = ScriptedKernel(
        [in_combat], recognize=lambda n, i: False,
        raise_at_tick=2, raise_type=AbortException,
    )
    session = make_session(
        {"rules": [{"name": "r", "actions": ["e"]}]},
        kernel, duration=5.0, tick_interval=0.1,
    )
    result = session.run()
    check_equal(result.reason, REASON_ABORTED, "AbortException -> aborted")
    check(not result.success, "中止时 success=False")

    # --- max_ticks 上限 ---
    kernel = ScriptedKernel([in_combat], recognize=lambda n, i: False)
    session = make_session(
        {"rules": [{"name": "r", "actions": ["e"]}]},
        kernel, duration=100.0, tick_interval=0.01, max_ticks=5,
    )
    result = session.run()
    check_equal(result.reason, REASON_MAX_TICKS, "达到 tick 上限")
    check_equal(result.ticks, 5, "tick 数精确等于上限")

    # --- 无可用规则时空转，不发键 ---
    kernel = ScriptedKernel([in_combat], recognize=lambda n, i: False)
    session = make_session(
        {
            "rules": [
                {"name": "never_r", "when": {"enemy_visible": True},
                 "actions": ["e"]}
            ]
        },
        kernel, duration=0.5, tick_interval=0.1,
    )
    result = session.run()
    check(result.idle_ticks > 0, "无可用规则时记录空闲 tick")
    check_equal(result.rule_fires, {}, "空闲时没有规则触发")
    check(
        not any(c[0] == "send_key" for c in kernel.calls),
        "空闲时不发任何按键",
    )

    # --- 身份条件端到端：roster + 槽位驱动 ---
    kernel = ScriptedKernel(
        [frames["slot1_white"]], recognize=lambda n, i: False
    )
    session = make_session(
        {
            "rules": [
                {"name": "mint_skill", "when": {"character": "mint"},
                 "actions": ["e"]},
                {"name": "zero_skill", "when": {"character": "zero"},
                 "actions": ["q"]},
            ],
            "roster": {"1": "mint", "2": "zero"},
        },
        kernel, duration=0.5, tick_interval=0.1,
    )
    result = session.run()
    check(
        result.rule_fires.get("zero_skill", 0) > 0,
        "槽位1=zero，触发 zero 的规则",
    )
    check_equal(
        result.rule_fires.get("mint_skill", 0), 0, "未触发 mint 的规则"
    )
    check(("send_key", "q") in kernel.calls, "发出 zero 的技能键")
    check(("send_key", "e") not in kernel.calls, "未发出 mint 的技能键")

    # --- once 规则在整场会话只触发一次 ---
    kernel = ScriptedKernel([in_combat], recognize=lambda n, i: False)
    session = make_session(
        {
            "rules": [{"name": "opener", "actions": ["r"], "once": True}],
            "fallback": ["space"],
        },
        kernel, duration=1.0, tick_interval=0.1,
    )
    result = session.run()
    check_equal(
        result.rule_fires.get("opener"), 1, "once 规则整场只触发一次"
    )
    check(result.fallback_fires > 0, "其余 tick 走兜底")

    # --- 未配对的 hold 在会话结束时被释放（正常结束路径）---
    kernel = ScriptedKernel([in_combat], recognize=lambda n, i: False)
    session = make_session(
        {"rules": [{"name": "dash", "actions": ["hold:lshift"], "once": True}]},
        kernel, duration=0.4, tick_interval=0.1,
    )
    result = session.run()
    check(result.success, "正常结束")
    check(
        ("key_up", "lshift") in kernel.calls,
        "正常结束时也释放未配对的 hold",
    )

    # --- commit 必须在动作执行之后 ---
    # runtime 文档承诺"执行后才记账，避免决定了但没执行成功却白占冷却"。
    # 用一个执行时抛异常的 runner 来验证：动作失败 => 不该记账。
    from agent.custom.action.Combat.script import ActionRunner

    class ExplodingRunner(ActionRunner):
        """第一次执行动作就抛异常，用来观察记账是否已经发生。"""

        def run(self, actions):
            raise RuntimeError("action execution failed")

    kernel = ScriptedKernel([in_combat], recognize=lambda n, i: False)
    script = parse_script(
        {"rules": [{"name": "cd_rule", "actions": ["e"], "cooldown": 99}]}
    )
    perception = Perception(kernel, roster=None)
    session = CombatSession(
        kernel,
        script,
        config=SessionConfig(duration=1.0, tick_interval=0.1),
        perception=perception,
        runner=ExplodingRunner(kernel, log=kernel.log_warning),
        clock=kernel.clock,
    )
    result = session.run()
    check_equal(result.reason, REASON_ERROR, "执行失败 -> error")
    # 关键断言：动作没执行成功，冷却账本不该被写入
    check_equal(
        session.engine.ledger.count("cd_rule"),
        0,
        "动作执行失败时不记账（commit 在执行后）",
    )
    check_equal(
        result.rule_fires, {}, "执行失败不计入 rule_fires"
    )

    # 正向对照：执行成功时必须记账
    kernel = ScriptedKernel([in_combat], recognize=lambda n, i: False)
    session = make_session(
        {"rules": [{"name": "cd_rule", "actions": ["e"], "cooldown": 99}]},
        kernel, duration=0.5, tick_interval=0.1,
    )
    result = session.run()
    check_equal(
        session.engine.ledger.count("cd_rule"), 1, "执行成功时记账一次"
    )
    check_equal(
        result.rule_fires.get("cd_rule"), 1, "长冷却规则整场只触发一次"
    )

    # --- ESC 菜单：立刻停手，宽限后结束 ---
    from agent.custom.action.Combat.perception import MENU_NODE
    from agent.custom.action.Combat.runtime import REASON_MENU_OPEN

    # 菜单一直开着 -> 宽限期后以 menu_open 结束
    kernel = ScriptedKernel(
        [in_combat], recognize=lambda node, img: node == MENU_NODE
    )
    session = make_session(
        {"rules": [{"name": "atk", "actions": ["e"]}]},
        kernel, duration=10.0, tick_interval=0.1, menu_grace=0.5,
    )
    result = session.run()
    check_equal(result.reason, REASON_MENU_OPEN, "菜单持续打开 -> 结束会话")
    check(result.success, "菜单结束属于正常结束")
    # 关键：菜单开着时一个技能键都不能发出去
    check(
        ("send_key", "e") not in kernel.calls,
        "菜单打开时不发任何技能键",
    )
    check(
        any("ESC 菜单" in m for m in kernel.logs), "菜单打开被记录"
    )
    check_equal(result.rule_fires, {}, "菜单期间没有规则触发")

    # 菜单开着但按住的键必须被松开（否则角色在菜单里还在跑）
    kernel = ScriptedKernel(
        [in_combat], recognize=lambda node, img: node == MENU_NODE
    )
    session = make_session(
        {"rules": [{"name": "run", "actions": ["hold:w"]}]},
        kernel, duration=10.0, tick_interval=0.1, menu_grace=0.4,
    )
    result = session.run()
    check_equal(result.reason, REASON_MENU_OPEN, "菜单结束（hold 场景）")
    check(
        ("key_down", "w") not in kernel.calls,
        "菜单打开时连 hold 都不该按下",
    )

    # 关键场景：先在战斗中按住 W，菜单随后弹出 -> 必须立刻松开。
    # 上一个用例菜单一开始就开着，所以根本没按下过，覆盖不到"松手"逻辑。
    menu_later = {"n": 0}

    def menu_after_two(node, img):
        if node != MENU_NODE:
            return False
        menu_later["n"] += 1
        return menu_later["n"] > 1  # 第一次检测未开，之后打开

    kernel = ScriptedKernel([in_combat], recognize=menu_after_two)
    session = make_session(
        {"rules": [{"name": "run", "actions": ["hold:w"], "once": True}]},
        kernel, duration=10.0, tick_interval=0.6, menu_grace=1.5,
    )
    result = session.run()
    check(
        ("key_down", "w") in kernel.calls,
        "菜单弹出前确实按下了 W（前置条件）",
    )
    check_equal(result.reason, REASON_MENU_OPEN, "菜单弹出后结束会话")
    # 必须在会话进行中就松手，而不是等 finally 收尾
    down_at = kernel.calls.index(("key_down", "w"))
    up_at = kernel.calls.index(("key_up", "w"))
    check(up_at > down_at, "W 在按下之后被松开")
    tail = kernel.calls[up_at:]
    check(
        any(c[0] == "sleep" for c in tail),
        "松手发生在会话循环内（之后还有 tick 等待），而非仅靠收尾兜底",
    )

    # 菜单短暂打开后关闭 -> 战斗继续，不结束
    frames_menu = [in_combat]
    menu_state = {"open": True, "calls": 0}

    def transient_menu(node, img):
        if node != MENU_NODE:
            return False
        menu_state["calls"] += 1
        # 前两次检测菜单是开的，之后关闭
        return menu_state["calls"] <= 2

    kernel = ScriptedKernel(frames_menu, recognize=transient_menu)
    session = make_session(
        {"rules": [{"name": "atk", "actions": ["e"]}]},
        kernel, duration=1.5, tick_interval=0.1, menu_grace=5.0,
    )
    result = session.run()
    check_equal(
        result.reason, REASON_TIMEOUT, "菜单短暂打开不结束会话"
    )
    check(
        ("send_key", "e") in kernel.calls, "菜单关闭后恢复发键"
    )

    # 关掉 stop_when_menu_open -> 不结束，但仍然不发键
    kernel = ScriptedKernel(
        [in_combat], recognize=lambda node, img: node == MENU_NODE
    )
    script = parse_script({"rules": [{"name": "atk", "actions": ["e"]}]})
    session = CombatSession(
        kernel, script,
        config=SessionConfig(
            duration=0.6, tick_interval=0.1, stop_when_menu_open=False
        ),
        perception=Perception(kernel, roster=None),
        clock=kernel.clock,
    )
    result = session.run()
    check_equal(
        result.reason, REASON_TIMEOUT, "关掉菜单结束开关后跑到超时"
    )
    check(
        ("send_key", "e") not in kernel.calls,
        "即使不结束会话，菜单打开时也不发键",
    )
    check(result.idle_ticks > 0, "菜单期间计入空闲 tick")

    # --- 启动前场景守卫：大世界拒跑 ---
    from agent.custom.action.Combat.perception import WORLD_NODE
    from agent.custom.action.Combat.runtime import REASON_OPEN_WORLD

    kernel = ScriptedKernel(
        [in_combat], recognize=lambda node, img: node == WORLD_NODE
    )
    session = make_session(
        {"rules": [{"name": "atk", "actions": ["e"]}], "fallback": ["space"]},
        kernel, duration=5.0, tick_interval=0.1,
    )
    result = session.run()
    check_equal(result.reason, REASON_OPEN_WORLD, "大世界启动 -> 拒绝运行")
    check(not result.success, "拒绝运行时 success=False")
    check_equal(result.ticks, 0, "大世界时一个 tick 都不跑")
    # 关键：一个键都不能发出去
    check(
        not any(c[0] in ("send_key", "key_down", "click") for c in kernel.calls),
        "大世界时不发任何按键",
    )
    check(
        any("大世界" in m for m in kernel.logs),
        "拒绝原因写进日志，用户能看懂为什么没动",
    )

    # 不在大世界 -> 正常跑
    kernel = ScriptedKernel([in_combat], recognize=lambda node, img: False)
    session = make_session(
        {"rules": [{"name": "atk", "actions": ["e"]}]},
        kernel, duration=0.5, tick_interval=0.1,
    )
    result = session.run()
    check(result.ticks > 0, "非大世界时正常运行")
    check(("send_key", "e") in kernel.calls, "非大世界时正常发键")

    # 守卫可关闭（给想在非战斗场景跑脚本的用户留出口）
    kernel = ScriptedKernel(
        [in_combat], recognize=lambda node, img: node == WORLD_NODE
    )
    script = parse_script({"rules": [{"name": "atk", "actions": ["e"]}]})
    session = CombatSession(
        kernel, script,
        config=SessionConfig(
            duration=0.4, tick_interval=0.1, guard_open_world=False
        ),
        perception=Perception(kernel, roster=None),
        clock=kernel.clock,
    )
    result = session.run()
    check(
        result.reason != REASON_OPEN_WORLD, "关掉守卫后不再拦截"
    )
    check(result.ticks > 0, "关掉守卫后正常跑")

    # 守卫识别异常 -> 放行（不因识别抖动挡掉合法战斗）
    def world_explodes(node, img):
        if node == WORLD_NODE:
            raise RuntimeError("world recognition failed")
        return False

    kernel = ScriptedKernel([in_combat], recognize=world_explodes)
    session = make_session(
        {"rules": [{"name": "atk", "actions": ["e"]}]},
        kernel, duration=0.4, tick_interval=0.1,
    )
    result = session.run()
    check(
        result.reason != REASON_OPEN_WORLD, "守卫识别异常时放行"
    )
    check(
        any("大世界检测失败" in m for m in kernel.logs), "守卫异常被记录"
    )

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_entry_point():
    from agent.custom.action.Combat.script import parse_script
    from agent.custom.action.auto_combat import _load_script_source

    print("[10] 入口：脚本来源解析与拒绝策略")

    # 内联脚本
    raw, source = _load_script_source(
        {"script": {"rules": [{"actions": ["e"]}]}}
    )
    check(raw is not None, "内联脚本可加载")
    check("custom_action_param" in source, "来源标注为内联")

    # 未指定任何来源 -> 必须拒绝
    raw, source = _load_script_source({})
    check_equal(raw, None, "未指定脚本时拒绝")
    check("未指定脚本" in source, "拒绝原因明确")

    # 目录穿越防护
    for evil in ("../secret", "..\\secret", "/etc/passwd", ".hidden",
                 "sub/dir"):
        raw, source = _load_script_source({"preset": evil})
        check_equal(raw, None, f"拒绝可疑预设名: {evil!r}")
        check(
            "非法" in source or "不存在" in source,
            f"拒绝原因明确: {evil!r}",
        )

    # 不存在的预设：错误信息要列出可用预设，便于用户自查
    raw, source = _load_script_source({"preset": "definitely_not_here"})
    check_equal(raw, None, "不存在的预设被拒绝")
    check("可用预设" in source, "列出了可用预设")

    # 不存在的文件
    raw, source = _load_script_source({"script_path": "no/such/file.json"})
    check_equal(raw, None, "不存在的脚本文件被拒绝")
    check("不存在" in source, "文件不存在原因明确")

    # 优先级：script > preset > script_path
    raw, source = _load_script_source(
        {
            "script": {"rules": [{"actions": ["e"]}]},
            "preset": "whatever",
            "script_path": "whatever.json",
        }
    )
    check("custom_action_param" in source, "内联脚本优先级最高")

    # 空脚本内容会被 parse_script 拒绝（入口据此返回失败）
    script = parse_script({"rules": []})
    check(
        not script.rules and not script.fallback,
        "空脚本没有可执行内容",
    )
    check(len(script.issues) > 0, "空脚本有 issue")

    print(f"     完成，累计断言 {_CHECKS} 项")


def group_kernel_equivalence():
    print("[11] 内核等价性：原 core3 实现 vs 提取后的内核")
    frames = build_equivalence_corpus()

    for name, image in frames.items():
        # 布尔判定
        check_equal(
            kteam.is_black_screen(image),
            BASE._is_black_screen_in_image(image),
            f"is_black_screen({name})",
        )
        check_equal(
            kteam.is_in_team(image),
            BASE._is_in_team_in_image(image),
            f"is_in_team({name})",
        )

        # 逐元素数值必须完全相等
        check_equal(
            kteam.slot_scores(image),
            BASE._current_char_scores(image),
            f"slot_scores({name})",
        )
        check_equal(
            kteam.slot_core_scores(image),
            BASE._current_char_core_scores(image),
            f"slot_core_scores({name})",
        )

        base_scores = BASE._current_char_scores(image)
        for index in range(4):
            check_equal(
                kteam.is_slot_score_accepted(base_scores, index),
                BASE._is_current_char_score_accepted(base_scores, index),
                f"is_slot_score_accepted({name}, {index})",
            )

        check_equal(
            kteam.is_slot2_core_accepted(image),
            BASE._is_slot2_core_score_accepted(image),
            f"is_slot2_core_accepted({name})",
        )

        # current_slot_index / is_slot_active 的纯逻辑部分
        base_best = -1
        if base_scores:
            candidate = max(range(len(base_scores)), key=lambda i: base_scores[i])
            if BASE._is_current_char_score_accepted(base_scores, candidate):
                base_best = candidate
        check_equal(
            kteam.current_slot_index(image), base_best, f"current_slot_index({name})"
        )

        for index in range(4):
            expected = BASE._is_current_char_score_accepted(base_scores, index)
            if not expected and index == 1:
                expected = BASE._is_slot2_core_score_accepted(image)
            check_equal(
                kteam.is_slot_active(image, index),
                expected,
                f"is_slot_active({name}, {index})",
            )

    # ROI 工具
    rois = [
        TEAM_HEALTH_SLASH_ROI,
        CURRENT_CHAR_MARKER_ROI,
        CURRENT_CHAR_MARKER_CORE_ROI,
        [0, 0, 1, 1],
        [1270, 710, 40, 40],  # 越界
        [-20, -20, 60, 60],  # 负坐标
        [1400, 800, 10, 10],  # 完全在外
    ]
    for name in ("slot0_white", "slot0_half_res", "gray_2d", "empty"):
        image = frames[name]
        for roi in rois:
            got = crop_roi(image, roi)
            want = BASE._crop_roi(image, roi)
            if want is None or got is None:
                check(
                    (want is None) == (got is None),
                    f"crop_roi({name}, {roi}) none-ness",
                )
                continue
            check(
                got.shape == want.shape and np.array_equal(got, want),
                f"crop_roi({name}, {roi}) content",
            )
            check_equal(
                scale_roi(roi, image), BASE._scale_roi(roi, image),
                f"scale_roi({name}, {roi})",
            )
        got_bgr = as_bgr_image(image)
        want_bgr = BASE._as_bgr_image(image)
        if want_bgr is None or got_bgr is None:
            check(
                (want_bgr is None) == (got_bgr is None), f"as_bgr_image({name}) none"
            )
        else:
            check(np.array_equal(got_bgr, want_bgr), f"as_bgr_image({name}) content")

    # 快速颜色匹配
    color_cfg = {
        "roi": [650, 240, 520, 460],
        "lower_bgr": [119, 71, 197],
        "upper_bgr": [133, 78, 221],
        "count": 80,
        "stride": 4,
    }
    pink = blank_frame()
    paint(pink, [700, 300, 60, 60], (126, 74, 210))
    tiny_pink = blank_frame()
    paint(tiny_pink, [700, 300, 4, 4], (126, 74, 210))
    for name, image in (
        ("pink_blob", pink),
        ("tiny_pink", tiny_pink),
        ("blank", blank_frame()),
        ("empty", frames["empty"]),
    ):
        check_equal(
            fast_color_match(image, color_cfg),
            BASE._fast_color_match(image, color_cfg),
            f"fast_color_match({name})",
        )

    # 按键名与参数解析
    key_inputs = [
        None,
        "W",
        "wasd",
        "w+a",
        "w, a",
        ["W", "a", "W"],
        ("lshift", "space"),
        {"e"},
        123,
        "  Esc  ",
        "wd",
        "left",
    ]
    for value in key_inputs:
        check_equal(
            normalize_key_sequence(value),
            BASE._normalize_key_sequence(value),
            f"normalize_key_sequence({value!r})",
        )
    for value in ("W", "LShift", 4, "Esc"):
        check_equal(norm_key(value), BASE._norm_key(value), f"norm_key({value!r})")

    # is_hit 的多种返回结构
    class _Status:
        def __init__(self, succeeded=None):
            if succeeded is not None:
                self.succeeded = succeeded

    class _Result:
        def __init__(self, **kw):
            for key, value in kw.items():
                setattr(self, key, value)

    hit_cases = [
        None,
        _Result(),
        _Result(hit=False),
        _Result(hit=True),
        _Result(status=_Status(succeeded=True)),
        _Result(status=_Status(succeeded=False)),
        _Result(status=0),
        _Result(status=1),
    ]
    for index, case in enumerate(hit_cases):
        check_equal(is_hit(case), BASE._is_hit(case), f"is_hit(case{index})")

    print(f"     完成，累计断言 {_CHECKS} 项")


# ----------------------------------------------------------------------
# 第 11b 组：切人状态机的状态转移
# ----------------------------------------------------------------------


class FakeClock:
    """可手动推进的单调时钟，让状态机的超时分支变得可确定复现。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += float(delta)


class SwitchHarness:
    """驱动 CharacterSwitcher 的假环境，记录完整事件流。

    图像用一个 ``(kind, slot)`` 元组代替真实截图：``kind`` 取
    ``team`` / ``black`` / ``dead``，``slot`` 表示当前高亮槽位。
    这样可以精确编排"按了键之后画面变成什么"，而不必造 30 张图。
    """

    def __init__(self, frames, clock=None, auto_advance=0.05):
        self.frames = list(frames)
        self.clock = clock or FakeClock()
        self.events: list[str] = []
        self._auto_advance = auto_advance

    # --- 钩子实现 ---

    def screencap(self):
        if not self.frames:
            return ("team", -1)
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]

    def is_black_screen(self, image):
        return image is not None and image[0] == "black"

    def is_in_team(self, image):
        return image is not None and image[0] != "dead"

    def is_slot_active(self, image, index):
        return image is not None and image[1] == index

    def send_key(self, key, action_name=None, interval=-1):
        self.events.append(f"key:{key}")
        return True

    def ensure_in_team(self, time_out=2.0):
        self.events.append("ensure_in_team")
        return True

    def sleep(self, timeout):
        self.clock.advance(max(float(timeout), self._auto_advance))
        return True

    def log_warning(self, *args):
        self.events.append("warn:" + " ".join(str(a) for a in args))

    def on_key_sent(self, role, key):
        self.events.append(f"sent:{role}:{key}")

    def on_candidate_dead(self, role, key):
        self.events.append(f"dead:{role}:{key}")

    def build(self):
        from agent.custom.action.Combat.kernel.switching import (
            CharacterSwitcher,
            SwitchHooks,
        )

        return CharacterSwitcher(
            SwitchHooks(
                screencap=self.screencap,
                is_black_screen=self.is_black_screen,
                is_in_team=self.is_in_team,
                is_slot_active=self.is_slot_active,
                send_key=self.send_key,
                ensure_in_team=self.ensure_in_team,
                sleep=self.sleep,
                log_warning=self.log_warning,
                on_key_sent=self.on_key_sent,
                on_candidate_dead=self.on_candidate_dead,
                monotonic=self.clock,
            )
        )


def group_switch_state_machine():
    from agent.custom.action.Combat.kernel.errors import AbortException

    print("[11b] 切人状态机：状态转移序列")

    # 1) 一次成功：按 3 -> 画面高亮到槽位 2（key "3" 对应 index 2）
    h = SwitchHarness([("team", 2)])
    switcher = h.build()
    result = switcher.begin("runner", ["3"], check_switched=True)
    check_equal(result, "3", "switch/一次成功 返回键")
    check_equal(switcher.state, None, "switch/一次成功 状态已清空")
    check(
        "sent:runner:3" in h.events and "key:3" in h.events,
        "switch/一次成功 发出候选键",
    )
    check(
        not any(e.startswith("dead:") for e in h.events),
        "switch/一次成功 未误判死亡",
    )

    # 2) 重试后成功：先给不匹配的画面并推进超时，触发一次 retry
    clock = FakeClock()
    h = SwitchHarness([("team", 0), ("team", 0), ("team", 2)], clock=clock)
    switcher = h.build()
    original_sleep = h.sleep

    def sleep_pushing_deadline(timeout):
        original_sleep(timeout)
        clock.advance(1.2)  # 越过 SWITCH_CHECK_DURATION，逼出 retry

    h.sleep = sleep_pushing_deadline
    switcher = h.build()
    result = switcher.begin("runner", ["3"], check_switched=True)
    check_equal(result, "3", "switch/重试后 返回键")
    check(
        any("retry 1" in e for e in h.events),
        "switch/重试后 记录了一次重试",
    )
    check_equal(switcher.state, None, "switch/重试后 状态已清空")

    # 3) 重试耗尽放弃：画面永远不匹配
    clock = FakeClock()
    h = SwitchHarness([("team", 0)], clock=clock)

    def sleep_never_match(timeout):
        clock.advance(max(float(timeout), 0.05) + 1.2)

    h.sleep = sleep_never_match
    switcher = h.build()
    result = switcher.begin("runner", ["3"], check_switched=True)
    check_equal(result, "3", "switch/重试耗尽 仍返回最后候选")
    check(
        any("not confirmed" in e and "retry" not in e for e in h.events),
        "switch/重试耗尽 记录放弃",
    )
    check_equal(switcher.state, None, "switch/重试耗尽 状态已清空")

    # 4) 黑屏延时：黑屏期间不应判死，且截止时间被延长
    clock = FakeClock()
    h = SwitchHarness([("black", -1), ("black", -1), ("team", 2)], clock=clock)
    switcher = h.build()
    result = switcher.begin("runner", ["3"], check_switched=True)
    check_equal(result, "3", "switch/黑屏 最终确认成功")
    check(
        not any(e.startswith("dead:") for e in h.events),
        "switch/黑屏 期间不判死亡",
    )

    # 5) 死亡候选跳转：首个候选不在队伍 -> 换下一个
    clock = FakeClock()
    frames = [("dead", -1)] * 6 + [("team", 0)]
    h = SwitchHarness(frames, clock=clock)
    switcher = h.build()
    result = switcher.begin("fighter", ["4", "1"], check_switched=True)
    check_equal(result, "1", "switch/死亡跳转 切到下一个候选")
    check(
        "dead:fighter:4" in h.events, "switch/死亡跳转 记录首个候选阵亡"
    )
    check("ensure_in_team" in h.events, "switch/死亡跳转 尝试恢复队伍 UI")

    # 6) 全员判死：所有候选都不在队伍 -> AbortException
    clock = FakeClock()
    h = SwitchHarness([("dead", -1)], clock=clock)
    switcher = h.build()
    raised = False
    try:
        switcher.begin("fighter", ["4", "1"], check_switched=True)
    except AbortException as exc:
        raised = "dead or empty" in str(exc)
    check(raised, "switch/全员判死 抛出 AbortException")
    check_equal(switcher.state, None, "switch/全员判死 状态已清空")

    # 7) 空候选列表立即报错
    h = SwitchHarness([("team", 0)])
    switcher = h.build()
    raised = False
    try:
        switcher.begin("avoider", [], check_switched=False)
    except AbortException:
        raised = True
    check(raised, "switch/空候选 抛出 AbortException")

    # 8) 不确认模式：只发键，不等待
    h = SwitchHarness([("team", 0)])
    switcher = h.build()
    result = switcher.begin("runner", ["3"], check_switched=False)
    check_equal(result, "3", "switch/不确认 返回候选键")
    check(switcher.state is not None, "switch/不确认 状态仍在（交给 poll 收尾）")

    # 9) poll 在超时后自行清理状态
    clock = FakeClock()
    h = SwitchHarness([("team", 0)], clock=clock)
    switcher = h.build()
    switcher.begin("runner", ["3"], check_switched=False)
    clock.advance(5.0)
    switcher.poll()
    check_equal(switcher.state, None, "switch/poll 超时后清空状态")

    # 10) poll 遇到黑屏时延长截止时间而非清空
    #     注意：延长用的是 max(deadline, now + 0.5)，因此必须先推进到
    #     "剩余时间不足 0.5s" 才能观察到延长效果。这正是原实现的语义。
    clock = FakeClock()
    h = SwitchHarness([("black", -1)], clock=clock)
    switcher = h.build()
    switcher.begin("runner", ["3"], check_switched=False)
    clock.advance(0.8)
    before = switcher.state.deadline
    switcher.poll()
    check(
        switcher.state is not None and switcher.state.deadline > before,
        "switch/poll 黑屏时延长截止时间",
    )

    # 10b) 剩余时间充足时黑屏不应缩短截止时间
    clock = FakeClock()
    h = SwitchHarness([("black", -1)], clock=clock)
    switcher = h.build()
    switcher.begin("runner", ["3"], check_switched=False)
    clock.advance(0.2)
    before = switcher.state.deadline
    switcher.poll()
    check(
        switcher.state is not None and switcher.state.deadline == before,
        "switch/poll 黑屏且剩余充足时不改截止时间",
    )

    print(f"     完成，累计断言 {_CHECKS} 项")


# ----------------------------------------------------------------------
# 第 11c 组：切人状态机与原实现的差分等价
# ----------------------------------------------------------------------


def _build_original_switcher_class():
    """从基线快照里把原切人状态机摘成一个可独立实例化的类。

    原方法依赖 ``self`` 上的一堆能力（截图、判定、发键、日志），我们在
    ``make`` 里用 harness 的钩子逐个注入，从而让原实现和新实现跑在**同一个
    假环境**上，事件流才可以逐条比对。
    """
    import ast
    import types

    from agent.custom.action.Combat.kernel.errors import AbortException
    from agent.custom.action.Combat.kernel.switching import CharacterSwitchState

    wanted = {
        "_send_current_switch_key",
        "_clear_switch_state",
        "_handle_dead_switch_candidate",
        "_poll_character_switch",
        "_wait_character_switch_success",
        "_begin_character_switch",
    }
    tree = ast.parse(BASELINE_SOURCE)
    methods: list[ast.FunctionDef] = []
    consts: list[ast.stmt] = []
    forbidden = {"ctypes", "wintypes", "maa", "Image", "cv2", "AgentServer"}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if not names & forbidden:
                consts.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "PinkPawHeistCore3Path":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in wanted:
                    # 去掉注解，避免类体求值时找不到类型名
                    item.returns = None
                    for arg in item.args.args:
                        arg.annotation = None
                    methods.append(item)

    missing = wanted - {m.name for m in methods}
    if missing:
        raise ValueError(f"baseline missing switch methods: {missing}")

    cls = ast.ClassDef(
        name="OriginalSwitcher",
        bases=[],
        keywords=[],
        body=methods,
        decorator_list=[],
        type_params=[],
    )
    module = ast.Module(body=consts + [cls], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "CharacterSwitchState": CharacterSwitchState,
        "AbortException": AbortException,
    }
    exec(compile(module, "<baseline-switcher>", "exec"), namespace)
    original = namespace["OriginalSwitcher"]

    def make(harness, clock):
        obj = original()
        obj.ROLE_FIGHTER = "fighter"
        obj.SWITCH_CHECK_DURATION = 1.0
        obj._switch_state = None
        obj._handling_switch_state = False
        obj._next_switch_poll_at = 0.0
        obj._current_fighter_key = None
        obj._dead_fighter_keys = []
        obj._screencap = harness.screencap
        obj._is_black_screen_in_image = harness.is_black_screen
        obj._is_in_team_in_image = harness.is_in_team
        obj.is_char_at_index = lambda index, image=None: harness.is_slot_active(
            image, index
        )
        obj.send_key = lambda key, action_name=None, interval=-1: harness.send_key(
            key, action_name, interval
        )
        obj.ensure_in_team = harness.ensure_in_team
        obj.sleep = lambda t, check_reward=True, scaled=True: harness.sleep(t)
        obj.log_warning = harness.log_warning
        # 原方法内部直接调 time.monotonic()，替换成假时钟
        for name in wanted:
            getattr(original, name).__globals__["time"] = types.SimpleNamespace(
                monotonic=clock
            )
        return obj

    return make


def group_switch_equivalence():
    from agent.custom.action.Combat.kernel.errors import AbortException

    print("[11c] 切人状态机等价性：原实现 vs 内核实现（同一假环境）")
    make_original = _build_original_switcher_class()

    def visible(events):
        """投影掉新版专有的业务簿记事件，只比对可观察的外部行为。"""
        return [e for e in events if not e.startswith(("sent:", "dead:"))]

    def timeout_sleep(clock):
        """让每次等待都越过确认窗口，用于逼出超时/重试分支。"""

        def sleeper(timeout):
            clock.advance(max(float(timeout), 0.05) + 1.2)

        return sleeper

    cases = [
        ("一次成功", [("team", 2)], ("runner", ["3"], True), False),
        (
            "黑屏后成功",
            [("black", -1), ("black", -1), ("team", 2)],
            ("runner", ["3"], True),
            False,
        ),
        (
            "死亡候选跳转",
            [("dead", -1)] * 6 + [("team", 0)],
            ("fighter", ["4", "1"], True),
            False,
        ),
        ("全员判死", [("dead", -1)], ("fighter", ["4", "1"], True), False),
        ("不确认模式", [("team", 0)], ("runner", ["3"], False), False),
        ("重试耗尽", [("team", 0)], ("runner", ["3"], True), True),
    ]

    for label, frames, (role, keys, confirm), force_timeout in cases:
        clock_a = FakeClock()
        harness_a = SwitchHarness(list(frames), clock=clock_a)
        if force_timeout:
            harness_a.sleep = timeout_sleep(clock_a)
        original = make_original(harness_a, clock_a)
        try:
            result_a = original._begin_character_switch(role, keys, confirm)
            error_a = None
        except AbortException as exc:
            result_a, error_a = None, str(exc)

        clock_b = FakeClock()
        harness_b = SwitchHarness(list(frames), clock=clock_b)
        if force_timeout:
            harness_b.sleep = timeout_sleep(clock_b)
        switcher = harness_b.build()
        try:
            result_b = switcher.begin(role, keys, check_switched=confirm)
            error_b = None
        except AbortException as exc:
            result_b, error_b = None, str(exc)

        check_equal(result_b, result_a, f"switch-eq/{label} 返回值")
        check_equal(
            error_b is None, error_a is None, f"switch-eq/{label} 是否抛异常"
        )
        check_equal(
            visible(harness_b.events),
            visible(harness_a.events),
            f"switch-eq/{label} 事件流",
        )

    print(f"     完成，累计断言 {_CHECKS} 项")

def main():
    print("=" * 68)
    print("战斗内核离线验证")
    print("=" * 68)
    group_script_parsing()
    group_conditions()
    group_engine()
    group_primitives()
    group_identity_catalog()
    group_identity_recognition()
    group_perception()
    group_session_loop()
    group_entry_point()
    group_kernel_equivalence()
    group_switch_state_machine()
    group_switch_equivalence()
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
