"""Pipeline JSON 结构守卫：顶层键的值必须是对象。

## 这道检查守的是一个会让整个应用起不来的故障

MaaFramework 的 ``PipelineResMgr::parse_and_override_once`` 要求 pipeline
JSON 里每一个顶层键的值都是**对象**（一个节点定义）。遇到数组会报
``value is not object``，逐层上抛 ``parse_config failed`` ->
``open_and_parse_file failed`` -> ``load_all_json failed``，最终
**整个 bundle 加载失败**。

实际踩到的形态是「用数组写多行注释」：

```json
{
    "SomeMain": { "action": "Custom", ... },
    "_option_carrier_help": [
        "这里解释为什么要有下面那些载体节点……",
        "……"
    ],
    "Some_XxxOption": { "enabled": false, "attach": {} }
}
```

真实后果已用真实 MaaFramework 逐项实测（Resource + 真实 Win32 控制器）：

| | bundle 加载 | ``resource.loaded`` | ``tasker.inited`` |
|---|---|---|---|
| 干净 | true | true | **true** |
| 含数组值顶层键 | false | false | **false** |

``tasker.inited == False`` 意味着**任何任务都跑不了**——不只是这个文件里的
任务，整个 MaaNTE 起不来。所以这不是「某个任务坏了」级别的问题。

两个容易误判的细节，都已实测确认：

1. **节点未必读不到。** 解析器按键的字典序遍历，撞到坏键就返回，
   已插入的节点**不回滚**。``_`` 的 ASCII（0x5F）大于所有大写字母，
   所以 ``_option_carrier_help`` 排在最后，同文件的业务节点其实都已进了
   映射表、``get_node_data`` 照样读得到。坏键若排在前面（如 ``AAA_help``）
   则后面的节点全丢。**因此「节点读得到」不能用来证明文件是好的**，
   唯一可靠的判据是 bundle 加载状态。
2. ``json.loads`` 能正常解析这个文件。所有只做 JSON 语法校验的检查器
   都发现不了它，包括本仓库既有的那些静态检查。

多行说明请一律用 ``//`` 注释（仓库的 pipeline 是 JSONC，允许注释）。

用法：``python tools/check_pipeline_structure.py``
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from check_fishpro_assets import strip_jsonc  # noqa: E402

PIPELINE_DIR = REPO / "assets" / "resource" / "base" / "pipeline"


def check_file(path: Path) -> tuple[list[str], list[str]]:
    """返回 ``(致命错误, 提示)``。

    致命 = MaaFramework 会拒绝加载整个文件；提示 = 风格问题，MaaFramework
    实际能容忍（例如尾随逗号），不该让 CI 变红，但值得顺手清掉。
    """
    errors: list[str] = []
    notes: list[str] = []
    rel = path.relative_to(REPO).as_posix()

    raw = path.read_text(encoding="utf-8")
    stripped = strip_jsonc(raw)
    try:
        data = json.loads(stripped)
    except ValueError as exc:
        # 尾随逗号：MaaFramework 的解析器容忍，标准 json 不容忍。
        # 去掉逗号还能解析 -> 只是风格问题。
        relaxed = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            data = json.loads(relaxed)
        except ValueError:
            errors.append(f"{rel}: JSON 解析失败: {exc}")
            return errors, notes
        notes.append(f"{rel}: 存在尾随逗号（MaaFramework 容忍，建议清理）")

    if not isinstance(data, dict):
        errors.append(f"{rel}: 顶层必须是对象，实际是 {type(data).__name__}")
        return errors, notes

    for key, value in data.items():
        if isinstance(value, dict):
            continue
        kind = type(value).__name__
        hint = ""
        if isinstance(value, list):
            hint = (
                "。多行说明请改用 // 注释——数组值会让 MaaFramework 报"
                " 'value is not object' 并使整个文件的所有节点都加载失败"
            )
        errors.append(
            f"{rel}: 顶层键 {key!r} 的值是 {kind}，必须是对象（节点定义）{hint}"
        )

    return errors, notes


def main() -> int:
    print("Pipeline JSON 结构校验：顶层键的值必须是对象")
    print("=" * 68)

    files = sorted(PIPELINE_DIR.rglob("*.json"))
    if not files:
        print(f"[FAIL] 没有找到任何 pipeline JSON（{PIPELINE_DIR}）")
        return 1

    all_errors: list[str] = []
    all_notes: list[str] = []
    for path in files:
        errors, notes = check_file(path)
        all_errors.extend(errors)
        all_notes.extend(notes)

    for item in all_notes:
        print(f"  [note] {item}")

    if all_errors:
        print(f"发现 {len(all_errors)} 处致命问题：")
        for item in all_errors:
            print(f"  [FAIL] {item}")
        return 1

    print(f"全部通过：{len(files)} 个 pipeline 文件的顶层键均为对象")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
