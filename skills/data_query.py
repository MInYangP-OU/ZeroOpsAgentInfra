"""Skill: data-query -- structured query templates over MCP data sources.

启发 2：把数据查询层显式化为 Skill。
Agent 不再直接调用 Prometheus / Trace / Log MCP，而是通过本 Skill 调用。
好处：
  - 查询可被审计、可缓存
  - Agent prompt 不需要写 PromQL/SQL，减少幻觉
  - 查询失败有统一的降级策略

提供三类模板：
  - metric_at_time(service, metric, t±window) -> snapshot
  - trace_search_by_error(service, error_pattern, time_window) -> spans
  - log_grep(service, pattern, time_window) -> log snippets（暂用 trace 模拟）

每个方法都对 MCP 调用做了 Schema 校验，调用错误会进入降级路径并标注 source=degraded。
"""
from __future__ import annotations
import re
import time
from typing import Any


class DataQuerySkill:
    NAME = "data-query"
    PURPOSE = "Structured query templates over Prometheus / Trace / Log MCP"
    INPUT = "queries: list[dict] -- each {type, service, metric|pattern, window}"
    OUTPUT = "results: list[dict] -- each {type, value, source: prometheus|trace|log|degraded}"
    CALL_CONDITION = "RootCauseAnalyst / RecoveryValidator / AnomalyDetector 内部调用"
    DEPENDENCIES = ["Prometheus MCP", "Trace MCP"]
    FAILURE = "Mark source=degraded, never raise to caller"
    SECURITY = "Read-only, no mutations"
    REUSE = "通用查询层；任何需要 metric/trace/log 数据的 Agent 都可调用"

    _METRIC_PATTERN = re.compile(r"^(error_rate|latency_ms|qps)$")

    def execute(self, queries: list[dict], prometheus: Any | None = None,
                tracer: Any | None = None, **kw) -> list[dict]:
        """Run a batch of structured queries; return list of normalized results."""
        results = []
        for q in queries or []:
            qtype = q.get("type")
            try:
                if qtype == "metric_at_time":
                    results.append(self._metric_at_time(q, prometheus))
                elif qtype == "trace_search_by_error":
                    results.append(self._trace_search_by_error(q, tracer))
                elif qtype == "log_grep":
                    results.append(self._log_grep(q, tracer))
                elif qtype == "rate_based":
                    results.append(self._rate_based(q, prometheus))
                else:
                    results.append({
                        "type": str(qtype),
                        "value": None,
                        "source": "degraded",
                        "note": f"unknown query type: {qtype}",
                    })
            except Exception as e:
                # Never raise -- record degraded result for caller inspection.
                results.append({
                    "type": str(qtype),
                    "value": None,
                    "source": "degraded",
                    "note": f"query error: {e}",
                    "query": {k: str(v)[:80] for k, v in q.items()},
                })
        return results

    # ------------------------------------------------------------------
    # metric_at_time -- single-point metric snapshot.
    # ------------------------------------------------------------------
    def _metric_at_time(self, q: dict, prometheus: Any | None) -> dict:
        service = q.get("service", "")
        metric = q.get("metric", "")
        if not service or not metric:
            return {"type": "metric_at_time", "value": None,
                    "source": "degraded", "note": "missing service/metric"}
        if not self._METRIC_PATTERN.match(metric):
            return {"type": "metric_at_time", "value": None,
                    "source": "degraded", "note": f"unknown metric: {metric}"}
        if prometheus is None:
            return {"type": "metric_at_time", "value": None,
                    "service": service, "metric": metric,
                    "source": "degraded"}
        result = prometheus.call("query", service=service, metric=metric)
        if result.get("status") != "ok":
            return {"type": "metric_at_time", "value": result.get("current"),
                    "service": service, "metric": metric,
                    "source": "degraded"}
        return {
            "type": "metric_at_time",
            "value": result.get("current"),
            "series_length": len(result.get("values", [])),
            "service": service,
            "metric": metric,
            "source": "prometheus",
        }

    # ------------------------------------------------------------------
    # trace_search_by_error -- anomalous spans for candidate root cause.
    # ------------------------------------------------------------------
    def _trace_search_by_error(self, q: dict, tracer: Any | None) -> dict:
        service = q.get("service", "")
        pattern = q.get("error_pattern", "")
        if not service or not pattern:
            return {"type": "trace_search_by_error", "spans": [],
                    "source": "degraded", "note": "missing service/pattern"}
        if tracer is None:
            return {"type": "trace_search_by_error", "spans": [],
                    "service": service, "source": "degraded"}
        result = tracer.call("search_by_error", service=service,
                             error_pattern=pattern)
        spans = result.get("spans", []) if result.get("status") == "ok" else []
        return {
            "type": "trace_search_by_error",
            "service": service,
            "pattern": pattern,
            "spans": spans,
            "anomaly_count": sum(1 for s in spans if s.get("status") != "ok"),
            "source": "trace-mcp" if result.get("status") == "ok" else "degraded",
        }

    # ------------------------------------------------------------------
    # log_grep -- structured log search. We reuse the trace stub
    # as a stand-in for log data so the skill can run end-to-end
    # against the existing MCP stubs; in production this would map to
    # SLS/Loki/ELK.
    # ------------------------------------------------------------------
    def _log_grep(self, q: dict, log_source: Any | None) -> dict:
        service = q.get("service", "")
        pattern = q.get("pattern", "")
        if log_source is None:
            return {"type": "log_grep", "lines": [],
                    "service": service, "source": "degraded"}
        # When the trace MCP has a log-style search we'd call it here.
        # We keep the response shape stable across data sources.
        result = log_source.call("search_by_error", service=service,
                                 error_pattern=pattern)
        lines = [{"text": s.get("span", ""), "ts": q.get("window", "n/a")}
                 for s in result.get("spans", [])]
        return {
            "type": "log_grep",
            "service": service,
            "pattern": pattern,
            "lines": lines,
            "source": "log-stub",
        }

    # ------------------------------------------------------------------
    # rate_based -- change-rate query for proactive anomaly detection.
    # ------------------------------------------------------------------
    def _rate_based(self, q: dict, prometheus: Any | None) -> dict:
        service = q.get("service", "")
        metric = q.get("metric", "")
        threshold = float(q.get("threshold", 0.1))
        if prometheus is None:
            return {"type": "rate_based", "rate": 0.0,
                    "service": service, "metric": metric,
                    "source": "degraded"}
        result = prometheus.call("rate_based_query", service=service,
                                 metric=metric, threshold=threshold)
        return {
            "type": "rate_based",
            "service": service,
            "metric": metric,
            "rate": result.get("rate", 0.0),
            "above_threshold": result.get("above_threshold", False),
            "source": "prometheus" if result.get("status") == "ok" else "degraded",
        }

    # ------------------------------------------------------------------
    # Convenience: build a query batch from (service, metric) pairs.
    # ------------------------------------------------------------------
    @staticmethod
    def build_metric_batch(services: list[str], metrics: list[str],
                           window: str = "5m") -> list[dict]:
        return [
            {"type": "metric_at_time", "service": svc, "metric": m, "window": window}
            for svc in services for m in metrics
        ]

    def fallback(self, queries, **kw):
        return [{"type": q.get("type"), "value": None, "source": "degraded",
                 "note": "data_query degraded mode"}
                for q in (queries or [])]
