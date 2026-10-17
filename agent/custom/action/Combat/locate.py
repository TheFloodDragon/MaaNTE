"""方案文件查找：内置方案、示例方案与用户方案目录。

agent 运行时 cwd 是 interface.json 所在目录（开发模式为 assets/，发布包为安装根目录），
因此同时检查 cwd 与 agent 所在项目根，兼容两种布局。
"""

from __future__ import annotations

from pathlib import Path

from .scheme import BUILTIN_SCHEMES, CombatScheme, SchemeError, read_scheme_file

_AGENT_ROOT = Path(__file__).resolve().parents[4]  # .../agent/custom/action/Combat → 项目根


def _unique(paths) -> list[Path]:
    seen, result = set(), []
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def resource_base_dirs() -> list[Path]:
    cwd = Path.cwd()
    return _unique(
        [
            cwd / "resource" / "base",
            cwd / "assets" / "resource" / "base",
            _AGENT_ROOT / "assets" / "resource" / "base",
            _AGENT_ROOT / "resource" / "base",
        ]
    )


def builtin_dirs() -> list[Path]:
    return [base / "combat" / "schemes" for base in resource_base_dirs()]


def example_dirs() -> list[Path]:
    return [base / "combat" / "examples" for base in resource_base_dirs()]


def user_dirs() -> list[Path]:
    """用户方案目录：安装目录下的 config/combat/。"""
    # 发布包：cwd 即安装根目录；开发模式：cwd 为 assets/，项目根由 agent 位置推出
    return _unique([Path.cwd() / "config" / "combat", _AGENT_ROOT / "config" / "combat"])


def load_builtin(name: str) -> CombatScheme:
    if name not in BUILTIN_SCHEMES:
        raise SchemeError("unknown_builtin", name)
    for directory in builtin_dirs():
        path = directory / ("%s.json" % name)
        if path.is_file():
            return read_scheme_file(path)
    raise SchemeError("builtin_missing", name)


def resolve_user_file(value: str) -> tuple[Path | None, list[Path]]:
    """按 绝对路径 → 用户目录 → 示例目录 查找方案文件，返回 (命中路径, 已搜索目录)。"""
    name = (value or "").strip().strip('"').strip("'").strip()
    if not name:
        return None, []
    path = Path(name).expanduser()
    if not path.suffix:
        path = path.with_suffix(".json")
    if path.is_absolute():
        return (path if path.is_file() else None), [path.parent]
    searched = user_dirs() + example_dirs()
    for directory in searched:
        candidate = directory / path
        if candidate.is_file():
            return candidate, searched
    return None, searched


def load_custom(value: str) -> CombatScheme:
    name = (value or "").strip()
    if not name:
        raise SchemeError("custom_empty")
    path, searched = resolve_user_file(name)
    if path is None:
        raise SchemeError("custom_not_found", name, " | ".join(str(d) for d in searched))
    if path.suffix.lower() != ".json":
        raise SchemeError("custom_not_json", path.name)
    return read_scheme_file(path)


def load_scheme(scheme: str, custom_scheme: str = "") -> CombatScheme:
    """scheme 为内置方案名或 "custom"；custom 时从 custom_scheme 指定的文件加载。"""
    if scheme == "custom":
        return load_custom(custom_scheme)
    return load_builtin(scheme)
