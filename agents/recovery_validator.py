"""Agent: RecoveryValidator -- post-repair health validation and SLA verification."""
from __future__ import annotations
import logging

from core.agent_base import AgentBase, AgentIdentity

logger = logging.getLogger(__name__)


class RecoveryValidator(AgentBase):
    """RecoveryValidator Agent.

    Validates service recovery after repair execution using three layers:
    1. Metrics layer: key metrics within SLA thresholds for 3 min
    2. Trace layer: distributed tracing shows no anomalous call chains
    3. Regression layer: no secondary alerts

    Supported by: Agent Delivery Engineering (ADE-PRF, arXiv 2607.07689),
    AgentCompass (unified evaluation, arXiv 2607.13705)
    """

    def __init__(self, skills=None, mcp_clients=None):
        identity = AgentIdentity(
            name="RecoveryValidator",
            role="Recovery Validation",
            description="Post-repair health check, SLA verification, regression detection",
            capabilities=["service-health-check", "sla-validator", "regression-detection"],
            boundaries=["No repair decisions", "No config mutations", "Read-only validation"],
            upstream=["RepairExecutor"],
            downstream=["IncidentReviewer"],
            security_level="read-only",
            failure_strategy="degrade",
        )
        super().__init__(identity, skills, mcp_clients)

    def execute(self, context: dict) -> "IncidentContextBundle":
        from core.context_bundle import IncidentContextBundle, ValidationResult

        icb = context["icb"]
        state_machine = context["state_machine"]

        # Validate the full affected set: every alerted service plus every
        # repair target -- not just the repaired root-cause service -- so
        # cascaded victims (order-service, payment-service) must also meet
        # SLA before the incident is considered recovered.
        alerted = {a.get("service") for a in icb.raw_alerts or [] if a.get("service")}
        repair_targets = {a.target for a in icb.repair_actions if a.target}
        services = sorted(alerted | repair_targets)
        sla = self._get_sla(services)

        prometheus = self.mcp_clients.get("prometheus-mcp")
        tracer = self.mcp_clients.get("trace-mcp")
        validation = self.invoke_skill(
            "service-health-check",
            repair_actions=[a.__dict__ for a in icb.repair_actions],
            sla=sla,
            services=services,
            prometheus=prometheus,
            tracer=tracer,
        )

        icb.validation = ValidationResult(
            status=validation["status"],
            sla_check=validation["sla_check"],
            metrics_after=validation["metrics_after"],
            trace_check=validation.get("trace_check", {}),
            secondary_alerts=validation["secondary_alerts"],
            secondary_alert_details=validation.get("secondary_alert_details", []),
        )

        if validation["status"] != "recovered":
            logger.warning("[RecoveryValidator] Validation FAILED; triggering rollback")
            if self.skills.get("rollback-manager"):
                rollback_result = self.invoke_skill("rollback-manager",
                                                    repair_plan={"rollback_plan":
                                                                 [r.__dict__ for r in icb.rollback_plans]},
                                                    action="execute")
                logger.info(f"[RecoveryValidator] Rollback executed: {rollback_result['status']}")
                state_machine.transition("rollback", "Validation failed; rolled back")
                return icb

        logger.info(
            f"[RecoveryValidator] Validation: {validation['status']} "
            f"(SLA: {validation['sla_check']}, "
            f"trace: {'PASSED' if validation.get('trace_check', {}).get('passed', True) else 'FAILED'}, "
            f"services checked: {len(services)})"
        )
        return icb

    def _get_sla(self, services: list) -> dict:
        return {svc: {"error_rate": 0.05, "latency_ms": 100} for svc in services}
