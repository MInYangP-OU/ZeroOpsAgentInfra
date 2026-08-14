"""Agent: IncidentReviewer -- postmortem, knowledge extraction, and experience accumulation."""
from __future__ import annotations
import logging

from core.agent_base import AgentBase, AgentIdentity

logger = logging.getLogger(__name__)


class IncidentReviewer(AgentBase):
    """IncidentReviewer Agent.

    Performs post-incident analysis, extracts structured knowledge
    (root cause patterns, fix patterns), updates knowledge base and runbooks.
    Implements consistency checks to prevent knowledge manipulation.

    Supported by: TRiSM (AI Open, SCI), When Agents Go Rogue (arXiv 2607.06807),
    Flooding Spread (Sci China, CCF B)
    """

    def __init__(self, skills=None, mcp_clients=None):
        identity = AgentIdentity(
            name="IncidentReviewer",
            role="Incident Review",
            description="Postmortem analysis, knowledge extraction, runbook update",
            capabilities=["knowledge-extract", "postmortem-gen", "runbook-update"],
            boundaries=["No real-time repair participation", "No running config changes"],
            upstream=["RecoveryValidator"],
            downstream=["knowledge-base"],
            security_level="read-only",
            failure_strategy="degrade",
        )
        super().__init__(identity, skills, mcp_clients)

    def execute(self, context: dict) -> "IncidentContextBundle":
        from core.context_bundle import IncidentContextBundle

        icb = context["icb"]
        state_machine = context["state_machine"]

        icb_dict = icb.to_dict()
        icb_dict["state_history"] = state_machine.history

        knowledge = self.invoke_skill("knowledge-extract", icb=icb_dict)

        icb.postmortem = knowledge.get("postmortem", {})
        # Persist the full knowledge update (patterns, consistency check,
        # runbook suggestion) into the ICB as sedimentation evidence.
        icb.knowledge_update = knowledge

        if knowledge.get("consistency_check", False):
            self._write_knowledge(knowledge)
            logger.info("[IncidentReviewer] Knowledge written to KB (consistency verified)")
        else:
            logger.warning("[IncidentReviewer] Knowledge consistency check failed; "
                          "flagged for manual review")

        if "itsm-mcp" in self.mcp_clients:
            self.call_mcp("itsm-mcp", "create_ticket", ticket_type="close",
                          content=icb.incident_id)

        logger.info(f"[IncidentReviewer] Postmortem generated for {icb.incident_id}")
        return icb

    def _write_knowledge(self, knowledge: dict) -> None:
        if "kb-mcp" in self.mcp_clients:
            self.call_mcp("kb-mcp", "write", content=knowledge)
