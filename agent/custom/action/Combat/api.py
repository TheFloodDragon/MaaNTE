"""战斗系统对外入口。

    from custom.action.Combat import run_combat, prepare_combat

    plan = prepare_combat(context, overrides={"combat_speed": 0.8})  # 不截图、不发输入
    result = run_combat(context, overrides={"max_duration": 120})
    result = run_combat(context, script=lambda s: s.perform("skill, ultimate", slot=2))
    if result.reason == "cleared": ...

可覆盖的参数见 settings.CombatOptions；结果语义见 model.EndReason。
"""

from __future__ import annotations

import copy
from typing import Callable, Iterable, Mapping

from utils.logger import logger

from .engine import CombatEngine, EngineConfig, Reporter
from .inputs import InputDriver
from .locate import load_scheme
from .model import CombatResult, EndReason, Phase
from .perception import SIGNAL_KINDS, SignalConfig, find_health_bars, observe_frame
from .plan import CombatPlan, apply_overrides
from .runtime import Runtime
from .scheme import CombatScheme, SchemeError, parse_scheme
from .settings import CombatOptions, collect_layers, merge_options_strict

# 识别节点见 pipeline/Combat/Combat.json。CombatEnemyVisible 是下列敌人证据的 Or，供 Pipeline 门控；
# Python 内核逐个执行子节点，分别记录证据，两边用的是同一组节点。
ENEMY_NODE = "CombatEnemyHealthBar"  # 精确颜色（与粉爪已校准参数一致）
BAR_SHAPE_NODE = "CombatEnemyBarShape"  # 容差颜色 + 形状（辅助判定）
LEVEL_NODE = "CombatEnemyLevel"  # 敌人等级文字（辅助判定）
BOSS_NODE = "CombatBossLevel"  # Boss 等级文字（辅助判定）
RETREAT_PROMPT_NODE = "CombatRetreatPrompt"  # 撤离确认界面（暂停输入）
SCRIPT_SOURCE = "<script>"
COMBO_PREVIEW = 100
STATE_THROTTLE = 3.0
_announced: set[tuple] = set()
_DEFAULTS = CombatOptions()


def _t(key: str, *args) -> str:
    """i18n 取文案；参数与模板不匹配时退回原模板，避免格式化异常打断战斗。"""
    from utils.i18n import T

    try:
        return T(key, *args)
    except (TypeError, ValueError):
        return T(key)


def display(text: str) -> str:
    """方案中的名称/备注：以 $ 开头视为 agent i18n key，否则原样显示。"""
    return _t(text[1:]) if text.startswith("$") else text


def describe_scheme_error(exc: SchemeError) -> str:
    params = exc.params
    combo = exc.combo_error
    if combo is not None:  # combat.scheme.combo 模板的最后一个参数是 DSL 错误详情
        detail = _t("combat.dsl.%s" % combo.code, *combo.params)
        if combo.line:
            detail = _t("combat.dsl.at", combo.line, combo.col) + detail
        params = params + (detail,)
    return _t("combat.scheme.%s" % exc.code, *params)


def _num(value: float) -> str:
    """1.0 → 1、0.75 → 0.75：用户消息里不显示多余的 0。"""
    return ("%.2f" % value).rstrip("0").rstrip(".")


def source_label(plan: CombatPlan) -> str:
    """方案来源：内置 / 自定义文件（只显示文件名，不暴露完整路径）/ 脚本传入。"""
    source = plan.scheme.source
    if source == SCRIPT_SOURCE:
        return _t("combat.plan.from_script")
    if plan.options.scheme == "custom":
        return _t("combat.plan.from_file", source.replace("\\", "/").rsplit("/", 1)[-1])
    return _t("combat.plan.from_builtin")


