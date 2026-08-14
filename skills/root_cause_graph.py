"""Skill: root-cause-graph -- build causal graph, generate Top-K candidates,
collect evidence per candidate, and rank.

启发 3：从"单点定位"升级为"假设空间搜索"。

二阶段流程：
  Phase 1 -- 生成候选：从拓扑 apex + 特征匹配 + KB 打分得到 Top-K
  Phase 2 -- 证据收集：对每个候选，通过 data_query Skill 收集
              metric / trace 证据
  Phase 3 -- 排序：用证据对候选再打分，输出最终根因 + 置信度

向后兼容：保留旧接口 `execute()` 的单点定位逻辑，并新增
`execute_with_evidence()` 返回带 Top-K + 证据的结果。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


class RootCauseGraphSkill:
    NAME = "root-cause-graph"
    PURPOSE = "Top-K root cause candidates + per-candidate evidence + final rank"
    INPUT = (
        "incidents: list[dict], topology: dict, alerts: list[dict], "
        "knowledge: list[dict], data_query: DataQuerySkill (optional), "
        "prometheus/tracer for evidence (optional)"
    )
    OUTPUT = (
        "root_cause: dict -- node, confidence, causal_chain, suggested_fixes, "
        "top_k_candidates, evidence_per_candidate"
    )
    CALL_CONDITION = "After alert-correlation outputs structured incidents"
    DEPENDENCIES = [
        "CMDB MCP", "Trace MCP", "Knowledge Base MCP",
        "DataQuerySkill (optional, for evidence phase)",
    ]
    FAILURE = "Degrade to Top-K candidates with low confidence"
    SECURITY = "Read-only analysis"
    REUSE = "Core capability; methodology from KRCA paper (arXiv 2607.01788)"

    # --- Feature taxonomy for alert-to-service matching ---
    DB_KEYWORDS = re.compile(
        r"\b(db|database|connection[-_ ]?pool|sql|query|deadlock|"
        r"lock|transaction|primary|replica|wal|vacuum)\b",
        re.IGNORECASE,
    )
    MEMORY_KEYWORDS = re.compile(r"\b(memory|oom|gc|heap|ram|cache)\b", re.IGNORECASE)
    NETWORK_KEYWORDS = re.compile(
        r"\b(timeout|connectivity|network|dns|tcp|socket|"
        r"latency|slow|hang)\b",
        re.IGNORECASE,
    )

    # Evidence keywords used to weight traces.
    EVIDENCE_TERMS = ["error", "fail", "timeout", "exception", "5xx"]

    # ------------------------------------------------------------------
    # New Top-K + evidence interface (启发 3)
    # ------------------------------------------------------------------
    def execute_with_evidence(
        self,
        incidents: list[dict],
        topology: dict | None = None,
        alerts: list[dict] | None = None,
        knowledge: list[dict] | None = None,
        data_query: Any | None = None,
        prometheus: Any | None = None,
        tracer: Any | None = None,
        top_k: int = 5,
        **kw,
    ) -> dict:
        topo = topology or {}
        knowledge = knowledge or []
        alerts = alerts or []

        # Phase 1 -- candidate generation.
        candidates = self._generate_candidates(incidents, topo, alerts, knowledge, top_k)

        # Phase 2 -- evidence collection (per-candidate).
        evidence_per_candidate = self._gather_evidence(
            candidates, alerts, data_query, prometheus, tracer
        )

        # Phase 3 -- final scoring + ranking.
        scored = self._rank_with_evidence(candidates, evidence_per_candidate)
        if not scored:
            return self.fallback(incidents)

        winner = scored[0]
        node, confidence, final_score = winner
        winner_evidence = evidence_per_candidate.get(node, {})

        # Build causal chain (kept for backward compatibility).
        causal_chain = self._build_causal_chain(
            node, topo,
            {a.get("service") for a in alerts or [] if a.get("service")},
        )

        # Generate fix suggestions.
        knowledge_matches = sum(
            1 for k in knowledge if k.get("pattern_service") == node
        )
        suggested = self._generate_fixes(
            node, topo.get(node, {}), knowledge,
        )

        return {
            "node": node,
            "confidence": round(confidence, 2),
            "causal_chain": causal_chain,
            "suggested_fixes": suggested,
            "knowledge_matches": knowledge_matches,
            "score": final_score,
            "top_k_candidates": [
                {
                    "rank": i + 1,
                    "node": n,
                    "confidence": round(c, 2),
                    "raw_score": s,
                }
                for i, (n, c, s) in enumerate(scored[:top_k])
            ],
            "evidence_per_candidate": evidence_per_candidate,
        }

    # ------------------------------------------------------------------
    # Legacy single-candidate interface (kept for backward compat).
    # ------------------------------------------------------------------
    def execute(
        self,
        incidents: list[dict],
        topology: dict | None = None,
        alerts: list[dict] | None = None,
        traces: list[dict] | None = None,
        knowledge: list[dict] | None = None,
        **kw,
    ) -> dict:
        # Delegate to the new interface; no MCP clients -> empty evidence.
        return self.execute_with_evidence(
            incidents=incidents, topology=topology, alerts=alerts,
            knowledge=knowledge, data_query=None, prometheus=None, tracer=None,
            top_k=1,
        )

    # ------------------------------------------------------------------
    # Phase 1: candidate generation.
    # ------------------------------------------------------------------
    def _generate_candidates(
        self,
        incidents: list[dict],
        topo: dict,
        alerts: list[dict],
        knowledge: list[dict],
        top_k: int,
    ) -> list[tuple[str, float, int]]:
        alerted_services: set[str] = set()
        for inc in incidents or []:
            svc = inc.get("service")
            if svc:
                alerted_services.add(svc)
        for a in alerts or []:
            svc = a.get("service")
            if svc:
                alerted_services.add(svc)
        if not alerted_services:
            return []

        apex_candidates: set[str] = set()
        for svc in alerted_services:
            # Only walk upstream if the service is in topology.
            if svc in topo:
                apex_candidates.update(self._find_upstream_apexes(svc, topo))
        # Add alerted services that exist in topology as candidates.
        # (Services not in topology are "phantoms" -- they exist as alerts
        # but we can't reason about their dependencies, so we score them
        # only on evidence + KB, not topology.)
        apex_candidates.update(s for s in alerted_services if s in topo)

        alert_text = " ".join(
            str(a.get("message", "")) + " " + str(a.get("type", ""))
            for a in alerts
        )
        scored: list[tuple[str, float, int]] = []
        for apex in apex_candidates:
            score, kb_hits = self._score_candidate(
                apex, topo.get(apex, {}), alert_text, knowledge
            )
            scored.append((apex, score, kb_hits))

        scored.sort(key=lambda x: (-x[1], -x[2]))
        return scored[:top_k]

    # ------------------------------------------------------------------
    # Phase 2: per-candidate evidence collection.
    # ------------------------------------------------------------------
    def _gather_evidence(
        self,
        candidates: list[tuple[str, float, int]],
        alerts: list[dict],
        data_query: Any | None,
        prometheus: Any | None,
        tracer: Any | None,
    ) -> dict[str, dict]:
        evidence: dict[str, dict] = {}
        for cand, _, _ in candidates:
            per_cand: dict[str, Any] = {
                "metric_evidence": [],
                "trace_evidence": [],
                "kb_matches": [],
            }
            # Metric evidence: query error_rate / latency_ms for candidate service.
            if data_query is not None:
                queries = data_query.build_metric_batch(
                    [cand], ["error_rate", "latency_ms"], window="5m"
                )
                results = data_query.execute(
                    queries, prometheus=prometheus, tracer=tracer
                )
                per_cand["metric_evidence"] = results
            # Trace evidence: search for anomalous spans.
            if tracer is not None:
                trace_res = tracer.call(
                    "search_by_error", service=cand, error_pattern="error"
                )
                spans = trace_res.get("spans", [])
                per_cand["trace_evidence"] = spans
                per_cand["trace_anomaly_count"] = sum(
                    1 for s in spans if s.get("status") != "ok"
                )
            # KB evidence: count keyword matches in alert messages.
            alert_text = " ".join(
                str(a.get("message", "")) for a in alerts
            ).lower()
            per_cand["kb_keyword_hits"] = sum(
                1 for term in self.EVIDENCE_TERMS
                if term in alert_text and cand.lower() in alert_text
            )
            evidence[cand] = per_cand
        return evidence

    # ------------------------------------------------------------------
    # Phase 3: re-rank using evidence.
    # ------------------------------------------------------------------
    def _rank_with_evidence(
        self,
        candidates: list[tuple[str, float, int]],
        evidence: dict[str, dict],
    ) -> list[tuple[str, float, float]]:
        scored: list[tuple[str, float, float]] = []
        for cand, base_score, kb_hits in candidates:
            ev = evidence.get(cand, {})
            evidence_bonus = 0.0
            # Metric evidence: any non-degraded snapshot strengthens the candidate.
            for m in ev.get("metric_evidence", []):
                if m.get("source") == "prometheus":
                    evidence_bonus += 0.5
            # Trace evidence: anomalous spans strongly indicate root cause.
            anomaly = ev.get("trace_anomaly_count", 0)
            if anomaly:
                evidence_bonus += min(1.5, anomaly * 0.5)
            # KB keyword hits.
            evidence_bonus += ev.get("kb_keyword_hits", 0) * 0.3

            final_score = base_score + evidence_bonus
            # Map raw final_score -> confidence bucket.
            if final_score >= 5:
                confidence = 0.92
            elif final_score >= 4:
                confidence = 0.85
            elif final_score >= 2.5:
                confidence = 0.7
            else:
                confidence = 0.5
            scored.append((cand, confidence, final_score))
        scored.sort(key=lambda x: -x[2])
        return scored

    # ------------------------------------------------------------------
    # Causal chain (unchanged from previous version).
    # ------------------------------------------------------------------
    def _build_causal_chain(
        self, root: str, topo: dict, alerted_services: set[str]
    ) -> list[str]:
        dependents: dict[str, list[str]] = defaultdict(list)
        for svc_name, svc_info in topo.items():
            for dep in svc_info.get("depends_on", []) or []:
                dependents[dep].append(svc_name)

        chain: list[str] = []
        queue: list[list[str]] = [[root]]
        while queue:
            path = queue.pop(0)
            current = path[-1]
            for nxt in dependents.get(current, []):
                if nxt in path or nxt not in alerted_services:
                    continue
                new_path = path + [nxt]
                label = ("upstream failure" if len(new_path) == 2
                         else "cascading failure")
                chain.append(f"{' -> '.join(new_path)}: {label}")
                queue.append(new_path)
        if not chain:
            chain = [f"Root cause: {root}"]
        return chain

    # ------------------------------------------------------------------
    # Helpers (unchanged from previous version).
    # ------------------------------------------------------------------
    def _find_upstream_apexes(self, svc: str, topo: dict) -> set[str]:
        apexes: set[str] = set()
        stack: list[tuple[str, set[str]]] = [(svc, set())]
        visited: set[str] = set()

        while stack:
            current, path = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            deps = topo.get(current, {}).get("depends_on", []) or []
            if not deps:
                apexes.add(current)
            else:
                for dep in deps:
                    if dep not in path:
                        stack.append((dep, path | {current}))
        return apexes

    def _score_candidate(
        self, svc: str, info: dict, alert_text: str, knowledge: list[dict]
    ) -> tuple[float, int]:
        score = 0.0
        kb_hits = 0

        if not info.get("depends_on"):
            score += 3.0

        status = info.get("status", "")
        if status == "critical":
            score += 1.0
        elif status == "warning":
            score += 0.5

        svc_lc = svc.lower()
        if self.DB_KEYWORDS.search(alert_text):
            if any(k in svc_lc for k in ("db", "database", "inventory", "primary")):
                score += 2.0
        if self.MEMORY_KEYWORDS.search(alert_text):
            if any(k in svc_lc for k in ("cache", "memory", "compute")):
                score += 2.0
        if self.NETWORK_KEYWORDS.search(alert_text):
            if any(k in svc_lc for k in ("gateway", "proxy", "edge", "lb")):
                score += 1.5

        for k in knowledge:
            if k.get("pattern_service") == svc:
                kb_hits += 1
                score += 2.0
                if k.get("confidence", 0) >= 0.9:
                    score += 0.5

        return score, kb_hits

    def _generate_fixes(
        self, root: str, info: dict, matches: list[dict]
    ) -> list[dict]:
        fixes: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def add(fix: dict) -> None:
            key = (fix.get("target", ""), fix.get("action", ""))
            if key in seen:
                return
            seen.add(key)
            fixes.append(fix)

        status = info.get("status", "unknown")
        root_lc = root.lower()

        if "db" in root_lc or "inventory" in root_lc:
            add({
                "type": "scaling",
                "target": root,
                "action": "increase_max_connections",
                "from": 50,
                "to": 200,
                "risk": "high",
                "requires_approval": True,
            })
        if status == "critical":
            add({
                "type": "traffic",
                "target": root,
                "action": "circuit_breaker_activate",
                "risk": "high",
                "requires_approval": True,
            })
        for m in matches[:2]:
            add({
                "type": "runbook",
                "target": root,
                "action": m.get("fix_action", "restart"),
                "risk": "medium",
                "source": "knowledge_base",
            })
        if not fixes:
            add({
                "type": "restart",
                "target": root,
                "action": "service_restart",
                "risk": "high",
                "requires_approval": True,
            })
        return fixes

    def fallback(self, incidents, **kw):
        return {
            "node": incidents[0] if incidents else "unknown",
            "confidence": 0.3,
            "causal_chain": [],
            "suggested_fixes": [],
            "top_k_candidates": [],
            "evidence_per_candidate": {},
            "note": "Low confidence fallback -- manual review recommended",
        }
