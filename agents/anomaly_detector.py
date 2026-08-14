"""Agent: AnomalyDetector -- proactive anomaly detection via dynamic baseline.

启发 1 落地：在 AlertAggregator 之前运行，主动扫描每个服务的关键指标。
对超出动态阈值但尚未达到告警阈值的"软告警"（soft_alerts）写入 ICB，
让 RootCauseAnalyst 提前拿到早期信号。
"""
from __future__ import annotations
import logging

from core.agent_base import AgentBase, AgentIdentity

logger = logging.getLogger(__name__)


class AnomalyDetector(AgentBase):
    """AnomalyDetector Agent -- runs before AlertAggregator."""

    # Services / metrics to monitor proactively.
    WATCH_SERVICES = [
        "order-service", "payment-service",
        "inventory-service", "api-gateway",
    ]
    WATCH_METRICS = ["error_rate", "latency_ms"]

    def __init__(self, skills=None, mcp_clients=None):
        identity = AgentIdentity(
            name="AnomalyDetector",
            role="Proactive Anomaly Detection",
            description=(
                "EWMA-based dynamic threshold scanning; surfaces "
                "soft_alerts before hard alerts fire"
            ),
            capabilities=["dynamic-baseline", "rate-based-query"],
            boundaries=[
                "Read-only; never triggers repairs",
                "Only generates soft_alerts (advisory)",
            ],
            upstream=["prometheus-mcp"],
            downstream=["AlertAggregator", "RootCauseAnalyst"],
            security_level="read-only",
            failure_strategy="degrade",
        )
        super().__init__(identity, skills, mcp_clients)

    def execute(self, context: dict) -> "IncidentContextBundle":
        from core.context_bundle import IncidentContextBundle

        icb = context["icb"]
        state_machine = context["state_machine"]

        prometheus = self.mcp_clients.get("prometheus-mcp")
        soft_alerts: list[dict] = []

        for svc in self.WATCH_SERVICES:
            for metric in self.WATCH_METRICS:
                series = self._get_series(prometheus, svc, metric)
                if not series:
                    continue
                report = self.invoke_skill(
                    "dynamic-baseline", series=series
                )
                summary = report.get("summary", {})
                if summary.get("status") != "ok":
                    continue
                anomalies = report.get("anomalies", [])
                if anomalies:
                    soft_alerts.append({
                        "service": svc,
                        "metric": metric,
                        "anomaly_count": summary.get("anomaly_count", 0),
                        "max_z_score": max(
                            (a.get("z_score", 0) for a in anomalies),
                            default=0,
                        ),
                        "latest_value": series[-1],
                        "final_ewma": summary.get("final_ewma", 0),
                        "final_sigma": summary.get("final_sigma", 0),
                        "source": "anomaly_detector",
                    })

        icb.soft_alerts = soft_alerts
        logger.info(
            f"[AnomalyDetector] Generated {len(soft_alerts)} soft_alerts "
            f"from {len(self.WATCH_SERVICES)} services"
        )
        # AnomalyDetector never advances state; it only annotates the ICB.
        return icb

    def _get_series(self, prometheus, svc: str, metric: str) -> list[float]:
        # AnomalyDetector reads PRE-failure buildup data so that EWMA detects
        # the escalating trend. PrometheusMCP.DEFAULT_SERIES represents
        # post-repair recovery (decreasing), so for the demo we use
        # SAMPLE_METRICS which captures the leading-up-to-failure shape.
        # In production, query_range with the appropriate historical window
        # would be used.
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import sys
        if here not in sys.path:
            sys.path.insert(0, here)
        from data.sample_metrics import SAMPLE_METRICS
        return SAMPLE_METRICS.get(svc, {}).get(metric, [])