class PrintReporter(Reporter):
    """通过 maafocus 把战斗进度推送给 MXU（i18n）。"""

    def __init__(self, context, clock: Callable[[], float] | None = None):
        import time

        self.context = context
        self._clock = clock or time.monotonic
        self._last_state: dict[str, float] = {}
        self._lost = False

    def _print(self, key: str, *args) -> None:
        from utils.maafocus import Print

        Print(self.context, _t(key, *args))

    def waiting(self, scheme: CombatScheme) -> None:
        self._print("combat.waiting", display(scheme.name))

    def ended(self, result: CombatResult) -> None:
        logger.debug("combat ended: %s", result)
        if result.reason == EndReason.NO_COMBAT:
            self._print("combat.ended_no_fight")
        else:
            self._print(
                "combat.ended", _t("combat.reason.%s" % result.reason), result.duration
            )

    def plan_summary(self, plan: CombatPlan) -> None:
        """开战前输出最终会执行的内容：来源、输入节奏、轮转、每槽出招。"""
        scheme = plan.scheme
        if (
            scheme.mode == "cycle"
            and len(set(plan.order)) > 1
            and "switch" not in plan.options.disabled_actions
        ):
            rotation = _t(
                "combat.plan.cycle", "→".join(map(str, plan.order)), plan.opener
            )
        else:
            rotation = _t("combat.plan.stay")
        self._print(
            "combat.plan.header",
            display(scheme.name),
            source_label(plan),
            _num(plan.options.combat_speed),
            rotation,
        )
        for item in plan.slots:
            if not item.enabled:
                self._print("combat.plan.slot_off", item.slot)
                continue
            label = display(item.character) or _t("combat.team.any")
            if item.note:
                label = "%s (%s)" % (label, display(item.note))
            combo = item.combo
            if len(combo) > COMBO_PREVIEW:
                combo = combo[: COMBO_PREVIEW - 1] + "…"
            self._print(
                "combat.plan.slot",
                item.slot,
                label,
                _t("combat.plan.source.%s" % item.combo_source),
                combo,
                _num(item.field_time),
            )

    def state_changed(self, phase: str, snap) -> None:
        """只提示有意义的变化，并按类型节流：目标丢失确认 / 重新发现 / 撤离提示暂停。"""
        if phase == Phase.VERIFYING and not self._lost:
            self._lost = True
            key = "combat.state.verifying"
        elif phase == Phase.ACTIVE and self._lost:
            self._lost = False
            key = "combat.state.resumed"
        elif (
            phase == Phase.SUSPENDED and getattr(snap, "retreat_prompt", False) is True
        ):
            key = "combat.state.retreat_prompt"
        else:
            return
        now = self._clock()
        if now - self._last_state.get(key, float("-inf")) < STATE_THROTTLE:
            return
        self._last_state[key] = now
        self._print(key)

    def switched(self, previous: int, slot: int) -> None:
        logger.debug("combat switched %s -> %s", previous, slot)

    def slot_unavailable(self, slot: int) -> None:
        self._print("combat.slot_unavailable", slot)

    def degraded(self) -> None:
        self._print("combat.degraded")

    def started(self, scheme: CombatScheme) -> None:
        self._lost = False
        self._print("combat.started", display(scheme.name))

    def team_hint(self, scheme: CombatScheme) -> None:
        slots = []
        for slot in scheme.team:
            label = display(slot.character) or _t("combat.team.any")
            if slot.note:
                label = "%s (%s)" % (label, display(slot.note))
            slots.append(_t("combat.team.slot", slot.slot, label))
        self._print("combat.team_hint", display(scheme.name), " / ".join(slots))

    def scheme_error(self, exc: SchemeError) -> None:
        self._print("combat.scheme_error", describe_scheme_error(exc))

    def strategy_hint(self, changes) -> None:
        parts = []
        for kind, *args in changes:
            if kind == "disabled":
                args = ["/".join(_t("combat.action.%s" % action) for action in args[0])]
            parts.append(_t("combat.strategy.%s" % kind, *args))
        self._print("combat.strategy_hint", " | ".join(parts))


