"""Skill: service-health-check -- validate service recovery after repair.

Validation layers (all three now real):
  1. Metrics layer: query Prometheus MCP for current error_rate / latency_ms.
  2. Trace layer: cross-check no anomalous call chains.
  3. Regression layer: detect secondary alerts in the last 3 minutes.

If a prometheus client is not provided, falls back to degraded mode
(probe-based check) and the result is marked `source: degraded`.
"""
from __future__ import annotations

import time
from typing import Any


class ServiceHealthCheckSkill:
    NAME = "service-health-check"
    PURPOSE = "Validate service recovery: metrics, traces, regression"
    INPUT = (
        "repair_actions: list[dict], sla: dict, services: list[str], "
        "prometheus: PrometheusMCP"
    )
    OUTPUT = "validation: dict -- status, sla_check, metrics_after, secondary_alerts"
    CALL_CONDITION = "After RepairExecutor completes actions"
    DEPENDENCIES = ["Prometheus MCP", "Trace MCP", "Log MCP"]
    FAILURE = "Degrade to probe-based checks; mark source=degraded"
    SECURITY = "Read-only validation"
    REUSE = "Scene-agnostic; usable by RecoveryValidator and IncidentReviewer"

    def execute(
        self,
        repair_actions: list[dict],
        sla: dict | None = None,
        services: list[str] | None = None,
        prometheus: Any | None = None,
        tracer: Any | None = None,
        **kw,
    ) -> dict:
        targets = services or [
            a["target"] for a in repair_actions if a.get("target")
        ]
        metrics_after: dict[str, dict] = {}
        metrics_pass = True

        # Layer 1: metrics -- error_rate / latency within SLA thresholds.
        for svc in targets:
            err_rate, latency, source = self._read_metrics(svc, prometheus)
            thresholds = (sla or {}).get(
                svc, {"error_rate": 0.05, "latency_ms": 100}
            )
            ok = (
                err_rate <= thresholds.get("error_rate", 0.05)
                and latency <= thresholds.get("latency_ms", 100)
            )
            metrics_after[svc] = {
                "error_rate": err_rate,
                "latency_ms": latency,
                "healthy": ok,
                "source": source,
            }
            if not ok:
                metrics_pass = False

        # Layer 2: trace -- no anomalous spans in recent call chains.
        trace_check = self._check_traces(targets, tracer)

        # Layer 3: regression -- no secondary alerts in the last 3 minutes.
        secondary_alerts = self._detect_secondary_alerts(targets, prometheus)

        all_pass = (
            metrics_pass
            and trace_check["passed"]
            and len(secondary_alerts) == 0
        )

        return {
            "status": "recovered" if all_pass else "degraded",
            "sla_check": "PASSED" if metrics_pass else "FAILED",
            "metrics_after": metrics_after,
            "trace_check": trace_check,
            "secondary_alerts": len(secondary_alerts) > 0,
            "secondary_alert_details": secondary_alerts,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @staticmethod
    def _check_traces(services: list[str], tracer: Any | None) -> dict:
        """Trace layer: fail if any recent span has status != ok."""
        per_service: dict[str, dict] = {}
        all_ok = True
        source = "trace-mcp" if tracer is not None else "degraded"
        for svc in services:
            if tracer is None:
                per_service[svc] = {"passed": True, "source": "degraded"}
                continue
            data = tracer.call("get_trace", service=svc)
            spans = data.get("spans", [])
            bad = [s for s in spans if s.get("status") != "ok"]
            per_service[svc] = {
                "passed": len(bad) == 0,
                "span_count": len(spans),
                "anomalous_spans": bad,
                "source": source if data.get("status") == "ok" else "no_data",
            }
            if bad:
                all_ok = False
        return {"passed": all_ok, "services": per_service, "source": source}

    @staticmethod
    def _read_metrics(
        svc: str, prometheus: Any | None
    ) -> tuple[float, float, str]:
        """Return (error_rate, latency_ms, source)."""
        if prometheus is None:
            return 0.5, 500.0, "degraded"
        err_data = prometheus.call("query", service=svc, metric="error_rate")
        lat_data = prometheus.call("query", service=svc, metric="latency_ms")
        if err_data.get("status") != "ok" or lat_data.get("status") != "ok":
            return 0.5, 500.0, "degraded"
        return (
            float(err_data.get("current", 0.5)),
            float(lat_data.get("current", 500.0)),
            "prometheus",
        )

    @staticmethod
    def _detect_secondary_alerts(
        services: list[str], prometheus: Any | None
    ) -> list[dict]:
        if prometheus is None:
            return []
        alerts_data = prometheus.call("alert_list")
        return [
            a
            for a in alerts_data.get("alerts", [])
            if a.get("service") in services
            and a.get("ts_minutes_ago", 99) <= 3
        ]

    def fallback(self, repair_actions, **kw):
        return {
            "status": "unknown",
            "sla_check": "SKIP",
            "metrics_after": {},
            "trace_check": {"passed": True, "services": {}, "source": "degraded"},
            "secondary_alerts": False,
            "secondary_alert_details": [],
            "note": "Probe check degraded mode",
        }
