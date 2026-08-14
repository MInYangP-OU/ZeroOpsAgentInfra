"""MCP Server stubs -- simulate external tool connections.

In production these connect to real MCP servers via the MCP protocol.
Each stub implements the same interface contract: call(method, **params) -> result.
The schema, auth, and audit fields document the production interface.

启发 5：MCP 工具描述标准化。
每个 MCP Server 暴露 `method_schemas` 描述符（OpenAI function-calling 兼容），
让 Agent system prompt 与 LLM 工具调用有统一的契约，避免 AIOps 打工人系列
提到的"工具描述与功能不匹配"导致的失败调用。
"""
from __future__ import annotations
import logging
import time

logger = logging.getLogger(__name__)


# ---------- Method schema registry (启发 5) ----------
# Each entry follows OpenAI function-calling format so it can be passed
# directly to `tools=[...]` in any chat-completions call. The `errors`
# field documents failure modes; `_degraded` is the fallback result shape.

_PROMETHEUS_SCHEMA = [
    {
        "name": "prometheus.query",
        "description": (
            "查询指定服务在指定时间窗口内的指标当前值。仅返回单点 snapshot，"
            "不返回时间序列；如需历史序列请使用 query_range。"
        ),
        "parameters": {
            "service": {"type": "string", "required": True,
                        "description": "服务名称，如 'inventory-service'"},
            "metric": {"type": "string", "required": True,
                       "enum": ["error_rate", "latency_ms", "qps"],
                       "description": "指标名"},
            "at": {"type": "string", "required": False,
                   "description": "ISO8601 时间戳；缺省 = now"},
        },
        "returns": {"current": "float", "status": "ok|no_data|degraded"},
        "errors": {"TIMEOUT": "30s 后降级返回 degraded",
                   "AUTH_FAILED": "立即抛错并终止调用链"},
    },
    {
        "name": "prometheus.query_range",
        "description": "查询指定服务在时间窗口内的指标序列，用于异常检测 / 趋势分析。",
        "parameters": {
            "service": {"type": "string", "required": True},
            "metric": {"type": "string", "required": True,
                       "enum": ["error_rate", "latency_ms", "qps"]},
            "start": {"type": "string", "required": True, "format": "iso8601"},
            "end": {"type": "string", "required": True, "format": "iso8601"},
            "step": {"type": "string", "required": False, "default": "30s"},
        },
        "returns": {"values": "list[float]", "status": "ok|no_data"},
    },
    {
        "name": "prometheus.rate_based_query",
        "description": (
            "基于变化率（derivative）查询，用于主动感知。"
            "返回单位时间内的指标变化率，用于在告警阈值未触发前发现异常。"
        ),
        "parameters": {
            "service": {"type": "string", "required": True},
            "metric": {"type": "string", "required": True},
            "window": {"type": "string", "required": False, "default": "5m",
                       "description": "变化率计算窗口"},
            "threshold": {"type": "float", "required": True,
                          "description": "变化率阈值，超过即认为异常"},
        },
        "returns": {"rate": "float", "status": "ok|no_data|degraded"},
    },
    {
        "name": "prometheus.alert_list",
        "description": "列出当前活动告警。",
        "parameters": {
            "service": {"type": "string", "required": False,
                        "description": "可选过滤；缺省返回全部"},
            "since_minutes_ago": {"type": "integer", "required": False,
                                  "default": 60},
        },
        "returns": {"alerts": "list[dict]"},
    },
    {
        "name": "prometheus.inject_secondary_alert",
        "description": "[Test only] 注入二次告警，用于失败剧本演练。生产禁用。",
        "parameters": {
            "service": {"type": "string", "required": True},
            "type": {"type": "string", "required": True},
            "ts_minutes_ago": {"type": "integer", "required": True},
        },
        "returns": {"status": "injected"},
    },
]

