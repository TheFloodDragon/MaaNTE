"""固定种子的钓鱼控条仿真：验证有界性与明确行为，不宣称真实游戏收益。

模拟器是一个二阶光标动力学玩具模型，只用于对照新引擎与朴素短脉冲基线的
行为边界（时间加权在框率、换向次数、单段按键时长），不代表真实游戏物理，
其数值不能作为实机成功率或调参收益的证据。
"""

from dataclasses import dataclass, replace
import math
import unittest

from fish_test_support import load_fish_module

control = load_fish_module("fish_control")
params = load_fish_module("fish_params")

ROI_TOP = 43
GREEN_HEIGHT = 12
CURSOR_WIDTH = 2
CURSOR_HEIGHT = 14
ROI_LEFT = 399
ROI_RIGHT = 399 + 486


def _clip(value, lower, upper):
    return max(lower, min(upper, value))


@dataclass
class SimMetrics:
    frames: int
    total_dt: float
    inside_dt: float
    reversals: int
    max_hold_ms: float
    max_abs_error: float
    ended_finite: bool

    @property
    def time_weighted_inside(self) -> float:
        return self.inside_dt / self.total_dt if self.total_dt > 0 else 0.0


class CursorPlant:
    """二阶惯性光标：按键注入冲量，无按键时阻尼衰减。仅供确定性对照。"""

    def __init__(self, start: float, *, accel=2600.0, damping=0.80, max_v=1400.0):
        self.x = start
        self.v = 0.0
        self.accel = accel
        self.damping = damping
        self.max_v = max_v

    def step(self, key, duration_ms, dt) -> None:
        hold = min(max(0.0, duration_ms) / 1000.0, dt)
        if key == control.KEY_A:
            self.v -= self.accel * hold
        elif key == control.KEY_D:
            self.v += self.accel * hold
        self.v *= self.damping ** max(dt, 0.0)
        self.v = _clip(self.v, -self.max_v, self.max_v)
        self.x = _clip(self.x + self.v * dt, ROI_LEFT + 4, ROI_RIGHT - 4)


def target_center(kind: str, t: float) -> float:
    if kind == "static":
        return 600.0
    if kind == "uniform":
        # 在 [520, 680] 之间三角波往返，匀速无突变。
        period = 4.0
        phase = (t % period) / period
        return 520.0 + (680.0 - 520.0) * (2 * phase if phase < 0.5 else 2 * (1 - phase))
    if kind == "oscillate":
        return 600.0 + 70.0 * math.sin(2 * math.pi * t / 2.5)
    raise ValueError(kind)


def _green_box(center: float, width: float):
    return (center - width / 2.0, ROI_TOP, width, GREEN_HEIGHT)


def _cursor_box(cursor: float):
    return (cursor - CURSOR_WIDTH / 2.0, ROI_TOP, CURSOR_WIDTH, CURSOR_HEIGHT)


def simulate_engine(engine, *, kind, width, frames, dt, start_offset, drop_every=0):
    plant = CursorPlant(target_center(kind, 0.0) + start_offset)
    metrics = SimMetrics(0, 0.0, 0.0, 0, 0.0, 0.0, True)
    last_key = None
    t = 0.0
    for frame in range(1, frames + 1):
        t += dt
        center = target_center(kind, t)
        dropped = drop_every and frame % drop_every == 0
        green = None if dropped else _green_box(center, width)
        cursor_box = None if dropped else _cursor_box(plant.x)
        observation = engine.observe(green, cursor_box, t, frame_id=frame) if not dropped else None
        if observation is None:
            plant.step(None, 0.0, dt)
            if dropped:
                continue
        else:
            decision = engine.decide(observation)
            engine.remember_execution(decision)
            plant.step(decision.key, decision.duration_ms, dt)
            metrics.max_hold_ms = max(metrics.max_hold_ms, decision.duration_ms)
            if decision.key is not None:
                if last_key is not None and decision.key != last_key:
                    metrics.reversals += 1
                last_key = decision.key
        inside = abs(plant.x - center) <= width / 2.0
        metrics.frames += 1
        metrics.total_dt += dt
        metrics.inside_dt += dt if inside else 0.0
        metrics.max_abs_error = max(metrics.max_abs_error, abs(plant.x - center))
        metrics.ended_finite = metrics.ended_finite and math.isfinite(plant.x) and math.isfinite(plant.v)
    return metrics


