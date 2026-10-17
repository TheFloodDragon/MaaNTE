"""战斗方案（scheme）加载与校验。

方案是 JSON 文件（允许 // 与 /* */ 注释）：声明配队提示、每个槽位的出招表、轮转方式。
内置方案在 resource/base/combat/schemes/，用户方案放在安装目录 config/combat/。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .dsl import Command, ComboError, compile_combo

SCHEME_VERSION = 1
MAX_FILE_BYTES = 64 * 1024
MAX_ORDER = 16
MAX_FIELD_TIME = 10.0
BUILTIN_SCHEMES = ("team_rotation", "main_dps", "solo_skill", "basic_attack")
DEFAULT_SCHEME = "team_rotation"
COMBO_KEYS = ("1", "2", "3", "4", "default")
_TOP_KEYS = {"version", "name", "description", "team", "combos", "rotation"}
_TEAM_KEYS = {"slot", "character", "note"}
_ROTATION_KEYS = {"mode", "order", "min_field_time"}


@dataclass(frozen=True)
class TeamSlot:
    slot: int
    character: str = ""
    note: str = ""


@dataclass
class CombatScheme:
    name: str
    description: str = ""
    team: list[TeamSlot] = field(default_factory=list)
    combos: dict[str, list[Command]] = field(default_factory=dict)
    mode: str = "cycle"
    order: list[int] = field(default_factory=lambda: [1, 2, 3, 4])
    min_field_time: float = 1.0
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def combo_for(self, slot: int) -> list[Command]:
        """取槽位出招表：槽位专属 → default → 轮转首位。校验已保证至少一项存在。"""
        for key in (str(slot), "default", str(self.order[0])):
            if key in self.combos:
                return self.combos[key]
        return next(iter(self.combos.values()))


class SchemeError(ValueError):
    """方案错误。code 对应 agent i18n key `combat.scheme.<code>`，combo_error 为内嵌的 DSL 错误。"""

    def __init__(self, code: str, *params, combo_error: ComboError | None = None):
        self.code, self.params, self.combo_error = code, params, combo_error
        detail = " (%s)" % combo_error if combo_error else ""
        super().__init__("%s %s%s" % (code, " ".join(map(str, params)), detail))


def strip_json_comments(text: str) -> str:
    """去掉 // 行注释与 /* */ 块注释（字符串内的不处理），保留换行以便报错行号准确。"""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            block = text[i : (n if end < 0 else end + 2)]
            out.append("\n" * block.count("\n"))
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_team(raw) -> list[TeamSlot]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SchemeError("bad_team")
    team, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or not _is_int(item.get("slot")) or not 1 <= item["slot"] <= 4:
            raise SchemeError("bad_team")
        if item["slot"] in seen:
            raise SchemeError("duplicate_team_slot", item["slot"])
        seen.add(item["slot"])
        team.append(TeamSlot(item["slot"], str(item.get("character") or ""), str(item.get("note") or "")))
    return sorted(team, key=lambda t: t.slot)


def _parse_rotation(raw) -> tuple[str, list[int], float]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SchemeError("bad_rotation")
    mode = raw.get("mode", "cycle")
    if mode not in ("cycle", "stay"):
        raise SchemeError("bad_mode", mode)
    order = raw.get("order", [1, 2, 3, 4])
    if (
        not isinstance(order, list)
        or not 1 <= len(order) <= MAX_ORDER
        or not all(_is_int(s) and 1 <= s <= 4 for s in order)
    ):
        raise SchemeError("bad_order")
    min_field = raw.get("min_field_time", 1.0)
    if isinstance(min_field, bool) or not isinstance(min_field, (int, float)) or not 0 <= min_field <= MAX_FIELD_TIME:
        raise SchemeError("bad_field_time", MAX_FIELD_TIME)
    return mode, list(order), float(min_field)


def parse_scheme(data, source: str = "") -> CombatScheme:
    if not isinstance(data, dict):
        raise SchemeError("not_object")
    if data.get("version") != SCHEME_VERSION or not _is_int(data.get("version")):
        raise SchemeError("bad_version", data.get("version"), SCHEME_VERSION)
    raw_combos = data.get("combos")
    if not isinstance(raw_combos, dict) or not raw_combos:
        raise SchemeError("no_combos")
    combos: dict[str, list[Command]] = {}
    for key, value in raw_combos.items():
        if key not in COMBO_KEYS:
            raise SchemeError("bad_combo_key", key)
        try:
            combos[key] = compile_combo(value)
        except ComboError as exc:
            raise SchemeError("combo", key, combo_error=exc) from None
    mode, order, min_field = _parse_rotation(data.get("rotation"))
    team = _parse_team(data.get("team"))  # 先校验类型，下面统计未知字段时才能安全遍历
    needed = order if mode == "cycle" else [1, 2, 3, 4]
    if "default" not in combos:
        missing = [s for s in dict.fromkeys(needed) if str(s) not in combos]
        if missing:
            raise SchemeError("missing_combo", ",".join(map(str, missing)))
    warnings = ["unknown field: %s" % k for k in data if k not in _TOP_KEYS]
    for item in data.get("team") or []:
        if isinstance(item, dict):
            warnings += ["unknown team field: %s" % k for k in item if k not in _TEAM_KEYS]
    if isinstance(data.get("rotation"), dict):
        warnings += ["unknown rotation field: %s" % k for k in data["rotation"] if k not in _ROTATION_KEYS]
    name = data.get("name")
    return CombatScheme(
        name=str(name) if name else (Path(source).stem if source else "unnamed"),
        description=str(data.get("description") or ""),
        team=team,
        combos=combos,
        mode=mode,
        order=order,
        min_field_time=min_field,
        source=source,
        warnings=warnings,
    )


def parse_scheme_text(text: str, source: str = "") -> CombatScheme:
    try:
        data = json.loads(strip_json_comments(text))
    except json.JSONDecodeError as exc:
        raise SchemeError("bad_json", exc.lineno, exc.colno) from None
    return parse_scheme(data, source)


def read_scheme_file(path: Path) -> CombatScheme:
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SchemeError("too_large", MAX_FILE_BYTES // 1024)
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SchemeError("read_failed", str(path), exc.strerror or exc) from None
    except UnicodeDecodeError:
        raise SchemeError("not_utf8", str(path)) from None
    return parse_scheme_text(text, str(path))
