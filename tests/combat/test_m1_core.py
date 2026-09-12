"""M1 tests: observer, executor, sound event source (no MaaFramework needed)."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from _loader import load_combat_module

models = load_combat_module("models")
observer_mod = load_combat_module("observer")
executor_mod = load_combat_module("executor")
sound_events_mod = load_combat_module("sound_events")


def _job(succeeded: bool, result=None):
    """Mimic maa.job.Job: wait() returns self, success via .succeeded."""
    job = MagicMock()
    job.wait.return_value = job
    job.succeeded = succeeded
    job.get.return_value = result
    return job


def _controller(screencap=None, click_ok=True, key_ok=True):
    controller = MagicMock()
    controller.post_screencap.return_value = screencap or _job(True, MagicMock(size=1))
    controller.post_click.return_value = _job(click_ok)
    controller.post_click_key.return_value = _job(key_ok)
    controller.post_touch_down.return_value = _job(True)
    controller.post_touch_up.return_value = _job(True)
    controller.post_key_up.return_value = _job(True)
    return controller


def _context(controller=None, stopping=False, recognition=None):
    return SimpleNamespace(
        tasker=SimpleNamespace(controller=controller, stopping=stopping),
        run_recognition=MagicMock(return_value=recognition),
    )


class TestCombatObserver(unittest.TestCase):
    def test_missing_controller_gives_invalid_snapshot(self):
        snapshot = observer_mod.CombatObserver(SimpleNamespace(tasker=None)).observe()
        self.assertFalse(snapshot.valid)
        self.assertIn("no_controller", snapshot.evidence)

    def test_failed_screencap_gives_invalid_snapshot(self):
        ctx = _context(controller=_controller(screencap=_job(False)))
        snapshot = observer_mod.CombatObserver(ctx).observe()
        self.assertFalse(snapshot.valid)
        self.assertIn("screencap_failed", snapshot.evidence)

    def test_health_bar_hit_means_combat_true(self):
        ctx = _context(controller=_controller(), recognition=SimpleNamespace(hit=True))
        snapshot = observer_mod.CombatObserver(ctx).observe()
        self.assertTrue(snapshot.valid)
        self.assertIs(snapshot.combat, True)
        ctx.run_recognition.assert_called_once()
        self.assertEqual(ctx.run_recognition.call_args[0][0], observer_mod.ENEMY_HEALTH_BAR_NODE)

    def test_recognition_failure_is_unknown_not_false(self):
        ctx = _context(controller=_controller(), recognition=None)
        snapshot = observer_mod.CombatObserver(ctx).observe()
        self.assertTrue(snapshot.valid)
        self.assertIsNone(snapshot.combat)
        self.assertIn("combat:unknown", snapshot.evidence)

    def test_unimplemented_signals_are_unknown(self):
        ctx = _context(controller=_controller(), recognition=SimpleNamespace(hit=False))
        snapshot = observer_mod.CombatObserver(ctx).observe()
        self.assertIs(snapshot.combat, False)
        self.assertIsNone(snapshot.current_slot)
        self.assertIsNone(snapshot.skill_ready)
        self.assertIsNone(snapshot.ultimate_ready)


class TestCombatExecutor(unittest.TestCase):
    def test_blocks_when_stopping(self):
        executor = executor_mod.CombatExecutor(_context(controller=_controller(), stopping=True))
        result = executor.execute(models.ActionIntent(action="normal_attack"))
        self.assertIs(result.outcome, models.Outcome.BLOCKED)
        self.assertEqual(result.reason, "stopping")

    def test_missing_controller_fails(self):
        executor = executor_mod.CombatExecutor(_context(controller=None))
        result = executor.execute(models.ActionIntent(action="normal_attack"))
        self.assertIs(result.outcome, models.Outcome.FAILED)
        self.assertEqual(result.reason, "no_controller")

    def test_invalid_switch_slot_fails(self):
        executor = executor_mod.CombatExecutor(_context(controller=_controller()))
        result = executor.execute(models.ActionIntent(action="switch", slot=None))
        self.assertIs(result.outcome, models.Outcome.FAILED)
        self.assertEqual(result.reason, "invalid_slot")

    def test_switch_is_pending_until_confirmed(self):
        controller = _controller()
        executor = executor_mod.CombatExecutor(_context(controller=controller))
        result = executor.execute(models.ActionIntent(action="switch", slot=2))
        self.assertIs(result.outcome, models.Outcome.PENDING)
        controller.post_click_key.assert_called_once_with(executor_mod.VK_SLOT[2])

    def test_normal_attack_uses_click_xy(self):
        controller = _controller()
        executor = executor_mod.CombatExecutor(_context(controller=controller))
        result = executor.execute(models.ActionIntent(action="normal_attack"))
        self.assertIs(result.outcome, models.Outcome.SUCCEEDED)
        controller.post_click.assert_called_once_with(*executor_mod.ATTACK_POINT)

    def test_controller_failure_is_reported(self):
        executor = executor_mod.CombatExecutor(_context(controller=_controller(click_ok=False)))
        result = executor.execute(models.ActionIntent(action="normal_attack"))
        self.assertIs(result.outcome, models.Outcome.FAILED)
        self.assertEqual(result.reason, "click_failed")

    def test_charged_attack_releases_touch_when_stopped_midway(self):
        controller = _controller()
        ctx = _context(controller=controller)
        executor = executor_mod.CombatExecutor(ctx)

        def stop_after_first_chunk(*_):
            ctx.tasker.stopping = True

        controller.post_touch_down.return_value.wait.side_effect = None
        controller.post_touch_down.side_effect = lambda *a, **k: (stop_after_first_chunk(), _job(True))[1]
        result = executor.execute(models.ActionIntent(action="charged_attack", duration_ms=500))
        self.assertIs(result.outcome, models.Outcome.BLOCKED)
        controller.post_touch_up.assert_called()
        self.assertFalse(executor._touch_held)

    def test_release_all_releases_held_keys(self):
        controller = _controller()
        executor = executor_mod.CombatExecutor(_context(controller=controller))
        executor._held_keys = {0x41, 0x57}
        executor._touch_held = True
        executor.release_all()
        self.assertEqual(executor._held_keys, set())
        self.assertFalse(executor._touch_held)
        self.assertEqual(controller.post_key_up.call_count, 2)
        controller.post_touch_up.assert_called_once()


class TestSoundEventSource(unittest.TestCase):
    def test_rejects_start_when_already_running(self):
        source = sound_events_mod.SoundEventSource()
        source._running = True
        ok, reason = source.start("s", "dodge.wav", None, 0.1, 0.1, lambda: False)
        self.assertFalse(ok)
        self.assertEqual(reason, "already_running")

    def test_missing_sample_or_module_is_reported(self):
        source = sound_events_mod.SoundEventSource()
        ok, reason = source.start("s", "definitely_missing.wav", None, 0.1, 0.1, lambda: False)
        self.assertFalse(ok)
        self.assertTrue(
            reason.startswith("sample_not_found") or reason == "sound_module_unavailable",
            reason,
        )

    def test_expired_events_are_dropped(self):
        source = sound_events_mod.SoundEventSource()
        now = time.monotonic()
        source._event_queue = [
            models.SoundEvent("s", now - 1.0, now - 0.5, "dodge"),
            models.SoundEvent("s", now, now + 1.0, "dodge"),
        ]
        event = source.poll_event()
        self.assertIsNotNone(event)
        self.assertGreater(event.expires_at, now)
        self.assertIsNone(source.poll_event())

    def test_clear_events_empties_queue(self):
        source = sound_events_mod.SoundEventSource()
        now = time.monotonic()
        source._event_queue = [models.SoundEvent("s", now, now + 1.0, "dodge")]
        source.clear_events()
        self.assertIsNone(source.poll_event())

    def test_not_healthy_when_not_running(self):
        self.assertFalse(sound_events_mod.SoundEventSource().is_healthy())


if __name__ == "__main__":
    unittest.main()
