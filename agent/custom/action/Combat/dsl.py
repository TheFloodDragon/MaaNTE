"""出招表 DSL 编译器，语法兼容 ok-NTE 出招表的子集。

    l_click(0.5), jump          # 逗号顺序执行，# 后为注释
    ultimate, skill, l_click(1.5)
    walk(w, 0.3), dodge

用 ast 解析后只接受白名单节点，参数只走 literal_eval（裸标识符当作字符串），
不存在任何回退到对象属性/方法的路径。条件分支、循环、赋值等暂不支持，编译期报错。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from .keys import ALLOWED_KEYS, DIRECTIONS, MOUSE_CONTACT

MAX_COMMANDS = 200
MAX_SECONDS = 10.0
_REQUIRED = object()
_PUNCT = str.maketrans({"，": ",", "（": "(", "）": ")", "：": ":"})


@dataclass(frozen=True)
class Command:
    name: str
    args: tuple
    line: int = 0
    col: int = 0
    text: str = ""


class ComboError(ValueError):
    """编译错误。code 对应 agent i18n key `combat.dsl.<code>`，params 为格式化参数。"""

    def __init__(self, code: str, line: int = 0, col: int = 0, *params):
        self.code, self.line, self.col, self.params = code, line, col, params
        where = "line %d col %d: " % (line, col) if line else ""
        super().__init__("%s%s %s" % (where, code, " ".join(map(str, params))))


# 参数：(名称, 类型, 默认值)。类型 sec=秒(>=0) pos=秒(>0) key/button/dir/slot
_SPECS: dict[str, tuple[tuple[str, str, object], ...]] = {
    "skill": (("seconds", "sec", 0.0),),
    "ultimate": (),
    "arc": (),
    "l_click": (("seconds", "sec", 0.0),),
    "r_click": (("seconds", "sec", 0.0),),
    "l_hold": (("seconds", "pos", 0.6),),
    "r_hold": (("seconds", "pos", 0.3),),
    "wait": (("seconds", "pos", _REQUIRED),),
    "jump": (),
    "dodge": (),
    "walk": (("direction", "dir", _REQUIRED), ("seconds", "pos", _REQUIRED)),
    "switch": (("slot", "slot", _REQUIRED),),
    "keydown": (("key", "key", _REQUIRED),),
    "keyup": (("key", "key", _REQUIRED),),
    "keypress": (("key", "key", _REQUIRED),),
    "mousedown": (("button", "button", "left"),),
    "mouseup": (("button", "button", "left"),),
    "click": (("button", "button", "left"),),
}
_ALIASES = {"attack": "l_click", "heavy": "l_hold"}
COMMANDS = frozenset(_SPECS) | frozenset(_ALIASES)


def _pos(node) -> tuple[int, int]:
    return getattr(node, "lineno", 0), getattr(node, "col_offset", 0) + 1


def _literal(node, cmd: str, param: str):
    if isinstance(node, ast.Name):
        return node.id
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        raise ComboError("bad_arg", *_pos(node), cmd, param) from None


def _coerce(kind: str, value, cmd: str, param: str, node):
    def bad():
        return ComboError("bad_value", *_pos(node), cmd, param, value)

    if kind in ("sec", "pos"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise bad()
        if value < 0 or value > MAX_SECONDS or (kind == "pos" and value <= 0):
            raise ComboError("out_of_range", *_pos(node), cmd, param, value, MAX_SECONDS)
        return float(value)
    if kind == "slot":
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4:
            raise bad()
        return value
    if isinstance(value, int) and not isinstance(value, bool) and kind == "key":
        value = str(value)
    if not isinstance(value, str):
        raise bad()
    value = value.strip().lower()
    allowed = {"key": ALLOWED_KEYS, "button": MOUSE_CONTACT, "dir": DIRECTIONS}[kind]
    if value not in allowed:
        raise bad()
    return value


def _command(node, source: str) -> Command:
    if isinstance(node, ast.Name):
        func, pos_args, kw_args = node.id, [], []
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        func, pos_args, kw_args = node.func.id, node.args, node.keywords
    else:
        raise ComboError("unsupported_syntax", *_pos(node), type(node).__name__)
    name = _ALIASES.get(func, func)
    spec = _SPECS.get(name)
    if spec is None:
        raise ComboError("unknown_command", *_pos(node), func)
    if len(pos_args) > len(spec):
        raise ComboError("too_many_args", *_pos(node), func, len(spec))
    values: dict[str, tuple[object, object]] = {}
    for arg, (pname, _, _) in zip(pos_args, spec):
        values[pname] = (_literal(arg, func, pname), arg)
    names = [p[0] for p in spec]
    for kw in kw_args:
        if kw.arg is None or kw.arg not in names:
            raise ComboError("unknown_param", *_pos(kw.value), func, kw.arg or "**")
        if kw.arg in values:
            raise ComboError("duplicate_param", *_pos(kw.value), func, kw.arg)
        values[kw.arg] = (_literal(kw.value, func, kw.arg), kw.value)
    args = []
    for pname, kind, default in spec:
        if pname not in values:
            if default is _REQUIRED:
                raise ComboError("missing_arg", *_pos(node), func, pname)
            args.append(default)
            continue
        value, vnode = values[pname]
        args.append(_coerce(kind, value, func, pname, vnode))
    text = ast.get_source_segment(source, node) or func
    return Command(name, tuple(args), *_pos(node), text)


def _normalize(source) -> str:
    if isinstance(source, (list, tuple)):
        if not all(isinstance(line, str) for line in source):
            raise ComboError("not_text")
        source = "\n".join(source)
    if not isinstance(source, str):
        raise ComboError("not_text")
    lines = source.translate(_PUNCT).replace("\r\n", "\n").split("\n")
    return "\n".join(line.lstrip() for line in lines)


def compile_combo(source) -> list[Command]:
    """编译出招表（字符串或字符串数组）为指令列表，失败抛 ComboError。"""
    text = _normalize(source)
    try:
        tree = ast.parse(text, mode="exec")
    except (SyntaxError, ValueError, RecursionError) as exc:
        line = getattr(exc, "lineno", 0) or 0
        raise ComboError("syntax", line, getattr(exc, "offset", 0) or 0) from None
    commands: list[Command] = []
    for stmt in tree.body:
        if not isinstance(stmt, ast.Expr):
            raise ComboError("unsupported_statement", *_pos(stmt), type(stmt).__name__)
        nodes = stmt.value.elts if isinstance(stmt.value, ast.Tuple) else [stmt.value]
        for node in nodes:
            commands.append(_command(node, text))
            if len(commands) > MAX_COMMANDS:
                raise ComboError("too_many_commands", 0, 0, MAX_COMMANDS)
    if not commands:
        raise ComboError("empty")
    return commands
