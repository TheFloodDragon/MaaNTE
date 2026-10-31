"""有效方案：方案文件 + 配置覆盖（轮转、槽位出招/站场、禁用槽位、节奏）→ 校验 → 摘要。

自动运行、脚本入口和预览共用这一步。方案对象先复制再改，不污染内置方案缓存或下一场。
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, replace

from .dsl import ComboError, compile_combo
from .engine import effective_order
from .scheme import CombatScheme, SchemeError, TeamSlot
from .settings import SLOTS, CombatOptions


@dataclass(frozen=True)
class SlotPlan:
    slot: int
    enabled: bool  # False = 不主动切入（禁用或超出队伍人数）
    character: str
    note: str
    combo: str  # 实际执行的出招文本
    combo_source: str  # scheme / default / override
    field_time: float


@dataclass(frozen=True)
class CombatPlan:
    scheme: CombatScheme
    options: CombatOptions
    order: tuple[int, ...]  # 生效的轮转顺序（已去掉不可切入槽位）
    disabled_slots: frozenset[int]
    slots: tuple[SlotPlan, ...]

    @property
    def opener(self) -> int:
        """首发：轮转模式为生效顺序首位；站场模式为 0（跟随当前角色）。"""
        return self.order[0] if self.scheme.mode == "cycle" and self.order else 0

    @property
    def fingerprint(self) -> str:
        """摘要去重键：按实际生效内容计算，用户改了出招就会重新提示。"""
        parts = [
            self.scheme.source,
            self.scheme.name,
            self.scheme.mode,
            repr(self.order),
            repr(self.options.combat_speed),
            repr(self.options.disabled_actions),
            *(repr(s) for s in self.slots),
        ]
        return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _combo_text(commands) -> str:
    return ", ".join(c.text or c.name for c in commands)


def apply_overrides(scheme: CombatScheme, options: CombatOptions) -> CombatPlan:
    """把配置覆盖应用到方案副本并校验，失败抛 SchemeError（开战前）。"""
    eff = copy.deepcopy(scheme)
    eff.combo_sources = {key: "scheme" for key in eff.combos}
    if options.rotation_mode:
        eff.mode = options.rotation_mode
    if options.rotation_order:
        eff.order = list(options.rotation_order)
    if options.min_field_time is not None:
        eff.min_field_time = options.min_field_time

    team = {t.slot: t for t in eff.team}
    for slot in SLOTS:
        override = options.slot(slot)
        if override.combo:
            try:
                eff.combos[str(slot)] = compile_combo(override.combo)
            except ComboError as exc:
                raise SchemeError("slot_combo", slot, combo_error=exc) from None
            eff.combo_sources[str(slot)] = "override"
        if override.min_field_time is not None:
            eff.field_times[slot] = override.min_field_time
        if override.character or override.note:
            base = team.get(slot, TeamSlot(slot))
            team[slot] = replace(
                base,
                character=override.character or base.character,
                note=override.note or base.note,
            )
    eff.team = [team[s] for s in sorted(team)]

    disabled = frozenset(options.disabled_slots)
    excluded = disabled | frozenset(s for s in SLOTS if s > options.team_size)
    order = effective_order(eff.order, excluded)
    if eff.mode == "cycle":
        if not order:
            raise SchemeError("no_enabled_slot")
        needed = order
    else:
        needed = list(SLOTS)
    if "default" not in eff.combos:
        missing = [s for s in dict.fromkeys(needed) if str(s) not in eff.combos]
        if missing:
            raise SchemeError("missing_combo", ",".join(map(str, missing)))

    slots = []
    for slot in SLOTS:
        key = str(slot) if str(slot) in eff.combos else "default"
        commands = eff.combos.get(key) or eff.combo_for(slot)
        member = team.get(slot, TeamSlot(slot))
        slots.append(
            SlotPlan(
                slot=slot,
                enabled=slot not in excluded,
                character=member.character,
                note=member.note,
                combo=_combo_text(commands),
                combo_source="default" if key == "default" else eff.combo_sources[key],
                field_time=eff.field_time_for(slot),
            )
        )
    return CombatPlan(eff, options, tuple(order), disabled, tuple(slots))
