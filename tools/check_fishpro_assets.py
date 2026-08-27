"""FishPro 改动的静态校验：严格 JSONC 解析 + locale 键一致性。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PIPELINE_FILES = [
    "assets/resource/base/pipeline/Fish/FishNew.json",
    "assets/resource/tasks/Fish.json",
    "assets/interface.json",
]

INTERFACE_KEYS = [
    "task_fish_new_option_control_engine",
    "task_fish_new_option_control_engine_desc",
    "task_fish_new_option_control_engine_case_legacy",
    "task_fish_new_option_control_engine_case_pro",
    "task_fish_new_option_pro_learning",
    "task_fish_new_option_pro_learning_desc",
    "task_fish_new_option_pro_debug",
    "task_fish_new_option_pro_debug_desc",
]

AGENT_KEYS = ["autofish.pro_learning_on"]

LANGS = ["zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr"]


def strip_jsonc(text: str) -> str:
    """逐字符剥离 // 与 /* */ 注释，正确跳过字符串与转义。"""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue

        if char == "/" and index + 1 < length:
            nxt = text[index + 1]
            if nxt == "/":
                while index < length and text[index] != "\n":
                    index += 1
                continue
            if nxt == "*":
                index += 2
                while index + 1 < length and not (
                    text[index] == "*" and text[index + 1] == "/"
                ):
                    index += 1
                index += 2
                continue

        out.append(char)
        index += 1
    return "".join(out)


def check_pipelines() -> list[str]:
    errors: list[str] = []
    parsed: dict[str, dict] = {}
    for rel in PIPELINE_FILES:
        path = REPO / rel
        if not path.exists():
            errors.append(f"缺失文件: {rel}")
            continue
        try:
            parsed[rel] = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
        except Exception as exc:
            errors.append(f"JSONC 解析失败 {rel}: {exc}")

    pipeline = parsed.get("assets/resource/base/pipeline/Fish/FishNew.json", {})
    if "FishNewGamingPro" not in pipeline:
        errors.append("FishNew.json 缺少 FishNewGamingPro 节点")
    else:
        node = pipeline["FishNewGamingPro"]
        if node.get("custom_action") != "auto_fish_pro":
            errors.append("FishNewGamingPro 的 custom_action 不是 auto_fish_pro")
        if node.get("enabled") is not False:
            errors.append("FishNewGamingPro 默认应为 enabled=false")
        for follow in ("FishNewGameResult", "FishNewEscapeResult", "FishNewStart"):
            if follow not in node.get("next", []):
                errors.append(f"FishNewGamingPro 的 next 缺少 {follow}")
        if "[Anchor]FishNewErrorRestart" not in node.get("on_error", []):
            errors.append("FishNewGamingPro 缺少 on_error 恢复分支")

    hooked = pipeline.get("FishNewFishHooked", {})
    if "FishNewGamingPro" not in hooked.get("next", []):
        errors.append("FishNewFishHooked 的 next 未挂 FishNewGamingPro")
    if "FishNewGaming" not in hooked.get("next", []):
        errors.append("FishNewFishHooked 的 next 丢失原 FishNewGaming")

    legacy = pipeline.get("FishNewGaming", {})
    if legacy.get("custom_action") != "auto_fish_without_cv":
        errors.append("FishNewGaming 的默认实现被改动")
    if "enabled" in legacy:
        errors.append("FishNewGaming 不应新增 enabled 字段，默认行为须保持不变")

    tasks = parsed.get("assets/resource/tasks/Fish.json", {})
    options = tasks.get("option", {})
    for name in ("FishNewControlEngine", "FishNewProLearning", "FishNewProDebug"):
        if name not in options:
            errors.append(f"Fish.json 缺少选项 {name}")

    engine = options.get("FishNewControlEngine", {})
    if engine.get("default_case") != "Legacy":
        errors.append("FishNewControlEngine 默认档位应为 Legacy")
    case_names = [case.get("name") for case in engine.get("cases", [])]
    if case_names != ["Legacy", "Pro"]:
        errors.append(f"FishNewControlEngine 档位异常: {case_names}")
    for case in engine.get("cases", []):
        override = case.get("pipeline_override", {})
        if case.get("name") == "Legacy":
            if override.get("FishNewGaming", {}).get("enabled") is not True:
                errors.append("Legacy 档位未启用 FishNewGaming")
            if override.get("FishNewGamingPro", {}).get("enabled") is not False:
                errors.append("Legacy 档位未关闭 FishNewGamingPro")
        if case.get("name") == "Pro":
            if override.get("FishNewGaming", {}).get("enabled") is not False:
                errors.append("Pro 档位未关闭 FishNewGaming")
            if override.get("FishNewGamingPro", {}).get("enabled") is not True:
                errors.append("Pro 档位未启用 FishNewGamingPro")
            sub = case.get("option", [])
            for name in ("FishNewProLearning", "FishNewProDebug"):
                if name not in sub:
                    errors.append(f"Pro 档位未挂子选项 {name}")

    for task in tasks.get("task", []):
        if task.get("name") == "FishNew":
            if "FishNewControlEngine" not in task.get("option", []):
                errors.append("FishNew 任务未挂 FishNewControlEngine")
        if task.get("name") == "Fish":
            if "FishNewControlEngine" in task.get("option", []):
                errors.append("旧 Fish 任务不应被改动")

    return errors


def check_locales() -> list[str]:
    errors: list[str] = []
    for lang in LANGS:
        interface_path = REPO / f"assets/resource/locales/interface/{lang}.json"
        agent_path = REPO / f"assets/resource/locales/agent/{lang}.json"
        for path, keys in ((interface_path, INTERFACE_KEYS), (agent_path, AGENT_KEYS)):
            if not path.exists():
                errors.append(f"缺失 locale 文件: {path}")
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"locale 解析失败 {path.name}: {exc}")
                continue
            for key in keys:
                value = data.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{lang} 缺少或空文案: {key}")
    return errors


def main() -> int:
    errors = check_pipelines() + check_locales()
    if errors:
        print("静态校验失败:")
        for item in errors:
            print(f"  - {item}")
        return 1
    print("静态校验通过: Pipeline 节点、任务选项与 5 语言文案齐全")
    return 0


if __name__ == "__main__":
    sys.exit(main())
