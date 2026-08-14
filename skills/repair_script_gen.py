"""Skill: repair-script-gen -- generate repair actions from root cause analysis."""
from __future__ import annotations
import time


class RepairScriptGenSkill:
    NAME = "repair-script-gen"
    PURPOSE = "Generate repair scripts and config changes from root cause report"
    INPUT = "root_cause: dict, service_config: dict, runbook: list[dict]"
    OUTPUT = "repair_plan: dict -- actions, rollback_plan, risk_assessment"
    CALL_CONDITION = "RootCauseAnalyst confidence > 0.7"
    DEPENDENCIES = ["Knowledge Base MCP", "Nacos MCP", "CI/CD MCP"]
    FAILURE = "LLM-generated suggestion + manual approval flag"
    SECURITY = "High-risk actions require approval; never auto-execute writes"
    REUSE = "Scene-configurable; swap runbook for different ops scenarios"

    def execute(self, root_cause: dict, service_config: dict | None = None,
                runbook: list[dict] | None = None, **kw) -> dict:
        cfg = service_config or {}
        actions = []
        rollbacks = []
        for fix in root_cause.get("suggested_fixes", []):
            risk = fix.get("risk", "medium")
            aid = f"ACT-{len(actions)+1:03d}"
            action = {
                "action_id": aid, "action_type": fix["type"],
                "target": fix["target"], "change": fix["action"],
                "risk": risk,
                "requires_approval": risk == "high" or fix.get("requires_approval", False),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            actions.append(action)
            rollbacks.append({
                "action_id": f"{aid}-ROLLBACK",
                "change": f"revert: {fix['action']} on {fix['target']}",
            })
        return {
            "actions": actions,
            "rollback_plan": rollbacks,
            "risk_assessment": {
                "max_risk": max((a["risk"] for a in actions), default="none"),
                "requires_human": any(a["requires_approval"] for a in actions),
            },
        }

    def fallback(self, root_cause, **kw):
        return {"actions": [], "rollback_plan": [],
                "risk_assessment": {"max_risk": "unknown", "requires_human": True},
                "note": "No matching runbook; manual intervention required"}
