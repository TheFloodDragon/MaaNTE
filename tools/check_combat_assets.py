"""战斗引擎的资源静态校验。

在 CI / 提 PR 前跑一次，把以下问题挡在合并之前：

1. 内置预设能被解析，且没有 issue；
2. 预设引用的角色都在名录里；
3. 预设用了 ``character`` 条件却没声明 ``roster``（条件会永远不成立）；
4. 预设用了当前**未实现的感知信号**（如 ``hp_below``），会永远不成立；
5. 任务定义里引用的 locale key 在五种语言文件里都存在；
6. 任务定义引用的 pipeline 节点存在；
7. 预设引用的按键都在内核 VK 表里（否则运行时静默不发键）。

用法：``python tools/check_combat_assets.py``
返回码非 0 表示有问题。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))

from agent.custom.action.Combat.identity import resolve as resolve_character  # noqa: E402
from agent.custom.action.Combat.kernel.constants import VK  # noqa: E402
from agent.custom.action.Combat.script import parse_script  # noqa: E402
from agent.custom.action.Combat.script.conditions import (  # noqa: E402
    All,
    Any_,
    HpCompare,
    IsCharacter,
    Not,
)
from agent.custom.action.Combat.script.schema import (  # noqa: E402
    ACTION_CLICK,
    ACTION_MOUSE_DOWN,
    ACTION_MOUSE_UP,
    ALLOWED_MOUSE,
)

COMBAT_DIR = REPO / "assets" / "resource" / "base" / "combat"
TASKS_DIR = REPO / "assets" / "resource" / "tasks"
PIPELINE_DIR = REPO / "assets" / "resource" / "base" / "pipeline"
LOCALE_DIR = REPO / "assets" / "resource" / "locales" / "interface"
LANGUAGES = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")

# 感知层当前未实现的信号。用了这些条件的脚本不会报错，但条件恒为假，
# 属于"看起来配了其实没用"，必须在 CI 阶段告知。
# 补齐感知后从这里移除对应项。
UNIMPLEMENTED_SIGNALS = {
    "self_hp": "自身血量（缺少已验证的血条 ROI）",
    "boss_hp": "Boss 血量（缺少已验证的血条 ROI）",
}

_errors: list[str] = []
_warnings: list[str] = []


def error(message):
    _errors.append(message)
    print(f"  [ERROR] {message}")


def warn(message):
    _warnings.append(message)
    print(f"  [WARN ] {message}")


def load_jsonc(path: Path):
    """读取可能带 // 注释的 JSON（interface.json 用了 JSONC）。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return json.loads(text)


def walk_conditions(condition):
    """遍历条件树，产出所有叶子条件。"""
    if isinstance(condition, Not):
        yield from walk_conditions(condition.inner)
    elif isinstance(condition, (All, Any_)):
        for item in condition.items:
            yield from walk_conditions(item)
    else:
        yield condition


def check_presets():
    """校验内置预设。"""
    print("[1] 内置预设")
    if not COMBAT_DIR.exists():
        error(f"预设目录不存在: {COMBAT_DIR}")
        return

    presets = sorted(COMBAT_DIR.glob("*.json"))
    if not presets:
        error(f"预设目录为空: {COMBAT_DIR}")
        return

    for path in presets:
        label = path.name
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            error(f"{label}: JSON 无法解析: {exc}")
            continue

        script = parse_script(raw)
        for issue in script.issues:
            error(f"{label}: {issue}")

        if not script.rules and not script.fallback:
            error(f"{label}: 既无规则也无兜底动作，跑起来什么都不做")

        # 预设应当有 desc，方便用户理解它干什么
        if not raw.get("desc"):
            warn(f"{label}: 建议补 desc 字段说明预设用途")

        uses_character = False
        for rule in script.rules:
            for leaf in walk_conditions(rule.condition):
                if isinstance(leaf, IsCharacter):
                    uses_character = True
                    for name in leaf.names:
                        if resolve_character(name) is None:
                            error(
                                f"{label}/{rule.name}: 未知角色 {name!r}，"
                                "不在角色名录中"
                            )
                if isinstance(leaf, HpCompare):
                    reason = UNIMPLEMENTED_SIGNALS.get(
                        "boss_hp" if leaf.target == "boss" else "self_hp"
                    )
                    if reason:
                        warn(
                            f"{label}/{rule.name}: 用了 {leaf.describe()}，"
                            f"但{reason}尚未实现，该条件将永远不成立"
                        )

        roster = getattr(script, "roster", None)
        configured = bool(roster is not None and roster.configured)
        if uses_character and not configured:
            error(
                f"{label}: 用了 character 条件但没声明 roster，"
                "这些条件永远不会成立"
            )
        if configured and not uses_character:
            warn(f"{label}: 声明了 roster 但没有任何 character 条件使用它")

        # 按键必须在 VK 表里，否则运行时静默不发键
        for rule in script.rules:
            for action in rule.actions:
                _check_action_key(label, rule.name, action)
        for action in script.fallback:
            _check_action_key(label, "fallback", action)

        print(f"  [ok] {label}: 规则 {len(script.rules)} 条，"
              f"兜底 {len(script.fallback)} 个")


