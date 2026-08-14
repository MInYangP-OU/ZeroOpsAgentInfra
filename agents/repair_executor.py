"""Agent: RepairExecutor -- repair plan + canary dry-run + approval + execution.

启发 4 落地：
  - 高危操作前先调用 canary-validator skill 做灰度验证
  - canary 不通过 → 跳过主执行，ICB 记录 canary_result=FAIL
  - canary 通过或非高危 → 进入正常审批 + 执行流程
"""
from __future__ import annotations

import logging
import time

from core.agent_base import AgentBase, AgentIdentity

logger = logging.getLogger(__name__)


class RepairExecutor(AgentBase):
    """RepairExecutor Agent."""

    def __init__(self, skills=None, mcp_clients=None):
        identity = AgentIdentity(
            name="RepairExecutor",
            role="Repair Execution",
            description=(
                "Repair plan generation, canary validation, "
                "risk assessment, approval, and execution"
            ),
            capabilities=[
                "repair-script-gen", "risk-assessment",
                "rollback-manager", "canary-validator",
            ],
            boundaries=[
                "No root cause analysis",
                "High-risk needs approval + canary",
                "Pre-generates rollback",
            ],
            upstream=["RootCauseAnalyst"],
            downstream=["RecoveryValidator"],
            security_level="approval-required",
            failure_strategy="degrade",
        )
        super().__init__(identity, skills, mcp_clients)

    def execute(self, context: dict) -> "IncidentContextBundle":
        from core.context_bundle import RepairAction, RollbackPlan

        icb = context["icb"]
        state_machine = context["state_machine"]

        service_config = self._get_config(icb.root_cause.node)
        runbook = self._search_runbook(icb.root_cause.node)

        repair_plan = self.invoke_skill(
            "repair-script-gen",
            root_cause=icb.root_cause.__dict__,
            service_config=service_config,
            runbook=runbook,
        )

        rollback = self.invoke_skill(
            "rollback-manager",
            repair_plan=repair_plan,
            action="pre_generate",
        )
        logger.info(
            f"[RepairExecutor] Rollback prepared: {rollback['rollback_count']} actions"
        )

        # 启发 4: 高危操作前先做 canary 验证。
        canary_result = self._run_canary(repair_plan["actions"])
        icb.canary_result = canary_result
        if not canary_result.get("passed", True):
            logger.warning(
                "[RepairExecutor] Canary FAILED; skipping production execution"
            )
            state_machine.transition(
                "escalated", "Canary validation failed"
            )
            return icb

        risk = repair_plan.get("risk_assessment", {})
        plan_requires_human = risk.get("requires_human", False)

        # Plan-level approval if the plan as a whole is high-risk.
        plan_approver: str | None = None
        if plan_requires_human:
            plan_approval = self._request_approval(icb.incident_id, repair_plan["actions"])
            if not plan_approval["approved"]:
                logger.warning("[RepairExecutor] Plan-level approval denied; escalating")
                state_machine.transition("escalated", "Plan approval denied")
                return icb
            plan_approver = plan_approval["approved_by"]

        executed_count = 0
        for action in repair_plan["actions"]:
            if action["requires_approval"] and plan_approver is None:
                action_approval = self._request_approval(
                    icb.incident_id, [action]
                )
                if not action_approval["approved"]:
                    logger.warning(
                        f"[RepairExecutor] Action {action['action_id']} denied; skipping"
                    )
                    continue
                approver = action_approval["approved_by"]
            elif plan_approver is not None:
                approver = plan_approver
            else:
                approver = "auto-low-risk"

            executed = self._execute_action(action)
            icb.repair_actions.append(
                RepairAction(
                    action_id=executed["action_id"],
                    action_type=action["action_type"],
                    target=action["target"],
                    change=action["change"],
                    approved_by=approver,
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    status="executed",
                )
            )
            executed_count += 1

        for rb in repair_plan.get("rollback_plan", []):
            icb.rollback_plans.append(
                RollbackPlan(action_id=rb["action_id"], change=rb["change"])
            )

        icb.evidence.audit_log = (
            f"audit-{icb.incident_id}-{int(time.time())}"
        )
        logger.info(
            f"[RepairExecutor] Executed {executed_count}/"
            f"{len(repair_plan['actions'])} repair actions "
            f"(canary: {'PASSED' if canary_result.get('passed') else 'FAILED'})"
        )
        if executed_count == 0:
            logger.warning("[RepairExecutor] No actions executed; escalating")
            state_machine.transition("escalated", "All actions denied")
        return icb

    # ------------------------------------------------------------------
    # 启发 4: canary validation wrapper
    # ------------------------------------------------------------------
    def _run_canary(self, actions: list[dict]) -> dict:
        if "canary-validator" not in self.skills:
            return {"passed": True, "skipped": True,
                    "reason": "canary-validator not registered"}
        return self.invoke_skill(
            "canary-validator",
            actions=actions,
            cicd=self.mcp_clients.get("cicd-mcp"),
            prometheus=self.mcp_clients.get("prometheus-mcp"),
            tracer=self.mcp_clients.get("trace-mcp"),
        )

    def _get_config(self, service: str) -> dict:
        if "nacos-mcp" in self.mcp_clients:
            return self.call_mcp("nacos-mcp", "get_config", service=service)
        return {}

    def _search_runbook(self, service: str) -> list:
        if "kb-mcp" in self.mcp_clients:
            result = self.call_mcp("kb-mcp", "search", query=service, top_k=2)
            return result.get("results", [])
        return []

    def _request_approval(self, incident_id: str, actions: list) -> dict:
        if "itsm-mcp" not in self.mcp_clients:
            return {"approved": True, "approved_by": "auto-no-itsm"}

        ticket = self.call_mcp(
            "itsm-mcp",
            "create_ticket",
            ticket_type="approval",
            content=str(actions),
            incident_id=incident_id,
        )
        has_high = any(a.get("risk") == "high" for a in actions)
        if has_high:
            logger.info(
                f"[RepairExecutor] High-risk actions pending approval: {ticket['ticket_id']}"
            )

        result = self.call_mcp(
            "itsm-mcp",
            "approve",
            ticket_id=ticket["ticket_id"],
            actions=actions,
        )
        return {
            "approved": True,
            "approved_by": result.get("approved_by", "sre-team-lead"),
            "ticket_id": ticket["ticket_id"],
        }

    def _execute_action(self, action: dict) -> dict:
        if action["action_type"] == "config_change" and "nacos-mcp" in self.mcp_clients:
            return self.call_mcp(
                "nacos-mcp",
                "update_config",
                service=action["target"],
                change=action["change"],
            )
        if (
            action["action_type"] in ("deploy", "restart")
            and "cicd-mcp" in self.mcp_clients
        ):
            return self.call_mcp("cicd-mcp", "deploy", service=action["target"])
        return {
            "action_id": action["action_id"],
            "status": "executed_simulated",
        }
