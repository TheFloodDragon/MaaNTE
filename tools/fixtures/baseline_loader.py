"""把内核提取前的 core3 快照当作纯逻辑模块导入。

快照文件顶部 import 了 ``maa.*``，而且 ``Core3ActionHelper`` /
``PinkPawHeistCore3Path`` 依赖 MAA Context。等价性测试只关心其中的
**纯图像判定与解析逻辑**，因此这里做源码级裁剪：

1. 去掉 ``maa`` 相关 import 与 ``@AgentServer.custom_action`` 装饰的入口类；
2. 把 ``PinkPawHeistCore3Path`` 里的纯判定方法（不碰截图、不碰控制器的那些）
   抽成模块级函数，``self`` 参数丢弃。

这样做的好处是：基线逻辑仍然逐字来自快照，而不是我照抄一遍——照抄就失去
了"差分"的意义。
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

BASELINE_PATH = Path(__file__).with_name("pinkpaw_core3_baseline.py")

# 需要从 PinkPawHeistCore3Path 里提取的纯判定方法。
# 判定标准：方法体只依赖入参与模块级常量，不调用 self._screencap / self.ah。
_PURE_METHODS = (
    "_is_black_screen_in_image",
    "_is_in_team_in_image",
    "_current_char_roi_score",
    "_current_char_scores",
    "_current_char_core_scores",
    "_is_current_char_score_accepted",
    "_is_slot2_core_score_accepted",
)

# 需要保留的模块级函数与常量赋值。
_KEEP_FUNCTIONS = (
    "_is_hit",
    "_norm_key",
    "_normalize_key_sequence",
    "_parse_timing_scale",
    "_parse_interaction_pause",
    "_as_bgr_image",
    "_crop_roi",
    "_scale_roi",
    "_fast_color_match",
)


class _SelfStripper(ast.NodeTransformer):
    """把 ``self.foo(...)`` 改写成 ``foo(...)``，并删掉 ``self`` 形参。

    只处理 ``_PURE_METHODS`` 之间的互相调用（例如 ``_current_char_scores``
    会调 ``_current_char_roi_score``），其它 ``self.x`` 一律视为不纯，
    直接报错而不是静默放过——静默放过会让基线偷偷退化。
    """

    def __init__(self, allowed):
        self._allowed = set(allowed)

    def visit_Attribute(self, node):  # noqa: N802
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            if node.attr not in self._allowed:
                raise ValueError(
                    f"baseline method references impure self.{node.attr}; "
                    "refusing to build baseline"
                )
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node


def _references_forbidden(node: ast.AST) -> bool:
    """判断语句是否引用了我们刻意不导入的模块（ctypes / maa / PIL / cv2）。

    快照里 ``ULONG_PTR = ctypes.c_ulonglong if ...`` 这类赋值属于输入层，
    与图像判定无关，直接跳过。
    """
    forbidden = {"ctypes", "wintypes", "maa", "Image", "cv2", "AgentServer"}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in forbidden:
            return True
        if isinstance(sub, ast.Attribute):
            value = sub.value
            if isinstance(value, ast.Name) and value.id in forbidden:
                return True
    return False


def _build_module_source() -> str:
    tree = ast.parse(BASELINE_PATH.read_text(encoding="utf-8"))
    kept: list[ast.stmt] = []

    for node in tree.body:
        if isinstance(node, ast.Assign):
            # 模块级常量赋值：ROI、阈值表等，全部保留；输入层常量跳过。
            if not _references_forbidden(node):
                kept.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in _KEEP_FUNCTIONS:
            kept.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "PinkPawHeistCore3Path":
            stripper = _SelfStripper(_PURE_METHODS)
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in _PURE_METHODS:
                    func = stripper.visit(item)
                    func.args.args = [
                        arg for arg in func.args.args if arg.arg != "self"
                    ]
                    func.decorator_list = []
                    kept.append(func)

    missing = {name for name in _KEEP_FUNCTIONS} - {
        node.name for node in kept if isinstance(node, ast.FunctionDef)
    }
    missing |= {name for name in _PURE_METHODS} - {
        node.name for node in kept if isinstance(node, ast.FunctionDef)
    }
    if missing:
        raise ValueError(f"baseline snapshot is missing expected symbols: {missing}")

    module = ast.Module(body=kept, type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module)


def load_baseline() -> types.ModuleType:
    """返回一个只含纯逻辑的基线模块。"""
    import numpy as np

    module = types.ModuleType("pinkpaw_core3_baseline_pure")
    module.__dict__["np"] = np
    module.__dict__["Image"] = None
    module.__dict__["cv2"] = None
    exec(compile(_build_module_source(), "<baseline>", "exec"), module.__dict__)
    return module
