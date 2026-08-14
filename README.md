# AgentInfra ZeroOps -- Zero-Touch Operations Multi-Agent System

## Overview

This package implements a **6-Agent zero-touch operations (ZeroOps) system**
built on the AgentTeams coordination framework. It demonstrates a complete
closed-loop pipeline from proactive anomaly detection to incident review,
with 9 reusable Skills, 7 MCP server integration points, and full execution
trace/evidence generation.

> 2026-08-14 update: Pipeline extended to 6 Agents (AnomalyDetector at the
> front) and 9 Skills based on 2025 Tianchi AIOps challenge insights
> (Top-K + evidence verification, canary dry-run, dynamic baseline).

## Architecture

```
AnomalyDetector -> AlertAggregator -> RootCauseAnalyst -> RepairExecutor -> RecoveryValidator -> IncidentReviewer
     |                   |                    |                   |                  |                  |
     v                   v                    v                   v                  v                  v
 dynamic-baseline  alert-correlation   root-cause-graph  repair-script-gen   service-health-check  knowledge-extract
                                          data-query       canary-validator    rollback-manager
                                                            rollback-manager
```

The flow:
- **AnomalyDetector** runs FIRST: EWMA-based dynamic baseline scans all
  watched services for soft_alerts (pre-failure buildup signals).
- **AlertAggregator** consolidates hard alerts + soft_alerts into incidents.
- **RootCauseAnalyst** generates Top-K root cause candidates and
  verifies each via evidence (metrics + traces) collected by data-query.
- **RepairExecutor** generates a plan, runs **canary validation** on the
  canary track for high-risk actions, requests ITSM approval, then
  executes and pre-generates rollback.
- **RecoveryValidator** runs three-layer validation (metrics, trace,
  regression) and triggers rollback on failure.
- **IncidentReviewer** extracts knowledge, generates postmortem, performs
  consistency checks before writing to KB.

## Directory Structure

```
03_AgentTeams代码包/
  main.py              -- Entry point
  README.md            -- This file
  requirements.txt     -- Dependencies (stdlib only for submission)
  core/                -- Framework: AgentTeams, AgentBase, ICB, StateMachine
  agents/              -- 6 Agent implementations
  skills/              -- 9 Skill implementations (6 original + 3 AIOps-inspired)
  mcp/                 -- 7 MCP server stubs with method_schemas
  config/              -- Agent, Skill, MCP configuration (JSON)
  data/                -- Sample alerts, topology, knowledge base, configs
  evidence/            -- Generated: traces, state history, audit logs
  output/              -- Generated: ICB JSON, incident reports
```

## Quick Start

```bash
python3 main.py                          # run with sample data (happy path)
python3 main.py --scenario data/scenario_rollback.json
python3 main.py --scenario data/scenario_escalation.json
python3 main.py --scenario data/scenario_canary_failure.json
python3 main.py --skip-anomaly-detector  # disable proactive scanning
```

This runs the pipeline on sample alert data (8 alerts simulating a DB
connection pool exhaustion cascading failure) and produces:
- `output/icb_*.json` -- Full Incident Context Bundle
- `evidence/traces_*.json` -- Agent execution traces (Trace)
- `evidence/state_history_*.json` -- State machine transitions (Metrics)
- `evidence/mcp_audit_*.json` -- MCP tool call audit log
- `evidence/summary_*.txt` -- Human-readable summary (Report)

## Scenarios (4 drills)

| Scenario | File | Expected Path |
|---|---|---|
| **Happy path** | (default) | detected → ... → closed (canary PASSED, 2 actions, validation recovered) |
| **Canary failure** | `data/scenario_canary_failure.json` | detected → ... → escalated → reviewed → closed (canary FAIL, 0 actions) |
| **Rollback** | `data/scenario_rollback.json` | detected → ... → verifying → rollback → reviewed → closed (validation FAIL, auto-rollback) |
| **Escalation** | `data/scenario_escalation.json` | detected → aggregating → analyzing → escalated → reviewed → closed (low confidence, 0 actions) |

## AgentTeams Mapping

