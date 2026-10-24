"""刷本预算与计次模型。纯逻辑，不依赖 maa，可直接单测。

语义：
- runs=0 表示刷到余额不足一次普通领取为止；runs>0 表示最多确认领取 runs 次。
- 余额只有“已知非负整数”与“未知(None)”两种；未知既不当 0 也不当无限。
- 只有观察到本轮领取成功的证据才计一次；同一轮重复确认不重复累计。
- 每次需要决定“继续/停止”时都用最新读到的余额重新判断，不沿用启动时的估算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_RUNS = 999


class Decision:
    """一次预算判定的结果。值同时用作 agent i18n key 的后缀。"""

    CONTINUE = "continue"  # 余额够一次普通领取，且未达指定次数
    DONE = "done"  # 已确认领取指定次数（正常结束）
    EXHAUSTED = "exhausted"  # 余额不足一次普通领取（正常结束）
    UNKNOWN_BALANCE = "unknown_balance"  # 余额无法确认（失败，不消费）
    CONFIG_ERROR = "config_error"  # 次数或单次消耗配置无效（失败）


# 正常结束：流程走最终退出并报告成功
FINISHED = frozenset({Decision.DONE, Decision.EXHAUSTED})

_BALANCE_RE = re.compile(r"^(\d{1,6})(?:/(\d{1,6}))?$")


def parse_runs(value) -> int | None:
    """任务选项“刷取次数”→ 0..MAX_RUNS；非法返回 None（配置错误，不猜默认值）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    return number if 0 <= number <= MAX_RUNS else None


def parse_balance(text) -> int | None:
    """OCR 文本 → 当前余额。接受“120”或“120/240”（取斜杠前的当前值）。

    去掉空白与千分位逗号后必须整体匹配；有其他字符、多段数字或为空时返回 None。
    """
    if not isinstance(text, str):
        return None
    compact = re.sub(r"[\s,，]", "", text).replace("／", "/")
    match = _BALANCE_RE.match(compact)
    return int(match.group(1)) if match else None


@dataclass
class FarmState:
    """一次刷本任务的计次状态。由独立 reset 动作在任务开始时重建。"""

    target: int = 0  # 0 = 刷到余额不足
    cost: int = 0  # 单次普通领取消耗；<=0 视为未配置
    claimed: int = 0  # 已确认领取次数
    pending: bool = False  # 已点击普通领取，尚未看到成功证据
    last_balance: int | None = None

    def decide(self, balance: int | None) -> str:
        """用最新余额判断能否再领取一次。达到次数时不需要余额。"""
        if self.cost <= 0 or self.target < 0:
            return Decision.CONFIG_ERROR
        if self.target and self.claimed >= self.target:
            return Decision.DONE
        self.last_balance = balance
        if balance is None:
            return Decision.UNKNOWN_BALANCE
        if balance < self.cost:
            return Decision.EXHAUSTED
        return Decision.CONTINUE

    def begin_claim(self) -> bool:
        """点击普通领取前调用；已有待确认的领取时拒绝（防止连点重复消费）。"""
        if self.pending:
            return False
        self.pending = True
        return True

    def confirm_claim(self) -> bool:
        """观察到本轮领取成功后调用；返回是否计了这一次（重复确认返回 False）。"""
        if not self.pending:
            return False
        self.pending = False
        self.claimed += 1
        return True

    def cancel_claim(self) -> None:
        """领取未成功（弹窗消失但无成功证据等）：清除待确认标记，不计次。"""
        self.pending = False

    @property
    def remaining(self) -> int | None:
        """指定次数模式下还差几次；耗尽模式返回 None。"""
        return max(self.target - self.claimed, 0) if self.target else None
