"""可中断等待机制。

从 ``pinkpaw_core3.sleep`` / ``next_frame`` / ``wait_until`` 提取为纯函数，
把"机制"与"策略"分离：core3 原本在一次 sleep 里轮询了 6 件事，其中

- 停止检查、切人异步确认、尾部忙等、按键时序敏感判定 -> 属于内核机制；
- 顺手捡保险箱、还在劫案里吗、交互监听 -> 属于业务策略，由钩子注入。

调用顺序与 core3 逐行对齐（见 ``interruptible_sleep`` 注释），
以保证切人确认这种"由 sleep 驱动的异步过程"时序不变。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .constants import (
    MAX_ROUTE_SLEEP_ADJUST,
    ROUTE_REWARD_CHECK_MIN_SLEEP,
    ROUTE_SLEEP_ADJUST_RATIO_CAP,
    ROUTE_SLEEP_BUSY_WAIT,
    ROUTE_SLEEP_POLL_INTERVAL,
    WAIT_UNTIL_POLL_INTERVAL,
)
from .errors import AbortException


@dataclass(frozen=True)
class PollInfo:
    """一次轮询的上下文，交给业务钩子决定要不要干活。

    - ``timing_sensitive``：当前按住了走位/冲刺/跳跃等对时序敏感的键，
      此时不应插入额外识别，否则会拖慢按键节奏。
    - ``allow_slow``：本次 sleep 足够长，允许跑低频的重识别。
    """

    timing_sensitive: bool
    allow_slow: bool


def scale_duration(duration: float, timing_scale: float) -> float:
    """按 timing_scale 对等待时长做小幅自适应修正。"""
    if duration <= 0 or timing_scale == 1.0:
        return max(duration, 0.0)

    wanted_adjust = duration * abs(1.0 - timing_scale)
    adaptive_cap = min(
        MAX_ROUTE_SLEEP_ADJUST,
        max(0.02, duration * ROUTE_SLEEP_ADJUST_RATIO_CAP),
    )
    adjust = min(wanted_adjust, adaptive_cap)
    if timing_scale < 1.0:
        return max(0.0, duration - adjust)
    return duration + adjust


def interruptible_sleep(
    duration,
    *,
    is_stopping,
    raise_if_stopped,
    poll_early=None,
    poll_kernel=None,
    poll_late=None,
    is_timing_sensitive=None,
    allow_slow_poll=True,
    monotonic=time.perf_counter,
    sleeper=time.sleep,
):
    """保持时间精度的可中断等待。

    调用顺序与 ``pinkpaw_core3.sleep`` 逐行一致：

    1. ``raise_if_stopped()``
    2. ``poll_early``       —— core3 的 ``_poll_quick_pick``
    3. ``poll_kernel``      —— core3 的 ``_poll_character_switch``
    4. 计算 ``timing_sensitive``
    5. ``poll_late``        —— core3 的局内检测 + 交互监听
    6. 让出 CPU，直到进入尾部忙等窗口
    7. 尾部忙等（只检查停止），保证到点精度
    8. 收尾再跑一次 ``poll_early`` + ``poll_kernel``
    """
    duration = max(float(duration), 0.0)
    target = monotonic() + duration
    busy_from = target - ROUTE_SLEEP_BUSY_WAIT
    allow_slow = bool(allow_slow_poll) and duration >= ROUTE_REWARD_CHECK_MIN_SLEEP
    while monotonic() < busy_from:
        raise_if_stopped()
        if poll_early is not None:
            poll_early()
        if poll_kernel is not None:
            poll_kernel()
        timing_sensitive = bool(
            is_timing_sensitive() if is_timing_sensitive is not None else False
        )
        if poll_late is not None:
            poll_late(
                PollInfo(timing_sensitive=timing_sensitive, allow_slow=allow_slow)
            )
        remaining = busy_from - monotonic()
        sleeper(max(0.0, min(ROUTE_SLEEP_POLL_INTERVAL, remaining)))
    while monotonic() < target:
        if is_stopping():
            raise_if_stopped()
    if poll_early is not None:
        poll_early()
    if poll_kernel is not None:
        poll_kernel()
    return True


def wait_until(
    condition,
    *,
    sleep,
    raise_if_stopped,
    time_out=0,
    pre_action=None,
    post_action=None,
    settle_time=-1,
    raise_if_not_found=False,
    monotonic=time.monotonic,
):
    """通用轮询等待函数，可在每轮检测前后插入动作并要求稳定命中。

    ``sleep`` 必须是可中断等待（通常来自 ``CombatKernel.sleep``），
    这样等待过程中依然会推进切人确认与业务钩子。
    """
    timeout = 10.0 if not time_out or time_out <= 0 else float(time_out)
    deadline = monotonic() + timeout
    settled_at = None
    while monotonic() < deadline:
        raise_if_stopped()
        if pre_action is not None:
            pre_action()
        found = bool(condition())
        if found:
            if post_action is not None:
                post_action()
            if settle_time is not None and settle_time >= 0:
                if settled_at is None:
                    settled_at = monotonic()
                if monotonic() - settled_at >= settle_time:
                    return True
            else:
                return True
        else:
            settled_at = None
        sleep(WAIT_UNTIL_POLL_INTERVAL)
    if raise_if_not_found:
        raise AbortException("timeout for wait_until")
    return False