| AgentTeams Capability | Implementation |
|---|---|
| Role orchestration | `AgentTeams.register_agent()` + `build_pipeline()` |
| Task decomposition | Each agent decomposes its input (alerts -> incidents, etc.) |
| Context passing | `IncidentContextBundle` (ICB) passed through pipeline |
| Collaborative execution | Sequential pipeline with parallel skill calls |
| State tracking | `IncidentStateMachine` with 9 states and audit trail |

## Skills (9 total)

| Skill | Agent | Purpose |
|---|---|---|
| alert-correlation | AlertAggregator | Alert dedup, correlation, noise reduction |
| **root-cause-graph** | RootCauseAnalyst | Top-K candidates + per-candidate evidence (二阶段) |
| repair-script-gen | RepairExecutor | Repair plan generation, risk assessment |
| service-health-check | RecoveryValidator | Post-repair health validation, SLA check |
| knowledge-extract | IncidentReviewer | Knowledge extraction, postmortem generation |
| rollback-manager | RepairExecutor + RecoveryValidator | Rollback plan pre-generation and execution |
| **data-query** | RootCauseAnalyst | Structured PromQL/Trace/Log queries (avoid agent 直查 MCP) |
| **dynamic-baseline** | AnomalyDetector | EWMA-based dynamic threshold for soft_alerts |
| **canary-validator** | RepairExecutor | Dry-run high-risk actions on canary track before production |

## MCP Servers

All 7 servers expose a `method_schemas` registry in OpenAI function-calling
format, so any LLM-backed Agent can use them via standard `tools=[...]`.

| Server | Tool | Auth | Methods |
|---|---|---|---|
| prometheus-mcp | Prometheus/VictoriaMetrics | api_token | query, query_range, rate_based_query, alert_list, inject_secondary_alert |
| cmdb-mcp | CMDB/Service Registry | mTLS | get_topology, get_service |
| cicd-mcp | CloudEffect/Jenkins | OAuth2 | deploy (production/canary), rollback |
| nacos-mcp | Nacos Config Center | mTLS | get_config, update_config |
| itsm-mcp | Jira/ServiceNow | api_token | create_ticket, approve |
| kb-mcp | PolarDB PG + pgvector | DB-Auth | search, write |
| trace-mcp | ARMS/Jaeger | api_token | get_trace, search_by_error, inject_span |

## AIOps-Inspired Improvements (2026-08-14)

The following improvements were added after studying 10 Tianchi AIOps
challenge notes:

1. **Proactive detection (AnomalyDetector + dynamic-baseline)** -- EWMA
   detects pre-failure buildup, generates `soft_alerts` before hard
   thresholds fire. Inspired by 阿里云 AIOps 实践 + 一文看懂监控平台.
2. **Top-K + evidence verification (RootCauseAnalyst)** -- Two-phase
   root cause: generate Top-K candidates → collect evidence per
   candidate → re-rank. Inspired by 大模型辅助 5 步诊断法.
3. **Data-query Skill (RootCauseAnalyst)** -- Structured query templates
   (`metric_at_time`, `trace_search_by_error`, `rate_based`) replace
   direct MCP calls. Inspired by Pandas/DuckDB 笔记 + 阿里云 SPL-first.
4. **Canary validation (RepairExecutor)** -- High-risk actions are
   dry-run on canary track first; if canary fails, the production
   execution is skipped. Inspired by 大模型 5 步法 + 灰度发布.
5. **MCP tool description standardization** -- Each method now has
   full docstring (purpose, params, errors) in OpenAI function-calling
   format. Inspired by AIOps 打工人踩坑系列.

## Literature Support

Each agent and skill is grounded in 2026 multi-agent infrastructure research:
- KRCA (arXiv 2607.01788) -- root cause analysis methodology
- Honest Quorum (arXiv 2607.16109) -- Byzantine fault tolerance for consensus
- Agent Delivery Engineering (arXiv 2607.07689) -- predictive reliability
- AgentCompass (arXiv 2607.13705) -- unified evaluation infrastructure
- TRiSM (AI Open, SCI) -- trust, risk, security management
- When Agents Go Rogue (arXiv 2607.06807) -- malicious behavior detection
- MPAC (arXiv 2604.09744) -- multi-principal agent coordination
- Agora (arXiv 2607.09600) -- auction-based task allocation
- Flooding Spread (Sci China, CCF B) -- knowledge manipulation prevention

## License

Apache 2.0
