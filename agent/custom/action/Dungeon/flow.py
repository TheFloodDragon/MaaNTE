"""刷本任务的 Pipeline 桥接逻辑：预算闸门、领取计次、有反馈的移动、战斗调度。

本模块不导入 maa，context 只按鸭子类型使用（get_node_data / run_recognition /
override_next / tasker.controller / tasker.stopping），可用假对象单测。
MaaFramework 注册在 actions.py。

Pipeline 管流程，这里只做 Pipeline 表达不了的部分：
- reset：读选项载体，重建本次任务的计次状态；
- gate：读余额并决定继续/正常结束/失败，用 override_next 改写本节点的 next；
- claim / confirm：点击普通领取前加锁，看到领取成功证据才计一次；
- walk_until：按住方向键直到识别到目标，期间持续截图识别，任何退出路径都松键；
- fight：调用最新战斗系统，单轮总时长不因多次调度而重置；
- finish：正常结束报成功，其余情况保留失败。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from utils.logger import logger
from utils.maafocus import PrintT

from .budget import FINISHED, Decision, FarmState, parse_balance, parse_runs

RUNS_NODE = "DungeonFarm_RunsOption"
STAGE_NODE = "DungeonFarm_StageOption"
# 战斗返回这些原因时直接失败：用户停止、内部异常、方案错误
_COMBAT_ABORT = frozenset({"stopped", "error", "scheme_error"})


@dataclass
class Session:
    state: FarmState = field(default_factory=FarmState)
    stage: str = ""
    finish: str | None = None  # 结束原因：Decision 值或失败原因
    round_deadline: float | None = None


_session = Session()


def session() -> Session:
    return _session


def parse_param(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("dungeon param is not valid JSON: %r", raw)
        return {}
    return value if isinstance(value, dict) else {}


def _attach(context, name: str) -> dict:
    try:
        data = context.get_node_data(name) or {}
    except Exception as exc:
        logger.warning("dungeon read node %s failed: %s", name, exc)
        return {}
    attach = data.get("attach") if isinstance(data, dict) else None
    return dict(attach) if isinstance(attach, dict) else {}


def _positive_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _names(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


# ---------------------------------------------------------------- 计次与闸门


def reset(context) -> bool:
    """任务入口调用：读“刷取次数”与“副本”选项载体，重建计次状态。"""
    global _session
    runs_raw = _attach(context, RUNS_NODE).get("runs", 0)
    stage = _attach(context, STAGE_NODE)
    runs = parse_runs(runs_raw)
    cost = _positive_int(stage.get("cost"))
    name = str(stage.get("name") or "").strip()  # 稳定 ID，显示名走 agent i18n：dungeon.stage.<id>
    _session = Session(state=FarmState(target=runs or 0, cost=cost), stage=name)
    if runs is None or cost <= 0 or not name:
        _session.finish = Decision.CONFIG_ERROR
        logger.warning("dungeon config invalid: runs=%r stage=%r", runs_raw, stage)
        PrintT(context, "dungeon.config_error")
        return False
    from utils.i18n import T

    target = T("dungeon.target_runs", runs) if runs else T("dungeon.target_exhaust")
    PrintT(context, "dungeon.start", T("dungeon.stage.%s" % name), target, cost)
    logger.debug("dungeon reset: stage=%s runs=%d cost=%d", name, runs, cost)
    return True


def read_balance(context, node: str, image) -> int | None:
    """用余额识别节点 OCR 当前帧；读不到或不是纯数字返回 None（未知）。"""
    if not node or image is None:
        return None
    try:
        detail = context.run_recognition(node, image)
    except Exception as exc:
        logger.warning("dungeon balance recognition %s failed: %s", node, exc)
        return None
    if detail is None or not getattr(detail, "hit", False):
        return None
    best = getattr(detail, "best_result", None)
    return parse_balance(getattr(best, "text", None))


def _cached_image(context):
    try:
        return context.tasker.controller.cached_image
    except Exception as exc:
        logger.warning("dungeon cached image unavailable: %s", exc)
        return None


def _route(context, node_name: str, balance: int | None, params: dict) -> bool:
    """按最新余额判定并改写 node_name 的 next。返回 False 表示应失败停止。"""
    state = _session.state
    decision = state.decide(balance)
    logger.debug("dungeon gate: node=%s balance=%s decision=%s claimed=%d", node_name, balance, decision, state.claimed)
    if decision == Decision.CONTINUE:
        PrintT(context, "dungeon.balance", balance, state.cost)
        return bool(context.override_next(node_name, _names(params.get("continue"))))
    _session.finish = decision
    if decision in FINISHED:
        return bool(context.override_next(node_name, _names(params.get("finish"))))
    if decision == Decision.UNKNOWN_BALANCE:
        PrintT(context, "dungeon.unknown_balance", state.claimed)
    else:
        PrintT(context, "dungeon.config_error")
    return False


def gate(context, node_name: str, params: dict) -> bool:
    """消费前闸门：OCR 余额（params.balance_node）后路由到 continue / finish。"""
    balance = read_balance(context, str(params.get("balance_node") or ""), _cached_image(context))
    return _route(context, node_name, balance, params)


def claim(context, box) -> bool:
    """点击普通领取。已有未确认的领取时拒绝，避免连点重复消费。"""
    state = _session.state
    if not box or len(box) < 4:
        logger.warning("dungeon claim without target box")
        return False
    if not state.begin_claim():
        PrintT(context, "dungeon.claim_repeat")
        return False
    x, y, w, h = (int(v) for v in box[:4])
    try:
        context.tasker.controller.post_click(x + w // 2, y + h // 2).wait()
    except Exception as exc:
        state.cancel_claim()
        logger.warning("dungeon claim click failed: %s", exc)
        return False
    return True


def confirm(context, node_name: str, params: dict) -> bool:
    """看到领取成功证据后调用：计一次，然后决定再次挑战还是最终退出。

    余额来源：params.balance_node 有值时 OCR 结算画面；否则用领取前刚读到的余额减去单次消耗。
    这一步只决定是否开始下一轮，下一轮消费前 gate 仍会重新读取余额。
    """
    state = _session.state
    if not state.confirm_claim():
        PrintT(context, "dungeon.settlement_unexpected")
        _session.finish = "unexpected_settlement"
        return False
    _session.round_deadline = None
    PrintT(context, "dungeon.claimed", state.claimed)
    node = str(params.get("balance_node") or "")
    if node:
        balance = read_balance(context, node, _cached_image(context))
    elif state.last_balance is not None:
        balance = max(state.last_balance - state.cost, 0)
    else:
        balance = None
    return _route(context, node_name, balance, params)


def finish(context) -> bool:
    """最终退出确认后调用：只有正常结束（指定次数 / 余额不足）才报成功。"""
    state = _session.state
    reason = _session.finish
    if reason in FINISHED:
        PrintT(context, "dungeon.finished.%s" % reason, state.claimed)
        return True
    PrintT(context, "dungeon.failed", state.claimed)
    return False


def fail(context) -> bool:
    """on_error 汇总：保留失败语义，报告已确认的领取次数。"""
    state = _session.state
    if state.pending:
        logger.warning("dungeon failed with an unconfirmed claim pending")
    PrintT(context, "dungeon.failed", state.claimed)
    return False


# ---------------------------------------------------------------- 移动与战斗

KEY_VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44}
WALK_MAX = 60.0
ROUND_DEFAULT = 600.0
ROUND_MAX = 3600.0


def _hit(context, node: str) -> bool:
    image = context.tasker.controller.post_screencap().wait().get()
    if image is None:
        return False
    detail = context.run_recognition(node, image)
    return bool(detail is not None and getattr(detail, "hit", False))


def walk_until(context, params: dict, clock=time.monotonic) -> bool:
    """按住方向键前进，每帧识别 params.until，命中即松键返回 True。

    超时（params.timeout 秒，上限 WALK_MAX）、用户停止或任何异常都会先松键再返回 False；
    超时只是失败边界，不作为推进条件，也不做盲目纠偏。
    """
    key = str(params.get("key") or "W").upper()
    vk = KEY_VK.get(key)
    node = str(params.get("until") or "")
    try:
        timeout = min(max(float(params.get("timeout", 10)), 0.5), WALK_MAX)
    except (TypeError, ValueError):
        timeout = 0.0
    if vk is None or not node or timeout <= 0:
        logger.warning("dungeon walk_until invalid params: %r", params)
        return False
    controller = context.tasker.controller
    if _hit(context, node):  # 已经到位：不按键
        return True
    deadline = clock() + timeout
    controller.post_key_down(vk).wait()
    try:
        while clock() < deadline:
            if context.tasker.stopping:
                return False
            if _hit(context, node):
                return True
    except Exception as exc:
        logger.warning("dungeon walk_until failed: %s", exc)
        return False
    finally:
        controller.post_key_up(vk).wait()
    PrintT(context, "dungeon.walk_timeout", node)
    return False


def fight(context, params: dict, clock=time.monotonic, combat=None) -> bool:
    """调度一场战斗。单轮总时长从本轮第一次调度开始计，后续多波共享同一截止时间。

    返回 True 只表示把画面交还 Pipeline 继续识别（通关 / 新一波 / 领奖），不代表通关。
    """
    try:
        limit = min(max(float(params.get("round_timeout", ROUND_DEFAULT)), 30.0), ROUND_MAX)
    except (TypeError, ValueError):
        limit = ROUND_DEFAULT
    now = clock()
    if _session.round_deadline is None:
        _session.round_deadline = now + limit
    remaining = _session.round_deadline - now
    if remaining <= 0:
        _session.finish = "round_timeout"
        PrintT(context, "dungeon.round_timeout")
        return False
    if combat is None:
        from ..Combat import run_combat as combat
    overrides = {"max_duration": max(int(remaining), 1)}
    if params.get("wait_timeout") is not None:
        overrides["wait_timeout"] = params["wait_timeout"]
    result = combat(context, overrides=overrides)
    logger.debug("dungeon fight result: %s remaining=%.1f", result, remaining)
    if result.reason in _COMBAT_ABORT:
        _session.finish = "combat_%s" % result.reason
        return False
    if result.reason == "timeout":  # max_duration 即本轮剩余时间：本轮已超时
        _session.finish = "round_timeout"
        PrintT(context, "dungeon.round_timeout")
        return False
    return True
