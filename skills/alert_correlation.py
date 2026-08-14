"""Skill: alert-correlation -- multi-source alert dedup, correlation, and noise reduction."""
from __future__ import annotations
import time
from collections import defaultdict


class AlertCorrelationSkill:
    NAME = "alert-correlation"
    PURPOSE = "Multi-source alert dedup, correlation, and noise reduction"
    INPUT = "alerts: list[dict] -- raw alerts from monitoring sources"
    OUTPUT = "incidents: list[dict] -- correlated incidents with severity"
    CALL_CONDITION = "Alert count exceeds threshold (default 3/min)"
    DEPENDENCIES = ["Prometheus MCP", "CMDB MCP", "Log MCP"]
    FAILURE = "Degrade to simple dedup by alert fingerprint"
    SECURITY = "Read-only, no mutations"
    REUSE = "Scene-agnostic; usable by AlertAggregator and IncidentReviewer"

    def execute(self, alerts: list[dict], topology: dict | None = None, **kw) -> list[dict]:
        """Correlate alerts by service and time window."""
        groups = defaultdict(list)
        for a in alerts:
            svc = a.get("service", "unknown")
            groups[svc].append(a)

        incidents = []
        for svc, svc_alerts in groups.items():
            # Severity: lower number = more severe in this dataset.
            # min() picks the most severe alert per service.
            severity = min((a.get("severity", 99) for a in svc_alerts), default=99)
            alert_types = sorted(set(a.get("type", "unknown") for a in svc_alerts))
            ts_range = f"{min(a.get('ts','') for a in svc_alerts)} ~ {max(a.get('ts','') for a in svc_alerts)}"
            incidents.append({
                "service": svc,
                "alert_count": len(svc_alerts),
                "severity": severity,
                "alert_types": alert_types,
                "time_window": ts_range,
                "summary": f"{svc}: {len(svc_alerts)} alerts [{', '.join(alert_types)}]",
                "correlated_services": self._find_correlated(svc, topology or {}),
            })
        incidents.sort(key=lambda x: x["severity"])
        return incidents

    def _find_correlated(self, svc: str, topology: dict) -> list[str]:
        deps = topology.get(svc, {})
        related = set()
        related.update(deps.get("depends_on", []))
        related.update(deps.get("depended_by", []))
        return sorted(related)

    def fallback(self, alerts, **kw):
        seen = set()
        deduped = []
        for a in alerts:
            key = (a.get("service"), a.get("type"))
            if key not in seen:
                seen.add(key)
                deduped.append(a)
        return [{"service": a["service"], "alert_count": 1, "severity": a.get("severity", 3),
                 "summary": f"{a['service']}: {a.get('type','alert')}", "correlated_services": []}
                for a in deduped]
