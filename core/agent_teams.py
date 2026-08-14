"""AgentTeams framework adapter -- multi-Agent coordination base.

Provides five core capabilities required by the competition:
1. Role orchestration
2. Task decomposition
3. Context passing
4. Collaborative execution
5. State tracking

In production this delegates to the official AgentTeams (Hiclaw) SDK.
In this submission it provides a local simulation following the same
design patterns and interface contracts.
"""
from __future__ import annotations
import logging
import time

from .agent_base import AgentBase
from .context_bundle import IncidentContextBundle
from .state_machine import IncidentStateMachine

logger = logging.getLogger(__name__)


class AgentTeams:
    """Multi-Agent coordination framework."""

    def __init__(self, team_name: str = "ZeroOps-Team"):
        self.team_name = team_name
        self.agents: dict[str, AgentBase] = {}
        self.pipeline: list[str] = []
        self.state_machine = IncidentStateMachine()
        self.team_trace: list[dict] = []

    def register_agent(self, agent: AgentBase) -> None:
        name = agent.identity.name
        self.agents[name] = agent
        if name not in self.pipeline:
            self.pipeline.append(name)
        logger.info(f"[AgentTeams] Registered: {name} (role: {agent.identity.role})")

    def build_pipeline(self, order: list[str]) -> None:
        self.pipeline = order
        logger.info(f"[AgentTeams] Pipeline: {' -> '.join(order)}")

    def execute_pipeline(self, initial_context: IncidentContextBundle) -> IncidentContextBundle:
        context = initial_context
        self._log("pipeline_started", context.incident_id)
        state_map = {
            "AlertAggregator": "aggregating",
            "RootCauseAnalyst": "analyzing",
            "RepairExecutor": "repairing",
            "RecoveryValidator": "verifying",
            "IncidentReviewer": "reviewed",
        }
        for agent_name in self.pipeline:
            agent = self.agents.get(agent_name)
            if not agent:
                continue
            # In a hold state (escalated/rollback) only the IncidentReviewer
            # may run -- postmortem is required for failed incidents too.
            # All other stages are skipped so, e.g., a low-confidence root
            # cause never leads to repair execution after escalation.
            if (
                self.state_machine.state in ("escalated", "rollback")
                and agent_name != "IncidentReviewer"
            ):
                logger.info(
                    f"[AgentTeams] Skipping {agent_name}: "
                    f"state={self.state_machine.state}"
                )
                self._log("agent_skipped", context.incident_id,
                          agent=agent_name, state=self.state_machine.state)
                continue
            target = state_map.get(agent_name, "")
            if target and self.state_machine.can_transition(target):
                self.state_machine.transition(target, f"{agent_name} starting")
            context.stage = target or context.stage
            start = time.time()
            try:
                result = agent.execute({"icb": context, "state_machine": self.state_machine})
                if isinstance(result, IncidentContextBundle):
                    context = result
                elapsed = round((time.time() - start) * 1000, 2)
                self._log("agent_completed", context.incident_id, agent=agent_name, elapsed_ms=elapsed)
            except Exception as e:
                logger.error(f"[AgentTeams] '{agent_name}' failed: {e}")
                self._log("agent_failed", context.incident_id, agent=agent_name, error=str(e))
                if self.state_machine.can_rollback():
                    self.state_machine.transition("rollback", f"{agent_name} failed")
                else:
                    self.state_machine.transition("escalated", f"{agent_name} failed")
                break

        # Final state: only transition to closed if the pipeline ended
        # cleanly.  If we already rolled back or escalated mid-pipeline,
        # preserve that terminal state.
        final = self.state_machine.state
        if final in ("escalated", "rollback"):
            self._log("pipeline_terminated", context.incident_id, final_state=final)
        else:
            self.state_machine.transition("closed", "Pipeline complete")
            self._log("pipeline_completed", context.incident_id)
        return context

    def get_team_trace(self) -> list[dict]:
        traces = list(self.team_trace)
        for agent in self.agents.values():
            traces.extend(agent.get_trace())
        return traces

    def _log(self, event, incident_id, **extra):
        self.team_trace.append({"event": event, "incident_id": incident_id,
                                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **extra})

    def print_summary(self) -> str:
        lines = [f"AgentTeams: {self.team_name}", f"Agents: {len(self.agents)}",
                 f"Pipeline: {' -> '.join(self.pipeline)}",
                 f"Final State: {self.state_machine.state}",
                 f"Transitions: {len(self.state_machine.history)}",
                 f"Team Events: {len(self.team_trace)}"]
        return "\n".join(lines)
