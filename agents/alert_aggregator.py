"""Agent: AlertAggregator -- multi-source alert aggregation and noise reduction."""
from __future__ import annotations
import logging

from core.agent_base import AgentBase, AgentIdentity

logger = logging.getLogger(__name__)


class AlertAggregator(AgentBase):
    """AlertAggregator Agent.

    Receives multi-source alerts, performs dedup/correlation/noise reduction,
    and outputs structured incidents for RootCauseAnalyst.

    Supported by: Agora (auction-based task allocation, arXiv 2607.09600),
    When Do MAS Help (information bottleneck, arXiv 2607.16133)
    """

    def __init__(self, skills=None, mcp_clients=None):
        identity = AgentIdentity(
            name="AlertAggregator",
            role="Alert Aggregation",
            description="Multi-source alert aggregation, dedup, correlation, and noise reduction",
            capabilities=["alert-correlation", "alert-prioritization"],
            boundaries=["No root cause analysis", "No repair decisions", "No mutations"],
            upstream=["alert-sources"],
            downstream=["RootCauseAnalyst"],
            security_level="read-only",
            failure_strategy="degrade",
        )
        super().__init__(identity, skills, mcp_clients)

    def execute(self, context: dict) -> "IncidentContextBundle":
        from core.context_bundle import IncidentContextBundle

        icb = context["icb"]
        state_machine = context["state_machine"]

        topology = self._get_topology()
        incidents = self.invoke_skill("alert-correlation",
                                      alerts=icb.raw_alerts, topology=topology)
        if not incidents:
            logger.warning("[AlertAggregator] No incidents after correlation")
            state_machine.transition("escalated", "No incidents found")
            return icb

        # Severity is the MAX (most severe) across all incidents,
        # not just the primary's severity.  P0 = sev 0, P1 = sev 1, etc.
        max_severity = min(
            (inc.get("severity", 99) for inc in incidents),
            default=99,
        )
        icb.severity = f"P{max_severity}"

        primary = incidents[0]
        icb.alert_fingerprint.source_count = len(icb.raw_alerts)
        icb.alert_fingerprint.correlated_services = primary.get("correlated_services", [])
        icb.alert_fingerprint.time_window = primary.get("time_window", "")
        icb.alert_fingerprint.alert_summary = primary["summary"]

        logger.info(
            f"[AlertAggregator] Aggregated {len(icb.raw_alerts)} alerts -> "
            f"{len(incidents)} incidents. Primary: {primary['service']}, "
            f"Severity: {icb.severity}"
        )
        return icb

    def _get_topology(self) -> dict:
        if "cmdb-mcp" in self.mcp_clients:
            return self.call_mcp("cmdb-mcp", "get_topology")
        return {}
