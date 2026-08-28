"""列出任务定义里引用的 locale 键，并检查五语言文件是否齐全。

单独放一个脚本而不是塞进 check_combat_assets.py：任务/选项的文案缺失
不会让程序崩，只会在界面上显示 `$task_xxx_label` 这种原始键——
必须有断言兜着，否则永远是「发布之后才被用户发现」的问题。

用法：python tools/check_locale_keys.py [任务文件名...]
不给参数时检查 resource/tasks 下所有任务定义。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASKS_DIR = REPO / "assets" / "resource" / "tasks"
LOCALE_DIR = REPO / "assets" / "resource" / "locales" / "interface"

KEY_PATTERN = re.compile(r"\$([A-Za-z0-9_.]+)")


def _strip_comments(text: str) -> str:
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def load_locales() -> dict[str, dict]:
    locales = {}
    for path in sorted(LOCALE_DIR.glob("*.json")):
        locales[path.stem] = json.loads(_strip_comments(path.read_text("utf-8")))
    return locales


def collect_keys(paths) -> dict[str, set[str]]:
    result = {}
    for path in paths:
        text = path.read_text("utf-8")
        result[path.name] = set(KEY_PATTERN.findall(text))
    return result


def main(argv) -> int:
    if argv:
        paths = [TASKS_DIR / name for name in argv]
        missing_files = [p for p in paths if not p.exists()]
        if missing_files:
            for p in missing_files:
                print(f"[FAIL] 任务文件不存在: {p}")
            return 2
    else:
        paths = sorted(TASKS_DIR.glob("*.json"))

    locales = load_locales()
    if not locales:
        print(f"[FAIL] 没找到任何语言文件: {LOCALE_DIR}")
        return 2

    per_file = collect_keys(paths)
    failures = 0
    for name, keys in sorted(per_file.items()):
        gaps = []
        for lang, table in sorted(locales.items()):
            absent = sorted(k for k in keys if k not in table)
            if absent:
                gaps.append((lang, absent))
        if not gaps:
            print(f"[ OK ] {name}: {len(keys)} 个键在 {len(locales)} 种语言里齐全")
            continue
        failures += 1
        print(f"[FAIL] {name}: 缺失文案")
        for lang, absent in gaps:
            print(f"        {lang}: 缺 {len(absent)} 个")
            for key in absent:
                print(f"          - ${key}")

    print(
        f"\n共检查 {len(per_file)} 个任务文件，"
        f"{'全部通过' if failures == 0 else f'{failures} 个有缺失'}"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
