"""Task result checks and cancellation-aware waits for custom actions."""

import math
import time
from typing import Any, Callable

from utils.logger import logger


class TaskFailedError(RuntimeError):
    """A required subtask failed; subsequent steps must not run."""


class TaskCancelledError(TaskFailedError):
    """The user requested cancellation, rather than a recoverable failure."""


def task_succeeded(detail: Any) -> bool:
    """A returned TaskDetail can describe a failed task as well."""
    return detail is not None and detail.status.succeeded


def action_succeeded(detail: Any) -> bool:
    """ActionDetail is returned even when the action itself fails."""
    return detail is not None and detail.success


def require_task(
    context: Any, entry: str, pipeline_override: dict | None = None
) -> None:
    """Run a required subtask, keeping cancellation distinct from failure."""
    if context.tasker.stopping:
        raise TaskCancelledError(entry)
    result = context.run_task(entry, pipeline_override or {})
    if context.tasker.stopping:
        raise TaskCancelledError(entry)
    if not task_succeeded(result):
        raise TaskFailedError(entry)


def run_task_checked(
    context: Any, entry: str, pipeline_override: dict | None = None
) -> bool:
    try:
        require_task(context, entry, pipeline_override)
    except TaskCancelledError:
        logger.debug("Subtask cancelled: %s", entry)
        return False
    except TaskFailedError:
        logger.error("Required subtask failed: %s", entry)
        return False
    return True


def wait_until_cancelled(
    seconds: float,
    should_cancel: Callable[[], bool],
    *,
    interval: float = 0.05,
) -> bool:
    """Return True after the duration, or False on cancellation."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Wait duration must be finite and nonnegative")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Wait interval must be finite and positive")
    deadline = time.monotonic() + seconds
    while not should_cancel():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(interval, remaining))
    return False


def interruptible_wait(context: Any, seconds: float, *, interval: float = 0.05) -> bool:
    return wait_until_cancelled(
        seconds, lambda: context.tasker.stopping, interval=interval
    )
