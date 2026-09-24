"""Custom recognition exposing combat state to the pipeline."""

from __future__ import annotations

import json
from typing import Optional

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_recognition import CustomRecognition

from utils.logger import logger

from .observer import CombatObserver

# A degenerate box: custom recognitions signal "hit" through a non-None box.
_HIT_BOX = [0, 0, 1, 1]


def _parse_param(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _session_is_terminal() -> bool:
    # Imported lazily to avoid an import cycle with actions.py.
    from . import actions

    session = actions.active_session()
    return session is None or session.terminal


@AgentServer.custom_recognition("CombatStatusRecognition")
class CombatStatusRecognition(CustomRecognition):
    """Pipeline-facing combat status checks.

    ``custom_recognition_param.check_type`` selects the condition:

    - ``in_combat``: hit when an enemy health bar is confirmed.
    - ``cleared``: hit when no enemy health bar is confirmed.
    - ``session_terminal``: hit when the active combat session has ended.

    Unknown observations never hit, so callers cannot mistake "could not
    tell" for a confirmed state.
    """

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Optional[CustomRecognition.AnalyzeResult]:
        params = _parse_param(argv.custom_recognition_param)
        check_type = params.get("check_type", "in_combat")

        try:
            if check_type == "session_terminal":
                terminal = _session_is_terminal()
                return CustomRecognition.AnalyzeResult(
                    box=_HIT_BOX if terminal else None,
                    detail={"check_type": check_type, "terminal": terminal},
                )

            snapshot = CombatObserver(context).analyze(argv.image)
            detail = {
                "check_type": check_type,
                "combat": snapshot.combat,
                "evidence": list(snapshot.evidence),
            }
            if check_type == "in_combat":
                hit = snapshot.combat is True
            elif check_type == "cleared":
                hit = snapshot.combat is False
            else:
                logger.warning("CombatStatusRecognition: unknown check_type %s", check_type)
                hit = False
            return CustomRecognition.AnalyzeResult(box=_HIT_BOX if hit else None, detail=detail)
        except Exception as exc:
            logger.error("CombatStatusRecognition failed: %s", exc)
            return None
