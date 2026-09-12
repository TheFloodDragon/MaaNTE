"""Sound event source for combat integration."""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .models import SoundEvent
from .resources import find_sound_sample


class SoundEventSource:
    """Wraps sound listener to produce session-scoped events."""

    def __init__(self):
        self._ear = None
        self._session_id: Optional[str] = None
        self._event_queue: list[SoundEvent] = []
        self._lock = threading.Lock()
        self._dodge_ttl = 0.3  # Event valid for 300ms
        self._running = False

    def start(
        self,
        session_id: str,
        sample_name: str,
        counter_name: Optional[str],
        threshold: float,
        counter_threshold: float,
        stop_check: Callable[[], bool],
    ) -> tuple[bool, str]:
        """Start sound listener and bind to session.

        Args:
            session_id: Combat session identifier
            sample_name: Dodge sample filename (e.g., "dodge.wav")
            counter_name: Optional counter sample filename (e.g., "counter.wav")
            threshold: Dodge recognition threshold
            counter_threshold: Counter recognition threshold
            stop_check: Callable to check if task is stopping

        Returns:
            (success, error_reason)
        """
        if self._running:
            return False, "already_running"

        try:
            from agent.custom.action.SoundTrigger.SoundListener import Ear
        except ImportError:
            try:
                from custom.action.SoundTrigger.SoundListener import Ear
            except ImportError:
                return False, "sound_module_unavailable"

        # Resolve sample files using resource resolver
        sample_path = find_sound_sample(sample_name)
        if sample_path is None:
            return False, f"sample_not_found:{sample_name}"

        counter_path = None
        if counter_name:
            counter_path = find_sound_sample(counter_name)
            if counter_path is None:
                return False, f"counter_sample_not_found:{counter_name}"

        try:
            self._ear = Ear(
                sample_path=str(sample_path),
                counter_path=str(counter_path) if counter_path else None,
                threshold=threshold,
                counter_threshold=counter_threshold,
                stop_check=stop_check,
            )
            self._ear.on_dodge = self._on_dodge
            self._ear.on_counter = self._on_counter
            self._session_id = session_id
            self._ear.start()

            # Wait a bit and check if thread is alive
            time.sleep(0.1)
            if not self._ear.is_healthy():
                self._ear = None
                return False, "listener_died_on_start"

            self._running = True
            return True, ""
        except Exception as exc:
            self._ear = None
            return False, f"start_error:{exc}"

    def stop(self, timeout_ms: int = 3000) -> tuple[bool, str]:
        """Stop listener and wait for thread exit.

        Returns:
            (success, reason) - False if timeout or thread didn't exit
        """
        if not self._running:
            return True, "not_running"

        if self._ear is None:
            self._running = False
            return True, "already_none"

        try:
            self._ear.stop()
            # Ear.stop() already joins with timeout; check if actually stopped
            if self._ear._thread and self._ear._thread.is_alive():
                return False, "thread_still_alive"
            self._ear = None
            self._running = False
            with self._lock:
                self._event_queue.clear()
            return True, ""
        except Exception as exc:
            return False, f"stop_error:{exc}"

    def is_healthy(self) -> bool:
        """Check if listener is running and thread is alive."""
        if not self._running or self._ear is None:
            return False
        return self._ear.is_healthy()

    def poll_event(self) -> Optional[SoundEvent]:
        """Get next unexpired event from queue."""
        now = time.monotonic()
        with self._lock:
            while self._event_queue:
                event = self._event_queue.pop(0)
                if event.expires_at > now:
                    return event
            return None

    def clear_events(self) -> None:
        """Clear all pending events (used when session ends or is interrupted)."""
        with self._lock:
            self._event_queue.clear()

    def _on_dodge(self):
        """Callback from Ear when dodge sound detected."""
        if self._session_id is None:
            return
        now = time.monotonic()
        event = SoundEvent(
            session_id=self._session_id,
            detected_at=now,
            expires_at=now + self._dodge_ttl,
            kind="dodge",
        )
        with self._lock:
            self._event_queue.append(event)

    def _on_counter(self):
        """Callback from Ear when counter sound detected."""
        if self._session_id is None:
            return
        now = time.monotonic()
        event = SoundEvent(
            session_id=self._session_id,
            detected_at=now,
            expires_at=now + self._dodge_ttl,
            kind="counter",
        )
        with self._lock:
            self._event_queue.append(event)
