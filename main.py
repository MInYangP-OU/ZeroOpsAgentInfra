#!/usr/bin/env python3
"""AgentInfra ZeroOps -- Zero-Touch Operations Multi-Agent System.

AIOps-inspired pipeline (2026-08-14 update):
  AnomalyDetector -> AlertAggregator -> RootCauseAnalyst ->
  RepairExecutor -> RecoveryValidator -> IncidentReviewer

Usage:
    python3 main.py                          # run with sample data (happy path)
    python3 main.py --alerts custom.json     # run with custom alerts
    python3 main.py --scenario data/scenario_rollback.json
    python3 main.py --scenario data/scenario_escalation.json
    python3 main.py --scenario data/scenario_canary_failure.json
    python3 main.py --skip-anomaly-detector  # for back-compat / debugging
    python3 main.py --evidence-only          # regenerate evidence from last run
"""
import argparse
import json
import logging
import os
import sys
import time

# Add package root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.agent_teams import AgentTeams
from core.context_bundle import IncidentContextBundle
from agents import (
    AnomalyDetector, AlertAggregator, RootCauseAnalyst,
    RepairExecutor, RecoveryValidator, IncidentReviewer,
)
from skills import (
    AlertCorrelationSkill, RootCauseGraphSkill, RepairScriptGenSkill,
    ServiceHealthCheckSkill, KnowledgeExtractSkill, RollbackManagerSkill,
    DataQuerySkill, DynamicBaselineSkill, CanaryValidatorSkill,
)
from mcp import PrometheusMCP, CMDBMCP, CICDMCP, NacosMCP, ITSMMCP, KnowledgeBaseMCP, TraceMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ZeroOps")


def load_sample_data(data_dir: str) -> dict:
    """Load sample alerts, topology, knowledge base, and service configs."""
    with open(os.path.join(data_dir, "sample_alerts.json")) as f:
        alerts = json.load(f)
    with open(os.path.join(data_dir, "sample_topology.json")) as f:
        topology = json.load(f)
    with open(os.path.join(data_dir, "knowledge_base.json")) as f:
        knowledge = json.load(f)
    with open(os.path.join(data_dir, "service_configs.json")) as f:
        configs = json.load(f)
    return {"alerts": alerts, "topology": topology, "knowledge": knowledge, "configs": configs}


def build_mcp_clients(data: dict) -> dict:
    """Initialize MCP server stubs with sample data."""
    return {
        "prometheus-mcp": PrometheusMCP(),
        "cmdb-mcp": CMDBMCP(topology=data["topology"]),
        "cicd-mcp": CICDMCP(),
        "nacos-mcp": NacosMCP(configs=data["configs"]),
        "itsm-mcp": ITSMMCP(),
        "kb-mcp": KnowledgeBaseMCP(knowledge=data["knowledge"]),
        "trace-mcp": TraceMCP(),
    }


def build_skills() -> dict:
    """Instantiate all 9 core Skills (6 original + 3 AIOps-inspired)."""
    return {
        "alert-correlation": AlertCorrelationSkill(),
        "root-cause-graph": RootCauseGraphSkill(),
        "repair-script-gen": RepairScriptGenSkill(),
        "service-health-check": ServiceHealthCheckSkill(),
        "knowledge-extract": KnowledgeExtractSkill(),
        "rollback-manager": RollbackManagerSkill(),
        # AIOps-inspired additions:
        "data-query": DataQuerySkill(),
        "dynamic-baseline": DynamicBaselineSkill(),
        "canary-validator": CanaryValidatorSkill(),
    }


def build_agents(skills: dict, mcp_clients: dict) -> dict:
    """Build 6 Agents (5 original + AnomalyDetector) with skill/MCP bindings."""
    return {
        "AnomalyDetector": AnomalyDetector(
            skills={k: v for k, v in skills.items() if k == "dynamic-baseline"},
            mcp_clients={k: v for k, v in mcp_clients.items() if k == "prometheus-mcp"},
        ),
        "AlertAggregator": AlertAggregator(
            skills={k: v for k, v in skills.items() if k == "alert-correlation"},
            mcp_clients={k: v for k, v in mcp_clients.items() if k in ("prometheus-mcp", "cmdb-mcp")},
        ),
        "RootCauseAnalyst": RootCauseAnalyst(
            skills={k: v for k, v in skills.items() if k in ("root-cause-graph", "data-query")},
            mcp_clients={k: v for k, v in mcp_clients.items() if k in ("cmdb-mcp", "kb-mcp", "trace-mcp", "prometheus-mcp")},
        ),
        "RepairExecutor": RepairExecutor(
            skills={k: v for k, v in skills.items() if k in ("repair-script-gen", "rollback-manager", "canary-validator")},
            mcp_clients={k: v for k, v in mcp_clients.items() if k in ("kb-mcp", "nacos-mcp", "cicd-mcp", "itsm-mcp", "prometheus-mcp", "trace-mcp")},
        ),
        "RecoveryValidator": RecoveryValidator(
            skills={k: v for k, v in skills.items() if k in ("service-health-check", "rollback-manager")},
            mcp_clients={k: v for k, v in mcp_clients.items() if k in ("prometheus-mcp", "trace-mcp")},
        ),
        "IncidentReviewer": IncidentReviewer(
            skills={k: v for k, v in skills.items() if k == "knowledge-extract"},
            mcp_clients={k: v for k, v in mcp_clients.items() if k in ("kb-mcp", "trace-mcp", "itsm-mcp")},
        ),
    }


