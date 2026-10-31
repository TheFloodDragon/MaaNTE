"""刷本冻结预算与领奖事务模型。纯逻辑，不依赖 maa，可直接单测。

runs 是成功领奖轮数，80 档也只计一轮。首次合法余额冻结本任务预算；
后续余额变化只能在冻结预算内影响当前可消费额度，恢复体力不会追加预算。
只有先批准、再观察到成功证据的领奖才记账；未知扣费不能直接解除锁定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_RUNS = 999
MAX_RESERVE = 999999
MAX_BALANCE = 999999
_REWARD_COSTS = (80, 40)


class Decision:
    """一次预算判定的结果。值同时用作 agent i18n key 的后缀。"""

    CONTINUE = "continue"  # 有可用档位、预算足够，且未达指定次数
    DONE = "done"  # 已确认领取指定次数（正常结束）
    EXHAUSTED = "exhausted"  # 剩余预算不足可用档位（正常结束）
    UNKNOWN_BALANCE = "unknown_balance"  # 余额无法确认（失败，不消费）
    CONFIG_ERROR = "config_error"  # 次数、保留量或档位配置无效（失败）
    NO_REWARD = "no_reward"  # 策略/profile/现场没有共同档位（失败）
    PENDING = "pending"  # 已批准的领奖仍待成功证据，禁止再次消费


# 正常结束：流程走最终退出并报告成功。
FINISHED = frozenset({Decision.DONE, Decision.EXHAUSTED})
_BALANCE_RE = re.compile(r"([0-9]{1,6})(?:\s*[/／]\s*([0-9]{1,6}))?")


def _integer_in_range(value, maximum: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum
    )


def _parse_option(value, maximum: int) -> int | None:
    if isinstance(value, str):
        digits = value.strip()
        if not re.fullmatch(r"[0-9]+", digits):
            return None
        # 先检查有效位数，避免超长字符串触发 int 转换限制/异常。
        digits = digits.lstrip("0") or "0"
        if len(digits) > len(str(maximum)):
            return None
        value = int(digits)
    return value if _integer_in_range(value, maximum) else None


def parse_runs(value) -> int | None:
    """任务选项“刷取次数”→ 0..MAX_RUNS；非法返回 None，不猜默认值。"""
    return _parse_option(value, MAX_RUNS)


def parse_reserve(value) -> int | None:
    """任务选项“保留体力”→ 0..MAX_RESERVE；非法返回 None。"""
    return _parse_option(value, MAX_RESERVE)


def parse_balance(text) -> int | None:
    """完整匹配余额或余额/正上限，不拼接被空格、逗号分开的数字。

    兼容外侧空白、斜杠周围空白与全角斜杠；不要求余额小于显示上限，
    也不把 240 当作硬上限。最多六位数字是输入边界，不是游戏体力上限。
    """
    if not isinstance(text, str):
        return None
    match = _BALANCE_RE.fullmatch(text.strip())
    if not match or (match.group(2) is not None and int(match.group(2)) == 0):
        return None
    return int(match.group(1))


def _valid_costs(costs, *, allow_empty: bool = False) -> bool:
    """只接受有限的档位集合，不消费生成器，也不将字符串/bool 转成档位。"""
    return (
        isinstance(costs, (tuple, list, set, frozenset))
        and (allow_empty or bool(costs))
        and len(costs) <= len(_REWARD_COSTS)
        and all(_integer_in_range(cost, 80) and cost in _REWARD_COSTS for cost in costs)
        and len(set(costs)) == len(costs)
    )


@dataclass
class FarmState:
    """一次刷本任务的预算与计次状态，由独立 reset 动作在任务开始时重建。"""

    target: int = 0  # 0 = 在冻结预算内刷到不足一次领取
    cost: int = 40  # 兼容旧普通领取字段，只接受 40；实际档位由 reward_mode 决定
    claimed: int = 0
    pending: bool = False
    last_balance: int | None = None
    reserve: int = 0
    reward_mode: str = "40"  # "40" / "80" / "auto"
    supported_costs: tuple[int, ...] = (40, 80)
    initial_balance: int | None = None
    budget_limit: int | None = None
    spent: int = 0
    selected_cost: int | None = None
    planned_runs: int = 0  # 当前预算/档位/剩余目标允许的未来领奖轮数，不创建轮次列表
    claims_40: int = 0
    claims_80: int = 0
    pending_cost: int | None = None
    pending_balance: int | None = None
    uncertain: bool = False
    _decision: str | None = field(default=None, init=False, repr=False)
    _approved_cost: int | None = field(default=None, init=False, repr=False)
    _approved_balance: int | None = field(default=None, init=False, repr=False)

    def _valid_config(self) -> bool:
        return (
            _integer_in_range(self.target, MAX_RUNS)
            and _integer_in_range(self.cost, 40)
            and self.cost == 40
            and _integer_in_range(self.reserve, MAX_RESERVE)
            and isinstance(self.reward_mode, str)
            and self.reward_mode in ("40", "80", "auto")
            and _valid_costs(self.supported_costs)
            and isinstance(self.pending, bool)
            and isinstance(self.uncertain, bool)
            and _integer_in_range(self.spent, MAX_BALANCE)
            and all(
                _integer_in_range(count, MAX_BALANCE // 40)
                for count in (self.claimed, self.claims_40, self.claims_80)
            )
            and all(
                value is None or _integer_in_range(value, MAX_BALANCE)
                for value in (
                    self.initial_balance,
                    self.budget_limit,
                    self.last_balance,
                )
            )
            and ((self.initial_balance is None) == (self.budget_limit is None))
        )

    def _available(self) -> int:
        if self.budget_limit is None or self.last_balance is None:
            return 0
        return max(
            min(self.budget_limit - self.spent, self.last_balance - self.reserve), 0
        )

    def decide(self, balance: int | None, offered_costs=None) -> str:
        """用最新合法余额判定档位，现场档位只能缩减 profile 支持范围。

        没有共同档位是 NO_REWARD，有共同档位但预算不足是 EXHAUSTED。
        待确认期间不更新余额快照；达到目标后无须继续识别余额。
        """
        self._decision = None
        self._approved_cost = None
        self._approved_balance = None
        self.selected_cost = None
        self.planned_runs = 0
        if not self._valid_config() or (
            offered_costs is not None
            and not _valid_costs(offered_costs, allow_empty=True)
        ):
            return Decision.CONFIG_ERROR
        if self.pending or self.uncertain:
            return Decision.PENDING
        if self.target and self.claimed >= self.target:
            return Decision.DONE
        if not _integer_in_range(balance, MAX_BALANCE):
            self.last_balance = None
            return Decision.UNKNOWN_BALANCE
        self.last_balance = balance
        if self.initial_balance is None:
            self.initial_balance = balance
            self.budget_limit = max(balance - self.reserve, 0)

        costs = tuple(
            cost
            for cost in _REWARD_COSTS
            if cost in self.supported_costs
            and (self.reward_mode == "auto" or str(cost) == self.reward_mode)
            and (offered_costs is None or cost in offered_costs)
        )
        if not costs:
            return Decision.NO_REWARD
        available = self._available()
        for cost in costs:  # 至多两个档位；auto 先 80，再用余量领取 40。
            if available >= cost and self.selected_cost is None:
                self.selected_cost = cost
            count, available = divmod(available, cost)
            self.planned_runs += count
        if self.target:
            self.planned_runs = min(self.planned_runs, self.remaining)
        if self.selected_cost is None:
            return Decision.EXHAUSTED
        self._decision = Decision.CONTINUE
        self._approved_cost = self.selected_cost
        self._approved_balance = self.last_balance
        return self._decision

    def begin_claim(self, cost: int | None = None) -> bool:
        """点击前锁定最近 CONTINUE 的档位与余额，不允许改档或重复批准。"""
        if (
            self.pending
            or self.uncertain
            or not self._valid_config()
            or self._decision != Decision.CONTINUE
            or not _integer_in_range(self.selected_cost, 80)
            or self.selected_cost != self._approved_cost
            or self.last_balance != self._approved_balance
            or self.selected_cost not in _REWARD_COSTS
            or self.selected_cost not in self.supported_costs
            or (
                self.reward_mode != "auto"
                and str(self.selected_cost) != self.reward_mode
            )
            or (
                cost is not None
                and (not _integer_in_range(cost, 80) or cost != self.selected_cost)
            )
            or (self.target and self.claimed >= self.target)
            or self._available() < self.selected_cost
        ):
            return False
        self.pending_cost = self.selected_cost
        self.pending_balance = self.last_balance
        self.pending = True
        self._decision = None
        return True

    def confirm_claim(self) -> bool:
        """成功证据到达后仅提交一次；80 档仍只增加一轮，重复确认返回 False。"""
        if (
            not self.pending
            or not _integer_in_range(self.pending_cost, 80)
            or self.pending_cost not in _REWARD_COSTS
            or not _integer_in_range(self.pending_balance, MAX_BALANCE)
            or self.pending_balance < self.pending_cost
        ):
            return False
        self.spent += self.pending_cost
        self.claimed += 1
        if self.pending_cost == 40:
            self.claims_40 += 1
        else:
            self.claims_80 += 1
        self.last_balance = self.pending_balance - self.pending_cost
        self.pending = False
        self.uncertain = False  # 迟到的成功证据也能解除未知扣费，不能靠取消解除。
        self.pending_cost = None
        self.pending_balance = None
        self.selected_cost = None
        self.planned_runs = max(self.planned_runs - 1, 0)
        self._decision = None
        return True

    def cancel_claim(self) -> None:
        """未取得成功证据时保留锁定；未知扣费不能解锁为一次新的消费。"""
        if self.pending:
            self.uncertain = True
        self._decision = None
        self.selected_cost = None
        self.planned_runs = 0

    @property
    def remaining(self) -> int | None:
        """指定次数模式下还差几轮；耗尽模式返回 None。"""
        return max(self.target - self.claimed, 0) if self.target else None
