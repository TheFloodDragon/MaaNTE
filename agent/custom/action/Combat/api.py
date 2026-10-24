"""战斗系统对外入口：run_combat(context, ...)。

其他 CustomAction 直接调用即可复用整套战斗（读取全局战斗配置，可按调用方覆盖）：

    from custom.action.Combat import run_combat
    result = run_combat(context, overrides={"max_duration": 120, "use_ultimate": False})
    if not result.success: ...

可覆盖的参数见 settings.CombatOptions。
"""

from __future__ import annotations

from typing import Iterable, Mapping

from utils.logger import logger

from .engine import CombatEngine, EngineConfig, Reporter
from .inputs import InputDriver
from .locate import load_scheme
from .model import CombatResult, EndReason
from .perception import observe_frame
from .runtime import Runtime
from .scheme import CombatScheme, SchemeError
from .settings import CombatOptions, collect_layers, merge_options

ENEMY_NODE = "CombatEnemyVisible"
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


class PrintReporter(Reporter):
    """通过 maafocus 把战斗进度推送给 MXU（i18n）。"""

    def __init__(self, context):
        self.context = context

    def _print(self, key: str, *args) -> None:
        from utils.maafocus import Print

        Print(self.context, _t(key, *args))

    def waiting(self, scheme: CombatScheme) -> None:
        self._print("combat.waiting", display(scheme.name))

    def started(self, scheme: CombatScheme) -> None:
        self._print("combat.started", display(scheme.name))

    def ended(self, result: CombatResult) -> None:
        if result.reason == EndReason.NO_COMBAT:
            self._print("combat.ended_no_fight")
        else:
            self._print("combat.ended", _t("combat.reason.%s" % result.reason), result.duration)

    def slot_unavailable(self, slot: int) -> None:
        self._print("combat.slot_unavailable", slot)

    def degraded(self) -> None:
        self._print("combat.degraded")

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
    return tuple(changes)


def _announce_once(key: tuple, action) -> None:
    """同一提示每个进程只输出一次（持续模式下不刷屏）。"""
    if key not in _announced:
        _announced.add(key)
        action()


def build_engine(context, scheme: CombatScheme, options: CombatOptions, reporter: Reporter) -> CombatEngine:
    controller = context.tasker.controller

    def capture():
        try:
            return controller.post_screencap().wait().get()
        except Exception as exc:
            logger.warning("combat screencap failed: %s", exc)
            return None

    def detect_enemy(image):
        try:
            detail = context.run_recognition(ENEMY_NODE, image)
        except Exception as exc:
            logger.warning("combat enemy recognition failed: %s", exc)
            return None
        return None if detail is None else bool(detail.hit)

    def observe():
        image = capture()
        snap = observe_frame(image, detect_enemy)
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
    )
    return CombatEngine(runtime, driver, scheme, config, reporter)


def run_combat(
    context,
    overrides: Mapping | None = None,
    option_nodes: Iterable[str] = (),
    reporter: Reporter | None = None,
) -> CombatResult:
    """执行一场战斗并返回结果。overrides/option_nodes 用于调用方覆盖全局战斗配置。"""
    reporter = reporter or PrintReporter(context)
    layers = collect_layers(context.get_node_data, overrides, option_nodes)
    options, warnings = merge_options(layers)
    for warning in warnings:
        logger.warning("%s", warning)
    try:
        scheme = load_scheme(options.scheme, options.custom_scheme)
    except SchemeError as exc:
        logger.warning("combat scheme load failed: %s", exc)
        reporter.scheme_error(exc)
        return CombatResult.of(EndReason.SCHEME_ERROR)
    for warning in scheme.warnings:
        logger.warning("combat scheme %s: %s", scheme.source, warning)
    if scheme.team:
        _announce_once(("team", scheme.source, scheme.name), lambda: reporter.team_hint(scheme))
    changes = strategy_changes(options)
    if changes:
        _announce_once(("strategy",) + changes, lambda: reporter.strategy_hint(changes))
    logger.debug("combat start: scheme=%s options=%s", scheme.source, options)
    return build_engine(context, scheme, options, reporter).run()