def save_evidence(team: AgentTeams, icb: IncidentContextBundle,
                  output_dir: str, evidence_dir: str) -> None:
    """Save execution evidence: ICB, traces, state history, summary."""
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(evidence_dir, exist_ok=True)

    # 1. Incident Context Bundle (full ICB including new AIOps fields).
    icb.save(os.path.join(output_dir, f"icb_{icb.incident_id}.json"))
    logger.info(f"ICB saved: output/icb_{icb.incident_id}.json")

    # 2. Agent execution traces (Trace).
    traces = team.get_team_trace()
    with open(os.path.join(evidence_dir, f"traces_{icb.incident_id}.json"), "w") as f:
        json.dump(traces, f, indent=2, ensure_ascii=False)
    logger.info(f"Traces saved: evidence/traces_{icb.incident_id}.json ({len(traces)} entries)")

    # 3. State machine history.
    with open(os.path.join(evidence_dir, f"state_history_{icb.incident_id}.json"), "w") as f:
        json.dump(team.state_machine.history, f, indent=2, ensure_ascii=False)
    logger.info(f"State history saved: {len(team.state_machine.history)} transitions")

    # 4. MCP call logs.
    mcp_logs = []
    for agent in team.agents.values():
        for entry in agent.get_trace():
            if "mcp_server" in entry:
                mcp_logs.append(entry)
    with open(os.path.join(evidence_dir, f"mcp_audit_{icb.incident_id}.json"), "w") as f:
        json.dump(mcp_logs, f, indent=2, ensure_ascii=False)
    logger.info(f"MCP audit log saved: {len(mcp_logs)} calls")

    # 5. Team summary (Report).
    summary = team.print_summary()
    with open(os.path.join(evidence_dir, f"summary_{icb.incident_id}.txt"), "w") as f:
        f.write(summary + "\n\n")
        f.write(f"Incident: {icb.incident_id}\n")
        f.write(f"Severity: {icb.severity}\n")
        f.write(f"Root Cause: {icb.root_cause.node} (confidence: {icb.root_cause.confidence})\n")
        f.write(f"Soft Alerts: {len(icb.soft_alerts)}\n")
        f.write(f"Top-K Candidates: {[c.get('node') for c in icb.top_k_candidates]}\n")
        f.write(f"Repair Actions: {len(icb.repair_actions)}\n")
        for a in icb.repair_actions:
            f.write(f"  - [{a.action_id}] {a.action_type} on {a.target}: "
                    f"{a.change} (approved_by: {a.approved_by})\n")
        f.write(f"Canary: {icb.canary_result.get('passed', 'n/a')}\n")
        f.write(f"Validation: {icb.validation.status} (SLA: {icb.validation.sla_check})\n")
        tc = icb.validation.trace_check or {}
        f.write(f"  - Layer1 Metrics: {icb.validation.sla_check} "
                f"({len(icb.validation.metrics_after)} services)\n")
        f.write(f"  - Layer2 Trace: {'PASSED' if tc.get('passed', True) else 'FAILED'}\n")
        f.write(f"  - Layer3 Regression: "
                f"{'PASSED' if not icb.validation.secondary_alerts else 'FAILED'}\n")
        ku = icb.knowledge_update or {}
        f.write(f"Postmortem: {bool(icb.postmortem)}\n")
        if ku:
            f.write(f"Knowledge Consistency: {ku.get('consistency_check')} "
                    f"(notes: {ku.get('consistency_notes', [])})\n")
            f.write(f"MTTR: {ku.get('postmortem', {}).get('mttr_seconds', '?')}s\n")
    logger.info(f"Summary saved: evidence/summary_{icb.incident_id}.txt")

    # 6. Console output (Log).
    print("\n" + "=" * 60)
    print("ZeroOps Execution Complete (AIOps-inspired pipeline)")
    print("=" * 60)
    print(summary)
    print(f"\nIncident: {icb.incident_id}")
    print(f"Severity: {icb.severity}")
    print(f"Root Cause: {icb.root_cause.node} (confidence: {icb.root_cause.confidence})")
    print(f"Soft Alerts (AnomalyDetector): {len(icb.soft_alerts)}")
    for sa in icb.soft_alerts:
        print(f"  - {sa['service']}/{sa['metric']}: z={sa['max_z_score']}")
    print(f"Top-K Candidates: {[c['node'] for c in icb.top_k_candidates]}")
    print(f"Repair Actions: {len(icb.repair_actions)}")
    for a in icb.repair_actions:
        print(f"  - [{a.action_id}] {a.action_type} on {a.target}: {a.change}")
    cr = icb.canary_result
    print(f"Canary: passed={cr.get('passed', 'n/a')} "
          f"(summary: {cr.get('summary', {})})")
    print(f"Validation: {icb.validation.status} (SLA: {icb.validation.sla_check})")
    print(f"  - Layer1 Metrics: {icb.validation.sla_check} "
          f"({len(icb.validation.metrics_after)} services)")
    print(f"  - Layer2 Trace: {'PASSED' if tc.get('passed', True) else 'FAILED'} "
          f"(source: {tc.get('source', 'n/a')})")
    print(f"  - Layer3 Regression: {'PASSED' if not icb.validation.secondary_alerts else 'FAILED'}")
    print(f"Rollback Plans: {len(icb.rollback_plans)}")
    print(f"Postmortem: {'Generated' if icb.postmortem else 'N/A'}")
    ku = icb.knowledge_update or {}
    if ku:
        print(f"Knowledge: consistency={ku.get('consistency_check')}, "
              f"patterns={1 + len(ku.get('fix_patterns', []))}, "
              f"MTTR={ku.get('postmortem', {}).get('mttr_seconds', '?')}s")
    print(f"\nEvidence files in: evidence/")
    print(f"Output files in: output/")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="ZeroOps Multi-Agent System")
    parser.add_argument("--alerts", type=str, default=None, help="Custom alerts JSON file")
    parser.add_argument("--scenario", type=str, default=None,
                        help="Scenario JSON: optional alerts/topology/knowledge overrides "
                             "plus sabotage injections (inject_spans / inject_secondary_alerts)")
    parser.add_argument("--evidence-only", action="store_true", help="Regenerate evidence")
    parser.add_argument("--skip-anomaly-detector", action="store_true",
                        help="Skip AnomalyDetector (back-compat / debug)")
    args = parser.parse_args()

    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(pkg_dir, "data")
    output_dir = os.path.join(pkg_dir, "output")
    evidence_dir = os.path.join(pkg_dir, "evidence")

    logger.info("Loading sample data...")
    data = load_sample_data(data_dir)

    scenario = None
    if args.scenario:
        with open(args.scenario) as f:
            scenario = json.load(f)
        for key in ("alerts", "topology", "knowledge", "configs"):
            if key in scenario:
                data[key] = scenario[key]
        logger.info(f"Loaded scenario '{scenario.get('name', args.scenario)}': "
                    f"{scenario.get('description', '')[:80]}")

    if args.alerts:
        with open(args.alerts) as f:
            data["alerts"] = json.load(f)
        logger.info(f"Loaded custom alerts: {len(data['alerts'])} alerts")

    logger.info("Initializing MCP servers...")
    mcp_clients = build_mcp_clients(data)

    # Apply scenario sabotage injections (post-repair failure simulation).
    if scenario:
        for span in scenario.get("inject_spans", []):
            mcp_clients["trace-mcp"].call("inject_span", **span)
            logger.info(f"[Scenario] Injected anomalous span: {span.get('service')}/{span.get('span')}")
        for alert in scenario.get("inject_secondary_alerts", []):
            mcp_clients["prometheus-mcp"].call("inject_secondary_alert", **alert)
            logger.info(f"[Scenario] Injected secondary alert: {alert.get('service')}/{alert.get('type')}")
        # Canary sabotage: force canary to fail by pre-poisoning the metric series.
        for poison in scenario.get("inject_canary_failure", []):
            svc = poison.get("service", "")
            metric = poison.get("metric", "error_rate")
            # Inject a high-error_rate value via set_metric so the canary probe
            # returns degraded health.
            mcp_clients["prometheus-mcp"].call(
                "set_metric", service=svc, metric=metric,
                values=[0.001, 0.001, 0.001, 0.5, 0.8, 0.95],
            )
            logger.info(f"[Scenario] Canary-failure poison set on {svc}/{metric}")

    logger.info("Initializing Skills...")
    skills = build_skills()

    logger.info("Building Agents...")
    agents = build_agents(skills, mcp_clients)

    logger.info("Setting up AgentTeams pipeline...")
    team = AgentTeams(team_name="ZeroOps-Team")
    pipeline = []
    if not args.skip_anomaly_detector:
        pipeline.append("AnomalyDetector")
    pipeline.extend([
        "AlertAggregator", "RootCauseAnalyst", "RepairExecutor",
        "RecoveryValidator", "IncidentReviewer",
    ])
    for name in pipeline:
        team.register_agent(agents[name])
    team.build_pipeline(pipeline)

    logger.info("Creating Incident Context Bundle...")
    icb = IncidentContextBundle()
    icb.raw_alerts = data["alerts"]
    logger.info(f"Incident {icb.incident_id} created with {len(icb.raw_alerts)} raw alerts")

    logger.info("Starting pipeline execution...")
    start_time = time.time()
    result_icb = team.execute_pipeline(icb)
    elapsed = round(time.time() - start_time, 2)
    logger.info(f"Pipeline completed in {elapsed}s")

    save_evidence(team, result_icb, output_dir, evidence_dir)


if __name__ == "__main__":
    main()