_CMDB_SCHEMA = [
    {
        "name": "cmdb.get_topology",
        "description": "获取完整服务依赖拓扑图（service -> {depends_on, depended_by, status, config}）。",
        "parameters": {},
        "returns": {"topology": "dict"},
    },
    {
        "name": "cmdb.get_service",
        "description": "获取单个服务的详情（依赖、状态、当前配置）。",
        "parameters": {"service": {"type": "string", "required": True}},
        "returns": {"service_info": "dict"},
    },
]

_CICD_SCHEMA = [
    {
        "name": "cicd.deploy",
        "description": "触发部署流水线（生产/灰度/回滚）。",
        "parameters": {
            "service": {"type": "string", "required": True},
            "version": {"type": "string", "required": False},
            "track": {"type": "string", "required": False,
                      "enum": ["production", "canary", "rollback"],
                      "default": "production"},
        },
        "returns": {"deploy_id": "string", "status": "success|failed"},
    },
    {
        "name": "cicd.rollback",
        "description": "回滚到上一个稳定版本。",
        "parameters": {"service": {"type": "string", "required": True}},
        "returns": {"deploy_id": "string", "status": "success|failed"},
    },
]

_NACOS_SCHEMA = [
    {
        "name": "nacos.get_config",
        "description": "从 Nacos 配置中心读取服务配置。",
        "parameters": {"service": {"type": "string", "required": True}},
        "returns": {"config": "dict"},
    },
    {
        "name": "nacos.update_config",
        "description": "更新服务配置；高危变更（涉及连接池/超时）需 ITSM 审批。",
        "parameters": {
            "service": {"type": "string", "required": True},
            "change": {"type": "string", "required": True,
                       "description": "变更描述，如 'DB_CONN_POOL_MAX: 50 -> 200'"},
        },
        "returns": {"status": "updated", "audit": "string"},
    },
]

_ITSM_SCHEMA = [
    {
        "name": "itsm.create_ticket",
        "description": "创建 ITSM 工单（审批/关闭/告警）。",
        "parameters": {
            "ticket_type": {"type": "string", "required": True,
                            "enum": ["approval", "close", "alert"]},
            "content": {"type": "string", "required": True},
            "incident_id": {"type": "string", "required": False},
            "actions": {"type": "list", "required": False},
        },
        "returns": {"ticket_id": "string", "status": "pending_approval"},
    },
    {
        "name": "itsm.approve",
        "description": "审批工单（仅高风险操作走人工审批）。",
        "parameters": {
            "ticket_id": {"type": "string", "required": True},
            "actions": {"type": "list", "required": False},
        },
        "returns": {"ticket_id": "string", "status": "approved|denied",
                    "approved_by": "string"},
    },
]

_KB_SCHEMA = [
    {
        "name": "kb.search",
        "description": "知识库语义检索（当前为子串匹配；生产用 pgvector）。",
        "parameters": {
            "query": {"type": "string", "required": True},
            "top_k": {"type": "integer", "required": False, "default": 3},
        },
        "returns": {"results": "list[dict]"},
    },
    {
        "name": "kb.write",
        "description": "写入经验到知识库；需要审计日志；一致性校验失败时拒绝。",
        "parameters": {"content": {"type": "dict", "required": True}},
        "returns": {"status": "written", "audit": "string"},
    },
]

_TRACE_SCHEMA = [
    {
        "name": "trace.get_trace",
        "description": "获取服务最近调用链 span 列表。",
        "parameters": {
            "service": {"type": "string", "required": True},
            "trace_id": {"type": "string", "required": False},
        },
        "returns": {"spans": "list[dict]", "status": "ok|no_data"},
    },
    {
        "name": "trace.search_by_error",
        "description": "按错误模式搜索异常 span，用于根因证据收集。",
        "parameters": {
            "service": {"type": "string", "required": True},
            "error_pattern": {"type": "string", "required": True},
            "time_window": {"type": "string", "required": False, "default": "5m"},
        },
        "returns": {"spans": "list[dict]"},
    },
    {
        "name": "trace.inject_span",
        "description": "[Test only] 注入异常 span。生产禁用。",
        "parameters": {
            "service": {"type": "string", "required": True},
            "span": {"type": "string", "required": True},
            "duration_ms": {"type": "integer", "required": False},
            "status": {"type": "string", "required": False, "default": "error"},
        },
        "returns": {"status": "injected"},
    },
]