def strategy_changes(options: CombatOptions) -> tuple[tuple, ...]:
    """与默认值不同的战斗策略，开战前提示一次，免得用户忘了自己关过什么。"""
    changes: list[tuple] = []
    if options.team_size != _DEFAULTS.team_size:
        changes.append(("team_size", options.team_size))
    if options.disabled_actions:
        changes.append(("disabled", options.disabled_actions))
    if not options.lock_target:
        changes.append(("lock_off",))
    if options.search_turn:
        changes.append(("search_turn",))
    if not options.assist_detection:
        changes.append(("assist_off",))
    return tuple(changes)


def _announce_once(key: tuple, action) -> None:
    """同一提示每个进程只输出一次（持续模式下不刷屏）。"""
    if key not in _announced:
        _announced.add(key)
        action()


def build_engine(
    context,
    scheme: CombatScheme | CombatPlan,
    options: CombatOptions,
    reporter: Reporter,
    signal_nodes: Mapping | None = None,
) -> CombatEngine:
    plan = (
        scheme if isinstance(scheme, CombatPlan) else apply_overrides(scheme, options)
    )
    controller = context.tasker.controller

    def capture():
        try:
            return controller.post_screencap().wait().get()
        except Exception as exc:
            logger.warning("combat screencap failed: %s", exc)
            return None

    failed: set[str] = set()

    def warn_once(name, exc):
        if name not in failed:  # 同一节点反复失败只记一次
            failed.add(name)
            logger.warning("combat recognition %s failed: %s", name, exc)

    def recognize(name, image):
        if name == BAR_SHAPE_NODE:
            # 与 combat_enemy_bar 自定义识别同一实现，直接用手里的截图计算，
            # 不在 custom action 里再经 run_recognition 回调 agent。
            try:
                return bool(find_health_bars(image))
            except Exception as exc:
                warn_once(name, exc)
                return None
        try:
            detail = context.run_recognition(name, image)
        except Exception as exc:
            warn_once(name, exc)
            return None
        # 节点存在且启用时，未命中也会返回 detail(hit=False)；None 表示识别没能启动，视为未知
        return None if detail is None else bool(detail.hit)

    assist = options.assist_detection
    signals = SignalConfig(
        enemy=(ENEMY_NODE, BAR_SHAPE_NODE) if assist else (ENEMY_NODE,),
        level=(LEVEL_NODE,) if assist else (),
        boss=(BOSS_NODE,) if assist else (),
        retreat_prompt=(RETREAT_PROMPT_NODE,),
        caller=tuple(sorted((signal_nodes or {}).items())),
    )

    def observe():
        image = capture()
        snap = observe_frame(image, recognize, signals)
        if snap.valid:  # 点击落点用截图坐标系的中心
            driver.center = (int(image.shape[1]) // 2, int(image.shape[0]) // 2)
        return snap

    runtime = Runtime(observe, lambda: bool(context.tasker.stopping))
    driver = InputDriver(controller, runtime.sleep)
    config = EngineConfig(
        wait_timeout=options.wait_timeout,
        max_duration=options.max_duration,
        clear_confirm=options.clear_confirm,
        hud_lost_timeout=options.hud_lost_timeout,
        team_size=options.team_size,
        lock_target=options.lock_target,
        search_turn=options.search_turn,
        disabled=frozenset(options.disabled_actions),
        disabled_slots=plan.disabled_slots,
        speed=options.combat_speed,
    )
    return CombatEngine(runtime, driver, plan.scheme, config, reporter)


def validate_signal_nodes(get_node_data, signal_nodes) -> dict[str, tuple[str, ...]]:
    """调用方信号：{种类: [识别节点, ...]}。只调用 recognition，不执行节点 action。

    节点必须存在且配置了非 DirectHit 的识别，否则终局信号会恒为真、一开战就结束。
    """
    if signal_nodes in (None, {}):
        return {}
    if not isinstance(signal_nodes, Mapping):
        raise SchemeError("bad_signal", "signal_nodes")
    result: dict[str, tuple[str, ...]] = {}
    for kind, names in signal_nodes.items():
        if kind not in SIGNAL_KINDS:
            raise SchemeError("bad_signal", kind)
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, (list, tuple)) or not all(
            isinstance(n, str) and n.strip() for n in names
        ):
            raise SchemeError("bad_signal", kind)
        for name in names:
            try:
                data = get_node_data(name)
            except Exception:
                data = None
            reco = data.get("recognition") if isinstance(data, dict) else None
            kind_name = reco.get("type") if isinstance(reco, dict) else reco
            # 被禁用的节点 run_recognition 恒返回 None（未知），会让整场卡在不确定状态
            if (
                not kind_name
                or kind_name == "DirectHit"
                or data.get("enabled", True) is False
            ):
                raise SchemeError("bad_signal", name)
        if names:
            result[kind] = tuple(names)
    return result


