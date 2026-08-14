"""Incident lifecycle state machine -- maps to AgentTeams state tracking."""
from __future__ import annotations
import logging
import time

logger = logging.getLogger(__name__)


class IncidentState:
    DETECTED = "detected"
    AGGREGATING = "aggregating"
    ANALYZING = "analyzing"
    REPAIRING = "repairing"
    VERIFYING = "verifying"
    REVIEWED = "reviewed"
    CLOSED = "closed"
    ROLLBACK = "rollback"
    ESCALATED = "escalated"

    TRANSITIONS = {
        DETECTED: [AGGREGATING],
        AGGREGATING: [ANALYZING, ESCALATED],
        ANALYZING: [REPAIRING, ESCALATED],
        REPAIRING: [VERIFYING, ROLLBACK, ESCALATED],
        VERIFYING: [REVIEWED, ROLLBACK],
        # Postmortem is required for failed incidents too, so both hold
        # states may proceed to REVIEWED (only the reviewer may run).
        ROLLBACK: [REPAIRING, ESCALATED, REVIEWED],
        REVIEWED: [CLOSED],
        ESCALATED: [ANALYZING, REVIEWED, CLOSED],
        CLOSED: [],
    }


class IncidentStateMachine:
    def __init__(self):
        self._state = IncidentState.DETECTED
        self._history: list[dict] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def history(self) -> list[dict]:
        return self._history

    def transition(self, new_state: str, reason: str = "") -> bool:
        valid = IncidentState.TRANSITIONS.get(self._state, [])
        if new_state not in valid:
            logger.error(f"Invalid transition: {self._state} -> {new_state}")
            return False
        # Millisecond resolution so MTTR computed from state history is
        # meaningful even when the pipeline completes in under a second.
        now = time.time()
        ts = (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
              + f".{int((now % 1) * 1000):03d}Z")
        entry = {"from": self._state, "to": new_state, "reason": reason,
                 "timestamp": ts}
        self._history.append(entry)
        old = self._state
        self._state = new_state
        logger.info(f"State: {old} -> {new_state} ({reason})")
        return True

    def is_terminal(self) -> bool:
        return self._state == IncidentState.CLOSED

    def can_transition(self, new_state: str) -> bool:
        return new_state in IncidentState.TRANSITIONS.get(self._state, [])

    def can_rollback(self) -> bool:
        return self._state in (IncidentState.REPAIRING, IncidentState.VERIFYING)