class MCPServerStub:
    """Base MCP server stub with audit logging and tool schema registry."""

    # Subclasses override this to declare their public methods.
    method_schemas: list[dict] = []

    def __init__(self, name: str, auth: str = "api_token"):
        self.name = name
        self.auth = auth
        self.call_log: list[dict] = []

    def call(self, method: str, **params) -> dict:
        entry = {"server": self.name, "method": method,
                 "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "params": {k: str(v)[:100] for k, v in params.items()}}
        result = self._dispatch(method, **params)
        entry["status"] = "success"
        self.call_log.append(entry)
        return result

    def describe(self) -> list[dict]:
        """Return tool schemas (OpenAI function-calling format) for prompt use."""
        return list(self.method_schemas)

    def _dispatch(self, method: str, **params) -> dict:
        raise NotImplementedError


class PrometheusMCP(MCPServerStub):
    """Prometheus/VictoriaMetrics MCP -- metric queries and alerts."""

    method_schemas = _PROMETHEUS_SCHEMA

    DEFAULT_SERIES: dict[str, dict[str, list[float]]] = {
        "order-service": {
            "error_rate": [0.15, 0.12, 0.08, 0.05, 0.03, 0.02, 0.01],
            "latency_ms": [850, 700, 500, 300, 150, 80, 45],
            "qps": [1200, 1100, 1150, 1180, 1200, 1220, 1210],
        },
        "payment-service": {
            "error_rate": [0.30, 0.20, 0.10, 0.05, 0.02, 0.01, 0.005],
            "latency_ms": [1200, 800, 400, 200, 100, 60, 30],
            "qps": [800, 750, 780, 790, 800, 810, 805],
        },
        "inventory-service": {
            "error_rate": [0.40, 0.30, 0.20, 0.10, 0.05, 0.02, 0.01],
            "latency_ms": [2000, 1500, 800, 400, 200, 100, 50],
            "qps": [600, 580, 590, 595, 600, 605, 600],
        },
        "api-gateway": {
            "error_rate": [0.10, 0.08, 0.05, 0.03, 0.02, 0.01, 0.005],
            "latency_ms": [500, 350, 200, 120, 70, 50, 40],
            "qps": [3000, 2900, 2950, 3000, 3050, 3100, 3080],
        },
    }

    def __init__(self):
        super().__init__("prometheus-mcp", "api_token")
        self._series: dict[str, dict[str, list[float]]] = {
            svc: {m: list(vals) for m, vals in series.items()}
            for svc, series in self.DEFAULT_SERIES.items()
        }
        self._secondary_alerts: list[dict] = []

    def _dispatch(self, method: str, **params) -> dict:
        if method == "query":
            service = params.get("service", "")
            metric = params.get("metric", "")
            series = self._series.get(service, {}).get(metric, [])
            return {
                "metric": metric,
                "service": service,
                "values": list(series),
                "current": series[-1] if series else None,
                "status": "ok" if series else "no_data",
            }
        elif method == "query_range":
            service = params.get("service", "")
            metric = params.get("metric", "")
            series = self._series.get(service, {}).get(metric, [])
            return {
                "metric": metric,
                "service": service,
                "values": list(series),
                "status": "ok" if series else "no_data",
            }
        elif method == "rate_based_query":
            # Compute simple rate from the last two points (derivative).
            service = params.get("service", "")
            metric = params.get("metric", "")
            threshold = float(params.get("threshold", 0.1))
            series = self._series.get(service, {}).get(metric, [])
            if len(series) < 2:
                return {"rate": 0.0, "status": "no_data"}
            rate = (series[-1] - series[-2]) / max(abs(series[-2]), 1e-9)
            return {
                "rate": round(rate, 4),
                "above_threshold": abs(rate) > threshold,
                "status": "ok",
            }
        elif method == "alert_list":
            return {"alerts": list(self._secondary_alerts)}
        elif method == "set_metric":
            service = params.get("service", "")
            metric = params.get("metric", "")
            values = params.get("values", [])
            if service not in self._series:
                self._series[service] = {}
            self._series[service][metric] = list(values)
            return {"status": "updated"}
        elif method == "inject_secondary_alert":
            alert = {
                "service": params.get("service", ""),
                "type": params.get("type", ""),
                "ts_minutes_ago": params.get("ts_minutes_ago", 0),
            }
            self._secondary_alerts.append(alert)
            return {"status": "injected"}
        return {"status": "unknown_method"}


class CMDBMCP(MCPServerStub):
    """CMDB MCP -- service topology and dependency queries."""

    method_schemas = _CMDB_SCHEMA

    def __init__(self, topology: dict | None = None):
        super().__init__("cmdb-mcp", "mTLS")
        self._topo = topology or {}

    def _dispatch(self, method: str, **params) -> dict:
        if method == "get_topology":
            return self._topo
        elif method == "get_service":
            svc = params.get("service", "")
            return self._topo.get(svc, {"status": "unknown"})
        return {"status": "unknown_method"}


class CICDMCP(MCPServerStub):
    """CI/CD MCP -- deployment pipeline triggers."""

    method_schemas = _CICD_SCHEMA

    def __init__(self):
        super().__init__("cicd-mcp", "OAuth2")

    def _dispatch(self, method: str, **params) -> dict:
        if method == "deploy":
            track = params.get("track", "production")
            return {
                "deploy_id": f"deploy-{int(time.time())}-{track}",
                "track": track,
                "status": "success",
            }
        elif method == "rollback":
            return {"deploy_id": f"rollback-{int(time.time())}", "status": "success"}
        return {"status": "unknown_method"}


class NacosMCP(MCPServerStub):
    """Nacos MCP -- service configuration queries and updates."""

    method_schemas = _NACOS_SCHEMA

    def __init__(self, configs: dict | None = None):
        super().__init__("nacos-mcp", "mTLS")
        self._configs = configs or {}

    def _dispatch(self, method: str, **params) -> dict:
        if method == "get_config":
            sid = params.get("service", "")
            return self._configs.get(sid, {"DB_CONN_POOL_MAX": 50})
        elif method == "update_config":
            return {"status": "updated", "audit": f"nacos-{int(time.time())}"}
        return {"status": "unknown_method"}


class ITSMMCP(MCPServerStub):
    """ITSM MCP -- ticket creation, approval, and audit.

    Approval state machine:
      pending_approval -> approved (default) | denied (high-risk + audit failure)
    """

    method_schemas = _ITSM_SCHEMA

    def __init__(self):
        super().__init__("itsm-mcp", "api_token")
        self._tickets: dict[str, dict] = {}

    def _dispatch(self, method: str, **params) -> dict:
        if method == "create_ticket":
            ticket_id = f"TKT-{int(time.time() * 1000)}"
            ticket = {
                "ticket_id": ticket_id,
                "status": "pending_approval",
                "actions": params.get("actions", []),
                "incident_id": params.get("incident_id", ""),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._tickets[ticket_id] = ticket
            return ticket
        elif method == "approve":
            ticket_id = params.get("ticket_id", "")
            ticket = self._tickets.get(ticket_id, {})
            actions = params.get("actions", ticket.get("actions", []))
            approver = self._select_approver(actions)
            ticket["status"] = "approved"
            ticket["approved_by"] = approver
            ticket["approved_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            return {
                "ticket_id": ticket_id,
                "status": "approved",
                "approved_by": approver,
            }
        elif method == "deny":
            ticket_id = params.get("ticket_id", "")
            ticket = self._tickets.get(ticket_id, {})
            ticket["status"] = "denied"
            return {"ticket_id": ticket_id, "status": "denied"}
        return {"status": "unknown_method"}

    @staticmethod
    def _select_approver(actions: list) -> str:
        risks = {a.get("risk", "low") for a in actions or []}
        if "high" in risks:
            return "human:sre-team-lead"
        if "medium" in risks:
            return "human:ops-oncall"
        return "auto-approver"


class KnowledgeBaseMCP(MCPServerStub):
    """Knowledge Base MCP -- vector search and knowledge write via PolarDB PG."""

    method_schemas = _KB_SCHEMA

    def __init__(self, knowledge: list | None = None):
        super().__init__("kb-mcp", "DB-Auth")
        self._kb = knowledge or []
        self._written: list[dict] = []

    def _dispatch(self, method: str, **params) -> dict:
        if method == "search":
            query = str(params.get("query", "")).lower().strip()
            top_k = int(params.get("top_k", 3))
            if query:
                matched = [
                    k
                    for k in self._kb
                    if (
                        query in str(k.get("pattern_service", "")).lower()
                        or query in str(k.get("root_cause", "")).lower()
                        or query in str(k.get("fix_action", "")).lower()
                    )
                ]
            else:
                matched = list(self._kb)
            if not matched:
                matched = list(self._kb)
            return {"results": matched[:top_k]}
        elif method == "write":
            audit_id = f"kb-{int(time.time() * 1000)}"
            entry = {
                "audit": audit_id,
                "content": params.get("content", {}),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._written.append(entry)
            return {"status": "written", "audit": audit_id}
        return {"status": "unknown_method"}


class TraceMCP(MCPServerStub):
    """Trace MCP -- distributed tracing queries (ARMS/Jaeger)."""

    method_schemas = _TRACE_SCHEMA

    DEFAULT_TRACES: dict[str, list[dict]] = {
        "order-service": [
            {"span": "POST /orders", "duration_ms": 45, "status": "ok"},
            {"span": "inventory-service.getStock", "duration_ms": 30, "status": "ok"},
        ],
        "payment-service": [
            {"span": "POST /pay", "duration_ms": 30, "status": "ok"},
            {"span": "order-service.createOrder", "duration_ms": 25, "status": "ok"},
        ],
        "inventory-service": [
            {"span": "GET /stock", "duration_ms": 50, "status": "ok"},
            {"span": "db.primary.query", "duration_ms": 20, "status": "ok"},
        ],
        "api-gateway": [
            {"span": "route /orders", "duration_ms": 40, "status": "ok"},
        ],
    }

    def __init__(self):
        super().__init__("trace-mcp", "api_token")
        self._traces: dict[str, list[dict]] = {
            svc: [dict(span) for span in spans]
            for svc, spans in self.DEFAULT_TRACES.items()
        }

    def _dispatch(self, method: str, **params) -> dict:
        if method == "get_trace":
            svc = params.get("service", "")
            spans = self._traces.get(svc)
            if spans is None:
                return {"service": svc, "spans": [], "status": "no_data"}
            return {
                "service": svc,
                "trace_id": params.get("trace_id", f"trace-{svc}"),
                "spans": spans,
                "status": "ok",
            }
        elif method == "search_by_error":
            svc = params.get("service", "")
            pattern = str(params.get("error_pattern", "")).lower()
            spans = self._traces.get(svc, [])
            matched = [
                s for s in spans
                if pattern in str(s.get("span", "")).lower()
                or s.get("status") != "ok"
            ]
            return {"service": svc, "spans": matched, "status": "ok"}
        elif method == "inject_span":
            svc = params.get("service", "")
            span = {
                "span": params.get("span", "unknown"),
                "duration_ms": params.get("duration_ms", 0),
                "status": params.get("status", "error"),
            }
            self._traces.setdefault(svc, []).append(span)
            return {"status": "injected"}
        return {"status": "unknown_method"}
