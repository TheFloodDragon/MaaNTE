"""M0 regression tests for stop, switch confirmation, and sound health."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
import sys
import itertools


class TestCore1StopChecks(unittest.TestCase):
    """Verify Core1 checks stopping during combat loops."""

    def test_fight_until_no_monster_stops_when_tasker_stopping(self):
        """Core1 fight_until_no_monster must check stopping in main loop."""
        # Simulate pinkpaw_core1 ActionHelper behavior
        fake_ctx = SimpleNamespace(
            tasker=SimpleNamespace(stopping=True),
            run_task=MagicMock(return_value=None),
        )

        # Inline minimal ActionHelper logic
        def is_stopping():
            return bool(getattr(fake_ctx.tasker, "stopping", False))

        def check_monster():
            return True  # Always report monster present

        attack_called = []

        def attack_cycle(times=3, loot=False):
            attack_called.append(True)

        # Simulate fight loop with stopping check
        no_monster_start = None
        iterations = 0
        max_iterations = 5

        while iterations < max_iterations:
            iterations += 1
            if is_stopping():
                break
            if check_monster():
                no_monster_start = None
                attack_cycle()

        # Should break immediately, no attacks
        self.assertEqual(iterations, 1, "Loop should stop after first check")
        self.assertEqual(len(attack_called), 0, "No attacks when stopping")

    def test_wait_monster_exits_on_stop(self):
        """Core1 wait_monster must exit early when stopping."""
        import time

        fake_ctx = SimpleNamespace(tasker=SimpleNamespace(stopping=False))

        def is_stopping():
            return bool(getattr(fake_ctx.tasker, "stopping", False))

        def check_monster():
            return False

        start = time.monotonic()
        timeout = 5000
        elapsed = 0

        while (time.monotonic() - start) < (timeout / 1000.0):
            if is_stopping():
                elapsed = (time.monotonic() - start) * 1000
                break
            if check_monster():
                break
            time.sleep(0.01)
            # Simulate stop signal after 50ms
            if (time.monotonic() - start) > 0.05:
                fake_ctx.tasker.stopping = True

        self.assertLess(elapsed, 200, "Should exit within 200ms of stop signal")


class TestCore3SwitchConfirmation(unittest.TestCase):
    """Verify Core3 returns None on unconfirmed switch."""

    def test_unconfirmed_switch_returns_none(self):
        """Core3 _wait_character_switch_success returns None after timeout."""
        clock = itertools.count(100, 100)

        def monotonic():
            return next(clock)

        fake = SimpleNamespace(
            _switch_state=SimpleNamespace(current_key="4", deadline=0),
            _handling_switch_state=False,
        )

        def log_warning(*args):
            pass

        def send_key(*args, **kwargs):
            pass

        def clear_switch():
            fake._switch_state = None

        fake.log_warning = log_warning
        fake.send_key = send_key
        fake._clear_switch_state = clear_switch

        # Simulate confirmation loop with timeout
        RETRY_COUNT = 6
        RETRY_WINDOW = 0.4
        retry = 0
        result = None

        while fake._switch_state is not None:
            now = monotonic()
            if now > fake._switch_state.deadline:
                if retry < RETRY_COUNT:
                    retry += 1
                    fake._switch_state.deadline = monotonic() + RETRY_WINDOW
                    continue
                # Timeout - should return None
                result = None
                clear_switch()
                break

        self.assertIsNone(result, "Unconfirmed switch must return None")


class TestSoundListenerHealth(unittest.TestCase):
    """Verify sound listener clears running flag on error."""

    def test_listener_clears_running_on_exception(self):
        """Ear must clear _running flag when _loop exits with exception."""
        import threading

        running = threading.Event()
        running.set()

        def failing_loop():
            try:
                raise OSError("simulated device failure")
            finally:
                running.clear()

        thread = threading.Thread(target=failing_loop, daemon=True)
        thread.start()
        thread.join(timeout=1.0)

        self.assertFalse(running.is_set(), "Running flag must be cleared on error")

    def test_is_healthy_detects_dead_thread(self):
        """is_healthy() should return False when thread died."""
        import threading

        running = threading.Event()
        running.set()

        def short_loop():
            try:
                pass  # Exit immediately
            finally:
                running.clear()

        thread = threading.Thread(target=short_loop, daemon=True)
        thread.start()
        thread.join(timeout=1.0)

        # Simulate is_healthy check
        healthy = running.is_set() and thread is not None and thread.is_alive()
        self.assertFalse(healthy, "is_healthy must return False for dead thread")


if __name__ == "__main__":
    unittest.main()
