"""Skill: canary-validator -- dry-run repair actions on a canary instance.

启发 4：Canary / Dry-run 验证。
高危操作（DB 变更、扩容、流量切换）在执行前先在 canary 实例/灰度环境
跑一次修复方案，验证后再决定是否在生产环境执行。爆炸半径↓
"""
from __future__ import annotations
import time
from typing import Any


class CanaryValidatorSkill:
    NAME = "canary-validator"
    PURPOSE = "Dry-run repair actions on canary instance before production execution"
    INPUT = (
        "actions: list[dict] -- repair actions to dry-run; "
        "cicd: CICDMCP -- to deploy canary; "
        "prometheus/tracer: for health probes"
    )
    OUTPUT = (
        "canary_result: dict -- passed (bool), per-action results, "
        "metrics_snapshot, trace_snapshot"
    )
    CALL_CONDITION = (
        "RepairExecutor 在执行高危操作前（risk == high）触发；"
        "中/低风险可跳过"
    )
    DEPENDENCIES = ["CI/CD MCP (deploy canary track)", "Prometheus MCP (probe)", "Trace MCP"]
    FAILURE = "Fail-closed: any exception => canary_result.passed = False"
    SECURITY = "Canary 环境只读访问，无生产写入"
    REUSE = "任何高危变更前都可调用；与 rollback-manager 互补"

    def execute(
        self,
        actions: list[dict],
        cicd: Any | None = None,
        prometheus: Any | None = None,
        tracer: Any | None = None,
        **kw,
    ) -> dict:
        """Dry-run each action on the canary track."""
        per_action: list[dict] = []
        passed_all = True

        for act in actions or []:
            risk = act.get("risk", "medium")
            target = act.get("target", "")
            change = act.get("change", "")

            # Low/medium risk: skip canary (overhead > benefit).
            if risk in ("low", "medium"):
                per_action.append({
                    "action_id": act.get("action_id"),
                    "skipped": True,
                    "reason": f"risk={risk}; canary not required",
                })
                continue

            # High risk: deploy canary, probe, decide.
            try:
                deploy = cicd.call(
                    "deploy", service=target, change=change, track="canary"
                ) if cicd is not None else {"status": "skipped-no-cicd"}
                probe = self._probe_canary(target, prometheus, tracer)
                ok = (
                    deploy.get("status") == "success"
                    and probe.get("healthy", False)
                )
                per_action.append({
                    "action_id": act.get("action_id"),
                    "deploy": deploy,
                    "probe": probe,
                    "passed": ok,
                })
                if not ok:
                    passed_all = False
            except Exception as e:
                # Fail-closed.
                per_action.append({
                    "action_id": act.get("action_id"),
                    "passed": False,
                    "error": str(e),
                })
                passed_all = False

        return {
            "passed": passed_all,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "per_action": per_action,
            "summary": {
                "total_actions": len(actions or []),
                "high_risk_checked": sum(
                    1 for p in per_action if p.get("passed") is not None
                ),
                "passed": sum(
                    1 for p in per_action if p.get("passed") is True
                ),
                "failed": sum(
                    1 for p in per_action if p.get("passed") is False
                ),
            },
        }

    def _probe_canary(
        self, target: str, prometheus: Any | None, tracer: Any | None
    ) -> dict:
        """Probe canary health: query error_rate/latency, scan trace spans."""
        result = {"healthy": True, "metrics": {}, "trace": {}}

        if prometheus is not None:
            err = prometheus.call("query", service=target, metric="error_rate")
            lat = prometheus.call("query", service=target, metric="latency_ms")
            result["metrics"] = {
                "error_rate": err.get("current"),
                "latency_ms": lat.get("current"),
                "source": "prometheus",
            }
            # Healthy if error_rate < 0.05 AND latency_ms < 200 (post-fix).
            if err.get("current", 0) > 0.05 or lat.get("current", 1000) > 200:
                result["healthy"] = False

        if tracer is not None:
            trace = tracer.call("get_trace", service=target)
            spans = trace.get("spans", [])
            bad = [s for s in spans if s.get("status") != "ok"]
            result["trace"] = {
                "span_count": len(spans),
                "bad_span_count": len(bad),
                "source": "trace-mcp",
            }
            if bad:
                result["healthy"] = False

        return result

    def fallback(self, actions=None, **kw):
        # When canary is unavailable, fail-closed: caller should skip production.
        return {
            "passed": False,
            "per_action": [],
            "summary": {"note": "canary unavailable; fail-closed"},
        }
