"""Incident Context Bundle (ICB) -- structured context passing between agents.

Schema extended for AIOps-inspired improvements (2026-08-14):
  - soft_alerts: list[dict] -- AnomalyDetector 输出
  - top_k_candidates: list[dict] -- RootCauseAnalyst 输出
  - evidence_for_root_cause: dict -- 每个候选的证据
  - canary_result: dict -- RepairExecutor 输出
"""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class AlertFingerprint:
    source_count: int = 0
    correlated_services: list[str] = field(default_factory=list)
    time_window: str = ""
    alert_summary: str = ""


@dataclass
class RootCause:
    node: str = ""
    confidence: float = 0.0
    causal_chain: list[str] = field(default_factory=list)
    suggested_fixes: list[dict] = field(default_factory=list)


@dataclass
class RepairAction:
    action_id: str = ""
    action_type: str = ""
    target: str = ""
    change: str = ""
    approved_by: str = ""
    timestamp: str = ""
    status: str = "pending"


@dataclass
class RollbackPlan:
    action_id: str = ""
    change: str = ""


@dataclass
class ValidationResult:
    status: str = "pending"
    sla_check: str = "pending"
    metrics_after: dict[str, float] = field(default_factory=dict)
    trace_check: dict[str, Any] = field(default_factory=dict)
    secondary_alerts: bool = False
    secondary_alert_details: list[dict] = field(default_factory=list)


@dataclass
class Evidence:
    trace_ids: list[str] = field(default_factory=list)
    metrics_snapshots: list[str] = field(default_factory=list)
    audit_log: str = ""


class IncidentContextBundle:
    """Structured event context for inter-agent communication.

    Maps to AgentTeams context passing capability.
    Flow:
      AnomalyDetector -> AlertAggregator -> RootCauseAnalyst ->
      RepairExecutor -> RecoveryValidator -> IncidentReviewer.

    New fields (2026-08-14):
      - soft_alerts: proactive anomaly signals (AnomalyDetector)
      - top_k_candidates / evidence_for_root_cause: root cause
        hypothesis space (RootCauseAnalyst)
      - canary_result: pre-execution dry-run (RepairExecutor)
    """

    def __init__(self, incident_id: str | None = None):
        self.incident_id = incident_id or f"INC-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.severity: str = "P1"
        self.stage: str = "detected"
        self.alert_fingerprint = AlertFingerprint()
        self.root_cause = RootCause()
        self.repair_actions: list[RepairAction] = []
        self.rollback_plans: list[RollbackPlan] = []
        self.validation = ValidationResult()
        self.evidence = Evidence()
        self.raw_alerts: list[dict] = []
        self.postmortem: dict[str, Any] = {}
        self.knowledge_update: dict[str, Any] = {}
        # New AIOps-inspired fields:
        self.soft_alerts: list[dict] = []         # AnomalyDetector
        self.top_k_candidates: list[dict] = []    # RootCauseAnalyst
        self.evidence_for_root_cause: dict = {}   # RootCauseAnalyst
        self.canary_result: dict[str, Any] = {}   # RepairExecutor

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id, "timestamp": self.timestamp,
            "severity": self.severity, "stage": self.stage,
            "alert_fingerprint": asdict(self.alert_fingerprint),
            "root_cause": asdict(self.root_cause),
            "repair_actions": [asdict(a) for a in self.repair_actions],
            "rollback_plans": [asdict(r) for r in self.rollback_plans],
            "validation": asdict(self.validation),
            "evidence": asdict(self.evidence),
            "raw_alerts": self.raw_alerts,
            "postmortem": self.postmortem,
            "knowledge_update": self.knowledge_update,
            "soft_alerts": self.soft_alerts,
            "top_k_candidates": self.top_k_candidates,
            "evidence_for_root_cause": self.evidence_for_root_cause,
            "canary_result": self.canary_result,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def save(self, filepath: str) -> None:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def from_dict(cls, data: dict) -> "IncidentContextBundle":
        icb = cls(incident_id=data.get("incident_id"))
        icb.timestamp = data.get("timestamp", icb.timestamp)
        icb.severity = data.get("severity", "P1")
        icb.stage = data.get("stage", "detected")
        icb.alert_fingerprint = AlertFingerprint(**data.get("alert_fingerprint", {}))
        icb.root_cause = RootCause(**data.get("root_cause", {}))
        icb.repair_actions = [RepairAction(**a) for a in data.get("repair_actions", [])]
        icb.rollback_plans = [RollbackPlan(**r) for r in data.get("rollback_plans", [])]
        icb.validation = ValidationResult(**data.get("validation", {}))
        icb.evidence = Evidence(**data.get("evidence", {}))
        icb.raw_alerts = data.get("raw_alerts", [])
        icb.postmortem = data.get("postmortem", {})
        icb.knowledge_update = data.get("knowledge_update", {})
        icb.soft_alerts = data.get("soft_alerts", [])
        icb.top_k_candidates = data.get("top_k_candidates", [])
        icb.evidence_for_root_cause = data.get("evidence_for_root_cause", {})
        icb.canary_result = data.get("canary_result", {})
        return icb
