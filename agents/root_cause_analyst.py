"""Agent: RootCauseAnalyst -- Top-K root cause localization with evidence.

启发 3 落地：
  - 调用 root_cause_graph.execute_with_evidence() 拿到 Top-K 候选 + 证据
  - 把 Top-K + 证据写入 ICB（top_k_candidates / evidence_per_candidate）
  - 置信度 < 0.7 仍走 escalated 安全闸门（向后兼容）
  - 工具调用通过 data_query Skill（启发 2），不再直查 MCP
"""
from __future__ import annotations
import logging

from core.agent_base import AgentBase, AgentIdentity

logger = logging.getLogger(__name__)


class RootCauseAnalyst(AgentBase):
    """RootCauseAnalyst Agent -- Top-K + evidence verification."""

    def __init__(self, skills=None, mcp_clients=None):
        identity = AgentIdentity(
            name="RootCauseAnalyst",
            role="Root Cause Analysis",
            description=(
                "Top-K root cause candidates via causal graph + "
                "evidence verification through data queries"
            ),
            capabilities=[
                "root-cause-graph",
                "causal-inference",
                "knowledge-retrieval",
                "data-query",
            ],
            boundaries=[
                "No repair execution",
                "No config mutations",
                "Output needs approval for high-risk",
            ],
            upstream=["AlertAggregator", "AnomalyDetector"],
            downstream=["RepairExecutor"],
            security_level="read-only",
            failure_strategy="degrade",
        )
        super().__init__(identity, skills, mcp_clients)

    def execute(self, context: dict) -> "IncidentContextBundle":
        from core.context_bundle import IncidentContextBundle

        icb = context["icb"]
        state_machine = context["state_machine"]

        topology = self._get_topology()
        knowledge = self._search_knowledge(icb.alert_fingerprint.alert_summary)

        # Build incident list from raw_alerts (covers all alerted services).
        alerted_services: set[str] = set()
        for a in icb.raw_alerts or []:
            svc = a.get("service")
            if svc:
                alerted_services.add(svc)
        if not alerted_services:
            alerted_services = set(icb.alert_fingerprint.correlated_services or [])
        if not alerted_services:
            alerted_services = {"unknown"}

        # Also surface soft_alerts from AnomalyDetector if present.
        if getattr(icb, "soft_alerts", None):
            for sa in icb.soft_alerts:
                svc = sa.get("service")
                if svc:
                    alerted_services.add(svc)

        incidents = [
            {
                "service": svc,
                "severity": 1,
                "summary": icb.alert_fingerprint.alert_summary,
            }
            for svc in sorted(alerted_services)
        ]

        # Resolve data sources: prefer DataQuerySkill if available;
        # else fall back to direct MCP clients (back-compat).
        data_query = self.skills.get("data-query")
        prometheus = self.mcp_clients.get("prometheus-mcp")
        tracer = self.mcp_clients.get("trace-mcp")

        root_cause = self.invoke_skill(
            "root-cause-graph",
            incidents=incidents,
            topology=topology,
            alerts=icb.raw_alerts,
            knowledge=knowledge,
            data_query=data_query,
            prometheus=prometheus,
            tracer=tracer,
            top_k=5,
        )

        # Backwards-compat: legacy execute() path returns minimal shape;
        # unwrap to the new format uniformly.
        if "top_k_candidates" not in root_cause:
            node = root_cause.get("node", "unknown")
            root_cause = {
                **root_cause,
                "top_k_candidates": [
                    {"rank": 1, "node": node,
                     "confidence": root_cause.get("confidence", 0),
                     "raw_score": root_cause.get("score", 0)},
                ],
                "evidence_per_candidate": {},
            }

        confidence = root_cause["confidence"]
        if confidence < 0.7:
            logger.warning(
                f"[RootCauseAnalyst] Low confidence: {confidence}"
            )
            state_machine.transition("escalated", "Low root cause confidence")

        icb.root_cause.node = root_cause["node"]
        icb.root_cause.confidence = confidence
        icb.root_cause.causal_chain = root_cause.get("causal_chain", [])
        icb.root_cause.suggested_fixes = root_cause.get("suggested_fixes", [])
        # 启发 3 落地：把 Top-K + 证据写入 ICB，供后续 Agent 复用。
        icb.top_k_candidates = root_cause.get("top_k_candidates", [])
        icb.evidence_for_root_cause = root_cause.get(
            "evidence_per_candidate", {}
        )

        logger.info(
            f"[RootCauseAnalyst] Root cause: {root_cause['node']} "
            f"(confidence: {confidence}); "
            f"Top-K: {[c['node'] for c in icb.top_k_candidates]}"
        )
        return icb

    def _get_topology(self) -> dict:
        if "cmdb-mcp" in self.mcp_clients:
            return self.call_mcp("cmdb-mcp", "get_topology")
        return {}

    def _search_knowledge(self, query: str) -> list:
        if "kb-mcp" in self.mcp_clients:
            result = self.call_mcp("kb-mcp", "search", query=query, top_k=3)
            return result.get("results", [])
        return []
