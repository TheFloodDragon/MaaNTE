"""测试加载器：不导入 maa、不触发 agent 日志落盘，直接加载 custom.action.Dungeon 的纯逻辑模块。

用法（仓库根目录）：python -m unittest discover -s tests/dungeon -t .
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
ACTION_DIR = AGENT / "custom" / "action"
RESOURCE_BASE = ROOT / "assets" / "resource" / "base"

PRINTED: list[str] = []  # maafocus.PrintT 桩收到的消息


def _module(name: str, path: Path | None = None) -> types.ModuleType:
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


def _install_stubs() -> None:
    if "custom.action" in sys.modules:  # 与 tests/combat 共用同一套桩
        return
    utils = _module("utils", AGENT / "utils")
    logger_mod = _module("utils.logger")
    test_logger = logging.getLogger("maante.test.dungeon")
    test_logger.addHandler(logging.NullHandler())
    test_logger.propagate = False
    logger_mod.logger = test_logger
    logger_mod.get_logger = logging.getLogger
    i18n = _module("utils.i18n")
    i18n.T = lambda key, *args: "%s|%s" % (key, "|".join(map(str, args))) if args else key
    maafocus = _module("utils.maafocus")
    maafocus.Print = lambda context, text: PRINTED.append(text)
    maafocus.PrintT = lambda context, key, *args: PRINTED.append(i18n.T(key, *args))
    utils.logger, utils.i18n, utils.maafocus = logger_mod, i18n, maafocus
    # 只注册包路径，不执行 custom/__init__.py 与 action/__init__.py（它们会导入全部依赖 maa 的动作）
    _module("custom", AGENT / "custom")
    _module("custom.action", ACTION_DIR)


def submodule(name: str):
    """返回 custom.action.Dungeon.<name>，不执行 Dungeon/__init__.py。"""
    _install_stubs()
    if "custom.action.Dungeon" not in sys.modules:
        _module("custom.action.Dungeon", ACTION_DIR / "Dungeon")
    return importlib.import_module("custom.action.Dungeon.%s" % name)
