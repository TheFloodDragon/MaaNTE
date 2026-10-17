"""引擎测试用的假时钟、假游戏世界（同时充当控制器）与记录型 Reporter。"""

from __future__ import annotations

from ._loader import submodule

VK_Q = 0x51
VK_E = 0x45
SLOT_VK = {0x31: 1, 0x32: 2, 0x33: 3, 0x34: 4}


class FakeClock:
    def __init__(self, limit: float = 900.0):
        self.t = 0.0
        self.limit = limit

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 1e-6)  # 保证时间前进，避免浮点停滞
        if self.t > self.limit:
            raise RuntimeError("fake clock limit exceeded")


class _Job:
    succeeded = True

    def wait(self):
        return self


class FakeWorld:
    """模拟游戏：按数字键延迟切人、按 Q 隐藏 HUD、敌人血条随时间变化。"""

    def __init__(
        self,
        clock: FakeClock,
        alive=(1, 2, 3, 4),
        slot=1,
        enemy=True,
        switch_delay=0.15,
        detect_slots=True,
        ult_hide=0.0,
        hud_lost_at=None,
        stop_at=None,
        hidden=(),
        manual=(),
        crash_at=None,
    ):
        self.clock = clock
        self.alive = set(alive)
        self.slot = slot
        self.enemy = enemy
        self.switch_delay = switch_delay
        self.detect_slots = detect_slots
        self.ult_hide = ult_hide
        self.hud_lost_at = hud_lost_at
        self.stop_at = stop_at
        self.hidden = list(hidden)  # [(开始, 结束)] HUD 不可见的时间段
        self.manual = sorted(manual)  # [(时间, 槽位)] 模拟用户手动切人
        self.crash_at = crash_at
        self.pending = None
        self.events: list[tuple[float, str, int]] = []
        self.captures = 0
        self._snapshot_cls = submodule("model").Snapshot

    # ---- 控制器接口 ----
    def post_key_down(self, vk):
        self.events.append((self.clock.t, "down", vk))
        if vk in SLOT_VK:
            target = SLOT_VK[vk]
            if target in self.alive and target != self.slot:
                self.pending = (target, self.clock.t + self.switch_delay)
        elif vk == VK_Q and self.ult_hide > 0:  # 终结技动画：0.1s 后 HUD 消失 ult_hide 秒
            self.hidden.append((self.clock.t + 0.1, self.clock.t + 0.1 + self.ult_hide))
        return _Job()

    def post_key_up(self, vk):
        self.events.append((self.clock.t, "up", vk))
        return _Job()

    def post_click(self, x, y, contact=0):
        if self.crash_at is not None and self.clock.t >= self.crash_at:
            raise RuntimeError("simulated controller crash")
        self.events.append((self.clock.t, "click", contact))
        return _Job()

    def post_touch_down(self, x, y, contact=0):
        self.events.append((self.clock.t, "tdown", contact))
        return _Job()

    def post_touch_up(self, contact=0):
        self.events.append((self.clock.t, "tup", contact))
        return _Job()

    # ---- 感知 ----
    def hud_visible(self, t: float) -> bool:
        if self.hud_lost_at is not None and t >= self.hud_lost_at:
            return False
        return not any(start <= t < end for start, end in self.hidden)

    def observe(self):
        t = self.clock.t
        self.captures += 1
        while self.manual and t >= self.manual[0][0]:
            self.slot = self.manual.pop(0)[1]
        if self.pending and t >= self.pending[1]:
            self.slot, self.pending = self.pending[0], None
        hud = self.hud_visible(t)
        enemy = self.enemy(t) if callable(self.enemy) else self.enemy
        return self._snapshot_cls(
            at=0.0,
            valid=True,
            in_hud=hud,
            black=False,
            slot=self.slot if (hud and self.detect_slots) else 0,
            enemy=enemy if hud else None,
        )

    def is_stopping(self) -> bool:
        return self.stop_at is not None and self.clock.t >= self.stop_at

    # ---- 断言辅助 ----
    def downs(self) -> list[tuple[float, int]]:
        return [(t, v) for (t, kind, v) in self.events if kind == "down"]

    def slot_presses(self) -> list[int]:
        return [SLOT_VK[v] for (_, v) in self.downs() if v in SLOT_VK]

    def count(self, kind: str, value=None) -> int:
        return sum(1 for (_, k, v) in self.events if k == kind and (value is None or v == value))


class RecordingReporter:
    def __init__(self):
        self.calls: list[tuple] = []

    def waiting(self, scheme):
        self.calls.append(("waiting",))

    def started(self, scheme):
        self.calls.append(("started",))

    def ended(self, result):
        self.calls.append(("ended", result.reason))

    def slot_unavailable(self, slot):
        self.calls.append(("unavailable", slot))

    def degraded(self):
        self.calls.append(("degraded",))


def make_engine(scheme: dict, config: dict | None = None, **world_kwargs):
    """返回 (engine, world, clock, reporter)。scheme 为方案 dict。"""
    runtime = submodule("runtime")
    inputs = submodule("inputs")
    engine_mod = submodule("engine")
    scheme_mod = submodule("scheme")
    clock = FakeClock()
    world = FakeWorld(clock, **world_kwargs)
    rt = runtime.Runtime(world.observe, world.is_stopping, clock=clock.now, sleeper=clock.sleep)
    driver = inputs.InputDriver(world, rt.sleep)
    cfg = engine_mod.EngineConfig(**{"wait_timeout": 5.0, "max_duration": 20.0, **(config or {})})
    reporter = RecordingReporter()
    engine = engine_mod.CombatEngine(rt, driver, scheme_mod.parse_scheme(scheme), cfg, reporter)
    return engine, world, clock, reporter