def simulate_baseline(*, kind, width, frames, dt, start_offset, pulse_ms=22.0):
    """朴素短脉冲基线：只看当前误差方向，固定脉冲，无预测、无刹车。"""
    plant = CursorPlant(target_center(kind, 0.0) + start_offset)
    metrics = SimMetrics(0, 0.0, 0.0, 0, 0.0, 0.0, True)
    last_key = None
    t = 0.0
    deadzone = width * 0.15
    for _ in range(1, frames + 1):
        t += dt
        center = target_center(kind, t)
        error = plant.x - center
        if abs(error) <= deadzone:
            key, duration = None, 0.0
        else:
            key = control.KEY_A if error > 0 else control.KEY_D
            duration = pulse_ms
        plant.step(key, duration, dt)
        if key is not None:
            if last_key is not None and key != last_key:
                metrics.reversals += 1
            last_key = key
        inside = abs(plant.x - center) <= width / 2.0
        metrics.frames += 1
        metrics.total_dt += dt
        metrics.inside_dt += dt if inside else 0.0
        metrics.max_abs_error = max(metrics.max_abs_error, abs(plant.x - center))
        metrics.ended_finite = metrics.ended_finite and math.isfinite(plant.x)
    return metrics


class FishSimulationTests(unittest.TestCase):
    def engine(self, **changes):
        config = replace(params.FishControlConfig(), **changes) if changes else params.FishControlConfig()
        return control.FishingEngine(config)

    def test_single_pulse_never_exceeds_hard_cap_in_any_scene(self):
        for kind in ("static", "uniform", "oscillate"):
            for width in (24.0, 80.0):
                with self.subTest(kind=kind, width=width):
                    metrics = simulate_engine(
                        self.engine(), kind=kind, width=width, frames=400,
                        dt=1 / 30, start_offset=90.0,
                    )
                    self.assertTrue(metrics.ended_finite)
                    self.assertLessEqual(metrics.max_hold_ms, params.MAX_KEY_HOLD_MS)

    def test_static_target_is_captured_and_held(self):
        metrics = simulate_engine(
            self.engine(), kind="static", width=80.0, frames=500,
            dt=1 / 30, start_offset=120.0,
        )
        self.assertGreater(metrics.time_weighted_inside, 0.75)
        self.assertLess(metrics.max_abs_error, 200.0)

    def test_moving_targets_stay_bounded_without_thrashing(self):
        for kind in ("uniform", "oscillate"):
            with self.subTest(kind=kind):
                metrics = simulate_engine(
                    self.engine(), kind=kind, width=80.0, frames=600,
                    dt=1 / 30, start_offset=60.0,
                )
                self.assertTrue(metrics.ended_finite)
                self.assertGreater(metrics.time_weighted_inside, 0.5)
                # 换向次数远小于帧数，说明没有逐帧抖动。
                self.assertLess(metrics.reversals, metrics.frames * 0.35)

    def test_engine_is_not_worse_than_naive_pulse_baseline(self):
        for kind in ("static", "uniform", "oscillate"):
            with self.subTest(kind=kind):
                shared = dict(kind=kind, width=80.0, frames=600, dt=1 / 30, start_offset=90.0)
                engine_metrics = simulate_engine(self.engine(), **shared)
                baseline_metrics = simulate_baseline(**shared)
                self.assertGreaterEqual(
                    engine_metrics.time_weighted_inside,
                    baseline_metrics.time_weighted_inside - 0.05,
                )

    def test_low_frame_rate_with_dropouts_stays_bounded(self):
        metrics = simulate_engine(
            self.engine(), kind="oscillate", width=80.0, frames=400,
            dt=1 / 12, start_offset=70.0, drop_every=5,
        )
        self.assertTrue(metrics.ended_finite)
        self.assertLessEqual(metrics.max_hold_ms, params.MAX_KEY_HOLD_MS)
        self.assertLess(metrics.max_abs_error, 260.0)

    def test_narrow_target_does_not_diverge(self):
        metrics = simulate_engine(
            self.engine(), kind="uniform", width=24.0, frames=500,
            dt=1 / 30, start_offset=50.0,
        )
        self.assertTrue(metrics.ended_finite)
        self.assertLess(metrics.max_abs_error, 220.0)


if __name__ == "__main__":
    unittest.main()