def _check_action_key(label, rule_name, action):
    if not action.key:
        return
    if action.type in {ACTION_CLICK, ACTION_MOUSE_DOWN, ACTION_MOUSE_UP}:
        if action.key not in ALLOWED_MOUSE:
            error(f"{label}/{rule_name}: 非法鼠标键 {action.key!r}")
        return
    if action.key not in VK:
        error(
            f"{label}/{rule_name}: 按键 {action.key!r} 不在内核 VK 表中，"
            "运行时会静默不发键"
        )


def collect_locale_refs(data, found: set):
    """递归收集 $xxx 形式的 locale 引用。"""
    if isinstance(data, str):
        if data.startswith("$"):
            found.add(data[1:])
    elif isinstance(data, dict):
        for value in data.values():
            collect_locale_refs(value, found)
    elif isinstance(data, (list, tuple)):
        for item in data:
            collect_locale_refs(item, found)


def check_task_locales():
    """校验战斗任务定义引用的 locale key 在五种语言里都存在。"""
    print("[2] 任务定义与多语言")
    task_file = TASKS_DIR / "AutoCombat.json"
    if not task_file.exists():
        warn(
            f"任务定义不存在: {task_file}（尚未接入 Pipeline 时属正常）"
        )
        return

    try:
        task_data = json.loads(task_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        error(f"AutoCombat.json 无法解析: {exc}")
        return

    refs: set[str] = set()
    collect_locale_refs(task_data, refs)
    if not refs:
        warn("AutoCombat.json 没有引用任何 locale key")

    locales = {}
    for lang in LANGUAGES:
        path = LOCALE_DIR / f"{lang}.json"
        if not path.exists():
            error(f"语言文件缺失: {path}")
            continue
        try:
            locales[lang] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            error(f"{lang}.json 无法解析: {exc}")

    for key in sorted(refs):
        for lang, data in locales.items():
            if key not in data:
                error(f"locale key 缺失: {lang} 缺 {key!r}")

    if refs and locales:
        print(f"  [ok] 校验了 {len(refs)} 个 locale key × {len(locales)} 种语言")

    # 任务是否注册进 interface.json
    interface = REPO / "assets" / "interface.json"
    if interface.exists():
        text = interface.read_text(encoding="utf-8")
        if "AutoCombat.json" not in text:
            error("AutoCombat.json 未在 assets/interface.json 的 import 中注册")
        else:
            print("  [ok] 已在 interface.json 注册")


def check_pipeline_nodes():
    """校验任务定义引用的 pipeline 节点存在。"""
    print("[3] Pipeline 节点引用")
    task_file = TASKS_DIR / "AutoCombat.json"
    if not task_file.exists():
        print("  [skip] 任务定义尚未创建")
        return

    try:
        task_data = json.loads(task_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # 已在上一组报错

    # 收集所有 pipeline 节点名
    known: set[str] = set()
    for path in PIPELINE_DIR.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            known.update(data.keys())

    entries = []
    for task in task_data.get("task", []):
        entry = task.get("entry")
        if entry:
            entries.append(entry)
    # option 的 pipeline_override 也要引用真实节点
    for option in (task_data.get("option") or {}).values():
        for case in option.get("cases", []):
            for node in (case.get("pipeline_override") or {}):
                entries.append(node)

    for node in sorted(set(entries)):
        if node not in known:
            error(f"引用了不存在的 pipeline 节点: {node!r}")
    if entries:
        print(f"  [ok] 校验了 {len(set(entries))} 个节点引用")


def check_custom_action_registered():
    """校验 CustomAction 已注册，否则 Pipeline 调用会失败。"""
    print("[4] CustomAction 注册")
    init_file = REPO / "agent" / "custom" / "action" / "__init__.py"
    text = init_file.read_text(encoding="utf-8")
    if "auto_combat" not in text:
        error("auto_combat 未在 agent/custom/action/__init__.py 中导入")
    elif '"AutoCombat"' not in text:
        error("AutoCombat 未加入 agent/custom/action/__init__.py 的 __all__")
    else:
        print("  [ok] auto_combat 已导入且在 __all__ 中")

    # pipeline 里引用的 custom_action 名必须与注册名一致
    used: set[str] = set()
    for path in PIPELINE_DIR.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                action = node.get("custom_action")
                if isinstance(action, str) and "Combat" in action:
                    used.add(action)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    for name in sorted(used):
        if name != "AutoCombat":
            warn(f"pipeline 引用了未知的战斗 custom_action: {name!r}")


def check_task_wiring():
    """校验任务定义自身的自洽性。

    这些是提交时最容易漏的低级错误：option 声明了却没定义、group 名写错、
    预设 case 指向不存在的预设文件。全都能静态查出来，不该留到运行时。
    """
    print("[5] 任务接线自洽性")
    task_file = TASKS_DIR / "AutoCombat.json"
    if not task_file.exists():
        print("  [skip] 任务定义尚未创建")
        return
    try:
        task_data = json.loads(task_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    tasks = task_data.get("task") or []
    options = task_data.get("option") or {}

    for task in tasks:
        declared = set(task.get("option") or [])
        defined = set(options)
        for name in sorted(declared - defined):
            error(f"任务 {task.get('name')} 声明了 option {name!r} 但未定义")
        for name in sorted(defined - declared):
            warn(f"option {name!r} 已定义但没有任务引用它")

    # group 必须在 interface.json 里存在
    interface = REPO / "assets" / "interface.json"
    if interface.exists():
        try:
            iface = load_jsonc(interface)
        except ValueError as exc:
            error(f"interface.json 无法解析: {exc}")
            iface = {}
        known_groups = {g.get("name") for g in iface.get("group", [])}
        for task in tasks:
            for group in task.get("group") or []:
                if group not in known_groups:
                    error(
                        f"任务 {task.get('name')} 引用了不存在的 group {group!r}"
                    )

    # 预设 case 指向的预设文件必须存在且可解析
    checked = 0
    for option_name, option in options.items():
        for case in option.get("cases", []):
            override = case.get("pipeline_override") or {}
            for node_body in override.values():
                param = (node_body or {}).get("custom_action_param") or {}
                preset = param.get("preset")
                if not preset:
                    continue
                path = COMBAT_DIR / f"{preset}.json"
                if not path.exists():
                    error(
                        f"{option_name}/{case.get('name')}: 预设文件不存在 "
                        f"{preset}.json"
                    )
                    continue
                checked += 1
    if checked:
        print(f"  [ok] 校验了 {checked} 个预设引用")

    # 入口节点的默认预设也必须有效
    entry_nodes = {t.get("entry") for t in tasks if t.get("entry")}
    for path in PIPELINE_DIR.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for node_name in entry_nodes & set(data):
            param = (data[node_name] or {}).get("custom_action_param") or {}
            preset = param.get("preset")
            if preset and not (COMBAT_DIR / f"{preset}.json").exists():
                error(
                    f"入口节点 {node_name} 的默认预设不存在: {preset}.json"
                )
            elif preset:
                print(f"  [ok] 入口 {node_name} 默认预设: {preset}")


def check_locale_line_endings():
    """校验 locale 文件仍是 LF 行尾。

    用脚本批量改 JSON 很容易把 LF 写成 CRLF，导致整个文件显示为全量改动、
    评审无法看出真实差异。这类问题在 diff 里不明显，值得单独查。
    """
    print("[6] locale 文件行尾")
    bad = []
    for lang in LANGUAGES:
        path = LOCALE_DIR / f"{lang}.json"
        if not path.exists():
            continue
        data = path.read_bytes()
        if b"\r\n" in data:
            bad.append(lang)
    for lang in bad:
        error(f"{lang}.json 含 CRLF 行尾，应统一为 LF")
    if not bad:
        print(f"  [ok] {len(LANGUAGES)} 个语言文件均为 LF")


def main():
    print("=" * 68)
    print("战斗引擎资源校验")
    print("=" * 68)
    check_presets()
    check_task_locales()
    check_pipeline_nodes()
    check_custom_action_registered()
    check_task_wiring()
    check_locale_line_endings()
    print("-" * 68)
    if _errors:
        print(f"发现 {len(_errors)} 个错误，{len(_warnings)} 个警告")
        return 1
    if _warnings:
        print(f"通过（{len(_warnings)} 个警告）")
        return 0
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
