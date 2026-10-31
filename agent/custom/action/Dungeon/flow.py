"""刷本任务的 Pipeline 桥接逻辑：配置快照、体力闸门、40/80 领取事务、有反馈的移动、战斗调度。

本模块不导入 maa，context 只按鸭子类型使用（get_node_data / override_pipeline / override_next /
run_recognition / tasker.controller / tasker.stopping），可用假对象单测。MaaFramework 注册在 actions.py。

Pipeline 管流程，这里只做 Pipeline 表达不了的部分：
- reset：读取任务选项与副本 profile，注入该副本的识别节点，重建本次运行状态；
- gate：入场前 / 领奖前 / 再次挑战前读体力，决定继续、正常结束或失败，用 override_next 改写去向；
- enter / begin_round：只在闸门批准后点击进入；确认在关卡内后建立本轮截止时间；
- claim / confirm：用新截图复核按钮与档位后单击一次，看到结算才记账，重复确认不重计；
- walk_until：按住方向键直到识别到目标，场景丢失/截图失败/停止都会先松键；
- fight：调用战斗系统，等待与战斗共享本轮剩余时间；
- finish / fail：正常结束报成功，其余情况保留失败并汇总。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from utils.logger import logger
from utils.maafocus import PrintT

from .budget import FINISHED, Decision, FarmState

# 战斗返回这些原因时直接失败：用户停止、内部异常、方案错误
_COMBAT_ABORT = frozenset({"stopped", "error", "scheme_error"})
# 结算后余额允许比推算值高出的点数（领奖到结算之间的自然恢复）
BALANCE_TOLERANCE = 2
# 关卡内识别不到守护场景的连续帧数，超过即视为场景丢失
GUARD_MISSES = 3


@dataclass
class Session:
    state: FarmState = field(default_factory=FarmState)
    stage: str = ""
    stage_label: str = ""
    profile: dict = field(default_factory=dict)
    finish: str | None = None  # 结束原因：Decision 值或失败原因
    round_id: int = 0  # 每次确认进入关卡 +1
    round_active: bool = False  # 已确认进入关卡、尚未确认领奖
    round_deadline: float | None = None
    entering: bool = False  # 已点击进入/再次挑战，等待确认在关卡内
    approved_entry: str | None = None  # 闸门批准的入场阶段：entry / repeat
    approved_claim: tuple | None = None  # (round_id, cost)
    committed_round: int = 0  # 最近一次已记账的轮次
    committed_next: list = field(default_factory=list)  # 该轮确认时的去向，重复确认原样复用
    estimated_balance: int | None = None  # 结算后推算余额（无余额显示时供再次挑战闸门使用）
    planned: bool = False  # 已输出过本次计划


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


def _names(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _succeeded(job) -> bool:
    """等待控制器作业并返回是否成功；缺少状态属性的作业视为成功（兼容旧绑定）。"""
    job = job.wait()
    return bool(getattr(job, "succeeded", True))


def _screencap(context):
    """新截一帧；失败返回 None（调用方按未知处理，不当作识别失败继续推进）。"""
    try:
        job = context.tasker.controller.post_screencap().wait()
        if not bool(getattr(job, "succeeded", True)):
            return None
        return job.get()
    except Exception as exc:
        logger.warning("dungeon screencap failed: %s", exc)
        return None


def _detail(context, node: str, image):
    if not node or image is None:
        return None
    try:
        detail = context.run_recognition(node, image)
    except Exception as exc:
        logger.warning("dungeon recognition %s failed: %s", node, exc)
        return None
    return detail if detail is not None and getattr(detail, "hit", False) else None


def _hit(context, node: str, image) -> bool:
    return _detail(context, node, image) is not None


def _rect(box):
    if box is None:
        return None
    if isinstance(box, (list, tuple)):
        return [int(v) for v in box[:4]] if len(box) >= 4 else None
    try:
        return [int(box.x), int(box.y), int(box.w), int(box.h)]
    except AttributeError:
        return None


def _results(detail) -> list[tuple[list[int], str]]:
    """识别结果 → [(rect, text)]；优先 filtered_results，退回 best_result。"""
    if detail is None:
        return []
    items = list(getattr(detail, "filtered_results", None) or [])
    if not items and getattr(detail, "best_result", None) is not None:
        items = [detail.best_result]
    out = []
    for item in items:
        rect = _rect(getattr(item, "box", None))
        if rect is None:
            rect = _rect(getattr(detail, "box", None))
        if rect is not None:
            out.append((rect, str(getattr(item, "text", "") or "")))
    return out


def _overlap(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _center(rect):
    return rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0


def _fail(context, reason: str, key: str | None = None, *args) -> bool:
    """记录失败原因并（可选）提示用户；总是返回 False，便于 `return _fail(...)`。"""
    _session.finish = reason
    if key:
        PrintT(context, key, *args)
    logger.warning("dungeon failed: reason=%s round=%d claimed=%d", reason, _session.round_id, _session.state.claimed)
    return False


def _stopped(context) -> bool:
    if getattr(context.tasker, "stopping", False):
        _session.finish = "stopped"
        return True
    return False


def _route_next(context, node_name: str, names: list[str]) -> bool:
    try:
        return bool(context.override_next(node_name, names))
    except Exception as exc:
        logger.warning("dungeon override_next %s failed: %s", node_name, exc)
        return False


def _decision_failure(context, decision: str) -> bool:
    state = _session.state
    if decision == Decision.UNKNOWN_BALANCE:
        return _fail(context, decision, "dungeon.unknown_balance", state.claimed)
    if decision == Decision.NO_REWARD:
        return _fail(context, decision, "dungeon.no_reward")
    if decision == Decision.PENDING:
        return _fail(context, decision, "dungeon.claim_repeat")
    return _fail(context, decision or Decision.CONFIG_ERROR, "dungeon.config_error")


# ---------------------------------------------------------------- 配置与入口


def reset(context) -> bool:
    """任务入口调用：解析选项与副本 profile，注入该副本的运行节点，重建本次运行状态。"""
    global _session
    _session = Session()
    from .config import load_config

    try:
        cfg = load_config(context)
    except ValueError as exc:
        logger.warning("dungeon config invalid: %s", exc)
        return _fail(context, Decision.CONFIG_ERROR, "dungeon.config_error")
    try:
        injected = bool(context.override_pipeline(cfg.overrides))
    except Exception as exc:
        logger.warning("dungeon override_pipeline failed: %s", exc)
        injected = False
    if not injected:
        return _fail(context, Decision.CONFIG_ERROR, "dungeon.config_error")
    profile = cfg.profile
    state = FarmState(
        target=cfg.runs,
        cost=40,
        reserve=cfg.reserve,
        reward_mode=cfg.reward_mode,
        supported_costs=tuple(profile["supported_costs"]),
    )
    from utils.i18n import T

    key = "dungeon.stage.%s" % profile["id"]
    label = T(key)
    if label == key:
        label = profile["name"]
    _session = Session(state=state, stage=profile["id"], stage_label=label, profile=profile)
    target = T("dungeon.target_runs", cfg.runs) if cfg.runs else T("dungeon.target_exhaust")
    PrintT(context, "dungeon.start", label, target, T("dungeon.reward_mode.%s" % cfg.reward_mode))
    if not profile.get("calibrated"):
        PrintT(context, "dungeon.uncalibrated")
    PrintT(context, "dungeon.team_current")
    logger.debug(
        "dungeon reset: stage=%s runs=%d reserve=%d mode=%s costs=%s",
        profile["id"], cfg.runs, cfg.reserve, cfg.reward_mode, state.supported_costs,
    )
    return True


# ---------------------------------------------------------------- 余额与领奖弹窗识别

_COST_RE = re.compile(r"(?<!\d)(40|80)(?!\d)")
# 领奖弹窗识别节点的默认名；profile 通过 reset 注入同名节点，Pipeline 参数可显式覆盖。
POPUP_NODE = "DungeonRewardPopup"
BALANCE_NODE = "DungeonBalance"
OPTIONS_NODE = "DungeonRewardOptions"
BUTTON_NODES = {"40": "DungeonClaimButton", "80": "DungeonClaim80Button"}


def read_balance(context, node: str, image) -> int | None:
    """OCR 余额；读不到、格式不对或同帧出现多个不同数值时返回 None（未知）。"""
    from .budget import parse_balance

    values = {parse_balance(text) for _, text in _results(_detail(context, node, image))}
    if len(values) != 1 or None in values:
        return None
    return values.pop()


def _cost_texts(results) -> list[tuple[list[int], int]]:
    """从弹窗 OCR 中挑出成本数字：恰含一个 40/80 且不是“余额/上限”。"""
    out = []
    for rect, text in results:
        if "/" in text or "／" in text:
            continue
        found = set(_COST_RE.findall(text))
        if len(found) == 1:
            out.append((rect, int(found.pop())))
    return out


def _associate(button, costs, radius: float) -> int | None:
    """按钮 → 最近的成本数字；超出半径或两种成本几乎等距时视为无法确认。"""
    bx, by = _center(button)
    ranked = sorted(
        ((((cx - bx) ** 2 + (cy - by) ** 2) ** 0.5, cost) for cx, cy, cost in
         ((*_center(rect), cost) for rect, cost in costs)),
    )
    if not ranked or ranked[0][0] > radius:
        return None
    best_d, best = ranked[0]
    for d, cost in ranked[1:]:
        if cost != best and d <= best_d * 1.25 + 8:
            return None
    return best


def _read_popup(context, params: dict, image):
    """领奖弹窗：返回 (错误原因或 None, 余额, 可用档位, {档位: 按钮矩形})。

    按钮由各档位独立识别节点给出，每个按钮必须唯一，且最近的成本数字必须等于该档位；
    不根据左右位置推断普通/双份。
    """
    if image is None or not _hit(context, str(params.get("popup_node") or POPUP_NODE), image):
        return "no_popup", None, (), {}
    balance = read_balance(context, str(params.get("balance_node") or BALANCE_NODE), image)
    buttons = params.get("buttons") if isinstance(params.get("buttons"), dict) else dict(BUTTON_NODES)
    costs = _cost_texts(_results(_detail(context, str(params.get("options_node") or OPTIONS_NODE), image)))
    try:
        radius = float(params.get("cost_radius", 240))
    except (TypeError, ValueError):
        radius = 240.0
    found = {}
    for key, node in buttons.items():
        if str(key) not in ("40", "80"):
            return "unknown_cost", balance, (), {}
        rects = [rect for rect, _ in _results(_detail(context, str(node), image))]
        if len(rects) > 1:
            return "unknown_cost", balance, (), {}
        if not rects:
            continue
        cost = int(key)
        if _associate(rects[0], costs, radius) != cost:
            return "unknown_cost", balance, (), {}
        found[cost] = rects[0]
    if len(found) == 2 and _overlap(found[40], found[80]):
        return "unknown_cost", balance, (), {}
    return None, balance, tuple(sorted(found)), found


# ---------------------------------------------------------------- 体力闸门

_ENTRY_PHASES = ("entry", "repeat")


def _balance_against_estimate(context, node: str, image) -> tuple[bool, int | None]:
    """再次挑战前的余额：有显示就核对推算值（允许少量自然恢复），没有显示用推算值。"""
    estimate = _session.estimated_balance
    balance = read_balance(context, node, image) if node else None
    if balance is None:
        return True, estimate
    if estimate is not None and not estimate <= balance <= estimate + BALANCE_TOLERANCE:
        logger.warning("dungeon balance mismatch: read=%d estimate=%d", balance, estimate)
        return False, balance
    return True, balance


def _gate_entry(context, node_name: str, phase: str, params: dict) -> bool:
    state = _session.state
    if _session.round_active or _session.entering or state.pending or state.uncertain:
        return _fail(context, "round_state", "dungeon.round_not_started")
    if phase == "repeat" and (_session.round_id == 0 or _session.committed_round != _session.round_id):
        return _fail(context, "round_state", "dungeon.round_not_started")
    if phase == "entry" and _session.round_id != 0:
        return _fail(context, "round_state", "dungeon.round_not_started")
    image = _screencap(context)
    node = str(params.get("balance_node") or "")
    if phase == "repeat":
        ok, balance = _balance_against_estimate(context, node, image)
        if not ok:
            return _fail(context, "balance_mismatch", "dungeon.balance_mismatch")
    else:
        balance = read_balance(context, node, image)
    decision = state.decide(balance)
    _session.approved_entry = None
    if decision == Decision.CONTINUE:
        if not _session.planned:
            _session.planned = True
            PrintT(context, "dungeon.plan", balance, state.reserve, state.planned_runs)
        _session.approved_entry = phase
        return _route_next(context, node_name, _names(params.get("continue")))
    if decision in FINISHED:
        _session.finish = decision
        if not _session.planned:
            _session.planned = True
            PrintT(context, "dungeon.plan", balance, state.reserve, 0)
        return _route_next(context, node_name, _names(params.get("finish")))
    return _decision_failure(context, decision)


def _gate_claim(context, node_name: str, params: dict) -> bool:
    state = _session.state
    if not _session.round_active:
        return _fail(context, "round_state", "dungeon.round_not_started")
    if state.pending or state.uncertain:
        return _fail(context, Decision.PENDING, "dungeon.claim_repeat")
    error, balance, offered, _ = _read_popup(context, params, _screencap(context))
    if error:
        return _fail(context, error, "dungeon.unknown_cost")
    decision = state.decide(balance, offered)
    if decision == Decision.NO_REWARD and balance is not None:
        # 现场没有可用档位：若按 profile 支持的档位预算本来就不够，属于正常耗尽；否则是界面异常。
        if state.decide(balance) == Decision.EXHAUSTED:
            decision = Decision.EXHAUSTED
        else:
            decision = Decision.NO_REWARD
    _session.approved_claim = None
    if decision == Decision.CONTINUE:
        _session.approved_claim = (_session.round_id, state.selected_cost)
        PrintT(context, "dungeon.balance", balance, state.selected_cost)
        return _route_next(context, node_name, _names(params.get("continue")))
    if decision in FINISHED:
        _session.finish = decision
        return _route_next(context, node_name, _names(params.get("finish")))
    return _decision_failure(context, decision)


def gate(context, node_name: str, params: dict) -> bool:
    """消费相关的闸门。phase=entry 入场前 / repeat 再次挑战前 / claim 领奖弹窗（默认）。

    只有读到可信余额才会放行；next 由本函数按判定结果改写为 continue 或 finish。
    """
    if _stopped(context):
        return False
    if _session.finish == Decision.CONFIG_ERROR:
        return False
    phase = str(params.get("phase") or "claim")
    if phase in _ENTRY_PHASES:
        return _gate_entry(context, node_name, phase, params)
    if phase == "claim":
        return _gate_claim(context, node_name, params)
    logger.warning("dungeon gate unknown phase: %r", phase)
    return _fail(context, Decision.CONFIG_ERROR, "dungeon.config_error")


# ---------------------------------------------------------------- 入场与本轮

ROUND_DEFAULT = 600.0
ROUND_MIN = 30.0
ROUND_MAX = 3600.0


def _click(context, rect) -> bool:
    x, y = _center(rect)
    try:
        return _succeeded(context.tasker.controller.post_click(int(x), int(y)))
    except Exception as exc:
        logger.warning("dungeon click failed: %s", exc)
        return False


def enter(context, params: dict, box=None) -> bool:
    """点击「进入」/「再次挑战」。必须由同阶段闸门刚批准，批准只用一次。

    优先在新截图里重新定位 params.button_node；定位不到时才用本节点识别到的 box。
    点击只表示请求入场，入场成功由后续识别关卡 HUD 确认。
    """
    if _stopped(context):
        return False
    phase = str(params.get("phase") or "entry")
    if phase not in _ENTRY_PHASES or _session.approved_entry != phase:
        return _fail(context, "entry_unapproved", "dungeon.entry_unapproved")
    _session.approved_entry = None
    rect = None
    node = str(params.get("button_node") or "")
    if node:
        rects = [r for r, _ in _results(_detail(context, node, _screencap(context)))]
        if len(rects) > 1:
            return _fail(context, "entry_button_ambiguous", "dungeon.entry_unapproved")
        rect = rects[0] if rects else None
    if rect is None:
        rect = _rect(box)
    if rect is None:
        return _fail(context, "entry_button_lost", "dungeon.entry_unapproved")
    if not _click(context, rect):
        return _fail(context, "input_failed", "dungeon.input_failed")
    _session.entering = True
    return True


def begin_round(context, params: dict, clock=time.monotonic) -> bool:
    """看到关卡 HUD 后调用：开始新一轮并建立本轮截止时间。同一轮重复调用不重置时间。"""
    if _session.round_active:
        return True
    if not _session.entering:
        return _fail(context, "round_state", "dungeon.round_not_started")
    try:
        limit = min(max(float(params.get("round_timeout", ROUND_DEFAULT)), ROUND_MIN), ROUND_MAX)
    except (TypeError, ValueError):
        limit = ROUND_DEFAULT
    _session.entering = False
    _session.round_id += 1
    _session.round_active = True
    _session.round_deadline = clock() + limit
    _session.approved_claim = None
    logger.debug("dungeon round %d started, limit=%.0fs", _session.round_id, limit)
    return True


# ---------------------------------------------------------------- 领奖事务


def claim(context, params: dict) -> bool:
    """领取一次奖励：用新截图复核弹窗、余额、按钮与档位，和闸门批准一致才单击。

    已有待确认的领取（包括点击结果未知）时拒绝，避免重复消费。
    """
    state = _session.state
    if _stopped(context):
        return False
    if state.pending or state.uncertain:
        return _fail(context, Decision.PENDING, "dungeon.claim_repeat")
    approved = _session.approved_claim
    _session.approved_claim = None
    if not _session.round_active or approved is None or approved[0] != _session.round_id:
        return _fail(context, "claim_unapproved", "dungeon.claim_unapproved")
    error, balance, offered, rects = _read_popup(context, params, _screencap(context))
    if error:
        return _fail(context, error, "dungeon.unknown_cost")
    decision = state.decide(balance, offered)
    cost = state.selected_cost
    if decision != Decision.CONTINUE or cost != approved[1] or cost not in rects:
        return _fail(context, "claim_unapproved", "dungeon.claim_unapproved")
    if not state.begin_claim(cost):
        return _fail(context, "claim_unapproved", "dungeon.claim_unapproved")
    if not _click(context, rects[cost]):
        state.cancel_claim()  # 点击结果未知：保持锁定，绝不再点第二次
        return _fail(context, "input_failed", "dungeon.input_failed")
    return True


def confirm(context, node_name: str, params: dict) -> bool:
    """看到本轮结算后调用：记一轮，再决定再次挑战还是最终退出。

    同一轮结算被重复识别时不重计，原样复用第一次的去向；没有本任务发起的领取则拒绝。
    """
    state = _session.state
    if _session.committed_round and _session.committed_round == _session.round_id and not _session.round_active:
        return _route_next(context, node_name, list(_session.committed_next))
    if not _session.round_active or not state.pending:
        return _fail(context, "unexpected_settlement", "dungeon.settlement_unexpected")
    cost = state.pending_cost
    if not state.confirm_claim():
        return _fail(context, "unexpected_settlement", "dungeon.settlement_unexpected")
    _session.round_active = False
    _session.round_deadline = None
    _session.committed_round = _session.round_id
    _session.estimated_balance = state.last_balance
    PrintT(context, "dungeon.claimed", state.claimed, cost)
    node = str(params.get("balance_node") or "")
    ok, _ = _balance_against_estimate(context, node, _screencap(context) if node else None)
    if not ok:
        _session.committed_next = []
        return _fail(context, "balance_mismatch", "dungeon.balance_mismatch")
    if state.target and state.claimed >= state.target:
        _session.finish = Decision.DONE
        names = _names(params.get("finish"))
    else:
        names = _names(params.get("continue"))
    _session.committed_next = names
    return _route_next(context, node_name, names)


# ---------------------------------------------------------------- 结束


def _summary(context) -> None:
    state = _session.state
    PrintT(context, "dungeon.summary", state.claims_40, state.claims_80, state.spent)


def finish(context) -> bool:
    """已回到大世界后调用：只有正常结束（指定次数 / 体力用尽）才报成功。"""
    state = _session.state
    reason = _session.finish
    _summary(context)
    if reason in FINISHED:
        PrintT(context, "dungeon.finished.%s" % reason, state.claimed)
        return True
    PrintT(context, "dungeon.failed", state.claimed)
    return False


def fail(context, params: dict | None = None) -> bool:
    """on_error 汇总：保留失败语义，报告已确认的领取次数。不做任何输入。"""
    state = _session.state
    reason = str((params or {}).get("reason") or "")
    if reason:
        _session.finish = reason
        if reason == "unsafe_start":
            PrintT(context, "dungeon.unsafe_start")
    if state.pending:
        logger.warning("dungeon failed with an unconfirmed claim pending: cost=%s", state.pending_cost)
    logger.warning("dungeon fail: finish=%s claimed=%d", _session.finish, state.claimed)
    _summary(context)
    PrintT(context, "dungeon.failed", state.claimed)
    return False


# ---------------------------------------------------------------- 移动与战斗

KEY_VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44}
WALK_MAX = 60.0


def _probe(context, node: str, image) -> bool | None:
    """单帧识别：True / False；截图缺失或识别异常返回 None（未知）。"""
    if image is None:
        return None
    try:
        detail = context.run_recognition(node, image)
    except Exception as exc:
        logger.warning("dungeon probe %s failed: %s", node, exc)
        return None
    return bool(detail is not None and getattr(detail, "hit", False))


def _walk_frame(context, until: str, guard: str, aborts: list[str]) -> str:
    """一帧的移动判定：arrived / abort / lost（未知或不在守护场景）/ walking。"""
    image = _screencap(context)
    reached = _probe(context, until, image)
    if reached:
        return "arrived"
    for node in aborts:
        if _probe(context, node, image):
            return "abort"
    if reached is None:
        return "lost"
    if guard and not _probe(context, guard, image):
        return "lost"
    return "walking"


def walk_until(context, params: dict, clock=time.monotonic) -> bool:
    """按住方向键前进，每帧识别 params.until，命中即松键返回 True。

    - abort_nodes 命中：松键返回 True，把画面交还 Pipeline 识别（如加载、弹窗）；
    - guard 连续 GUARD_MISSES 帧不命中或截图/识别失败：松键失败（场景丢失不继续盲走）；
    - 超时（上限 WALK_MAX）、用户停止、输入失败：松键失败。超时只是失败边界，不做盲目纠偏。
    """
    key = str(params.get("key") or "W").upper()
    vk = KEY_VK.get(key)
    until = str(params.get("until") or "")
    guard = str(params.get("guard") or "")
    aborts = _names(params.get("abort_nodes"))
    try:
        timeout = min(max(float(params.get("timeout", 10)), 0.5), WALK_MAX)
    except (TypeError, ValueError):
        timeout = 0.0
    if vk is None or not until or timeout <= 0:
        logger.warning("dungeon walk_until invalid params: %r", params)
        return _fail(context, Decision.CONFIG_ERROR)
    if _stopped(context):
        return False
    first = _walk_frame(context, until, guard, aborts)
    if first in ("arrived", "abort"):  # 已经到位或画面已变化：不按键
        return True
    if first == "lost":
        return _fail(context, "walk_lost", "dungeon.walk_lost", guard or until)
    controller = context.tasker.controller
    try:
        # 按下的发送 / 等待本身也在保护内：即使它抛异常，也会在 finally 里补发松键。
        outcome = _hold_until(context, controller, vk, until, guard, aborts, clock() + timeout, clock)
    finally:
        try:
            released = _succeeded(controller.post_key_up(vk))
        except Exception as exc:
            logger.warning("dungeon key up failed: %s", exc)
            released = False
    if not released:
        return _fail(context, "input_failed", "dungeon.input_failed")
    if outcome == "arrived":
        return True
    if outcome == "stopped":
        return False
    if outcome == "lost":
        return _fail(context, "walk_lost", "dungeon.walk_lost", guard or until)
    if outcome == "input_failed":
        return _fail(context, "input_failed", "dungeon.input_failed")
    if outcome == "error":
        return _fail(context, "walk_error")
    return _fail(context, "walk_timeout", "dungeon.walk_timeout", until)


def _hold_until(context, controller, vk, until, guard, aborts, deadline, clock) -> str:
    """walk_until 的按键阶段，返回 arrived / stopped / lost / input_failed / error / timeout。"""
    try:
        if not _succeeded(controller.post_key_down(vk)):
            return "input_failed"
        misses = 0
        while clock() < deadline:
            if _stopped(context):
                return "stopped"
            state = _walk_frame(context, until, guard, aborts)
            if state in ("arrived", "abort"):
                return "arrived"
            if state == "lost":
                misses += 1
                if misses >= GUARD_MISSES:
                    return "lost"
            else:
                misses = 0
    except Exception as exc:
        logger.warning("dungeon walk_until failed: %s", exc)
        return "error"
    return "timeout"


def fight(context, params: dict, clock=time.monotonic, combat=None) -> bool:
    """在本轮剩余时间内调度一场战斗。等待进战与战斗共享同一截止时间，多波不重置。

    返回 True 只表示把画面交还 Pipeline 继续识别（新一波 / 奖励 / 失败画面），不代表通关。
    """
    if _stopped(context):
        return False
    if not _session.round_active or _session.round_deadline is None:
        return _fail(context, "round_state", "dungeon.round_not_started")
    remaining = _session.round_deadline - clock()
    if remaining < 1.0:
        return _fail(context, "round_timeout", "dungeon.round_timeout")
    try:
        wait = float(params.get("wait_timeout", 5))
    except (TypeError, ValueError):
        wait = 5.0
    wait = min(max(wait, 0.5), remaining)  # 0 在战斗系统里表示一直等，这里不允许
    overrides = {"wait_timeout": wait, "max_duration": max(int(remaining - wait), 1)}
    if combat is None:
        from ..Combat import run_combat as combat
    result = combat(context, overrides=overrides)
    reason = getattr(result, "reason", "error")
    logger.debug("dungeon fight result: %s remaining=%.1f", reason, remaining)
    if reason in _COMBAT_ABORT:
        return _fail(context, "combat_%s" % reason)
    if reason == "timeout" or _session.round_deadline - clock() <= 0:
        return _fail(context, "round_timeout", "dungeon.round_timeout")
    return True