def prepare_combat(
    context,
    overrides: Mapping | None = None,
    option_nodes: Iterable[str] = (),
    *,
    scheme_data: Mapping | None = None,
) -> CombatPlan:
    """合并配置、加载方案、应用槽位/轮转覆盖并校验。不截图、不发送游戏输入。

    失败抛 SchemeError（code 对应 agent i18n key combat.scheme.<code>）。
    """
    layers = collect_layers(context.get_node_data, overrides, option_nodes)
    options, warnings, errors = merge_options_strict(layers)
    for warning in warnings:
        logger.warning("%s", warning)
    if errors:
        key, value = errors[0]
        raise SchemeError("bad_option", key, value)
    if scheme_data is not None:
        scheme = parse_scheme(copy.deepcopy(dict(scheme_data)), SCRIPT_SOURCE)
    else:
        scheme = load_scheme(options.scheme, options.custom_scheme)
    for warning in scheme.warnings:
        logger.warning("combat scheme %s: %s", scheme.source, warning)
    return apply_overrides(scheme, options)


def run_combat(
    context,
    overrides: Mapping | None = None,
    option_nodes: Iterable[str] = (),
    reporter: Reporter | None = None,
    *,
    scheme_data: Mapping | None = None,
    signal_nodes: Mapping | None = None,
    script: Callable | None = None,
) -> CombatResult:
    """执行一场战斗并返回结果。

    overrides / option_nodes：覆盖全局战斗配置；scheme_data：内存中的版本 1 方案；
    signal_nodes：调用方的只读识别节点（active/cleared/disengaged/retreated/defeated/suspended）；
    script(session)：确认进战后调用一次，接管出招，见 session.CombatSession。
    """
    reporter = reporter or PrintReporter(context)
    try:
        signals = validate_signal_nodes(context.get_node_data, signal_nodes)
        plan = prepare_combat(context, overrides, option_nodes, scheme_data=scheme_data)
    except SchemeError as exc:
        logger.warning("combat preparation failed: %s", exc)
        reporter.scheme_error(exc)
        return CombatResult.of(EndReason.SCHEME_ERROR)
    scheme, options = plan.scheme, plan.options
    _announce_once(
        ("plan", plan.fingerprint), lambda: _call(reporter, "plan_summary", plan)
    )
    if scheme.team:
        _announce_once(("team", plan.fingerprint), lambda: reporter.team_hint(scheme))
    changes = strategy_changes(options)
    if changes:
        _announce_once(("strategy",) + changes, lambda: reporter.strategy_hint(changes))
    logger.debug("combat start: scheme=%s options=%s", scheme.source, options)
    return build_engine(context, plan, options, reporter, signals).run(script)


def _call(reporter, name: str, *args) -> None:
    callback = getattr(reporter, name, None)
    if callable(callback):
        callback(*args)
