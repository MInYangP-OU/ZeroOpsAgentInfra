"""Agent base class with identity declaration, skill binding, and MCP integration."""
from __future__ import annotations
import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentIdentity:
    """Declarative Agent identity -- maps to AgentTeams role orchestration."""
    name: str
    role: str
    description: str
    capabilities: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    upstream: list[str] = field(default_factory=list)
    downstream: list[str] = field(default_factory=list)
    security_level: str = "read-only"
    failure_strategy: str = "degrade"


class AgentBase:
    """Base class for all agents in the AgentTeams framework.

    Provides identity declaration, skill registration, MCP tool integration,
    and execution trace logging for observability.
    """

    def __init__(self, identity: AgentIdentity, skills=None, mcp_clients=None):
        self.identity = identity
        self.skills = skills or {}
        self.mcp_clients = mcp_clients or {}
        self.trace = []
        self._agent_id = f"{identity.name}-{uuid.uuid4().hex[:8]}"

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def register_skill(self, name: str, skill_instance: Any) -> None:
        self.skills[name] = skill_instance
        logger.info(f"[{self.identity.name}] Skill registered: {name}")

    def invoke_skill(self, skill_name: str, **kwargs) -> Any:
        """Invoke a registered skill with full trace logging."""
        if skill_name not in self.skills:
            raise ValueError(f"Skill '{skill_name}' not registered on '{self.identity.name}'")
        skill = self.skills[skill_name]
        start = time.time()
        trace_entry = {
            "agent": self.identity.name,
            "agent_id": self._agent_id,
            "skill": skill_name,
            "input_keys": list(kwargs.keys()),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "started",
        }
        try:
            result = skill.execute(**kwargs)
            elapsed = round((time.time() - start) * 1000, 2)
            trace_entry["status"] = "success"
            trace_entry["elapsed_ms"] = elapsed
            trace_entry["output_summary"] = str(result)[:300]
            self.trace.append(trace_entry)
            logger.info(f"[{self.identity.name}] '{skill_name}' done in {elapsed}ms")
            return result
        except Exception as e:
            elapsed = round((time.time() - start) * 1000, 2)
            trace_entry["status"] = "failed"
            trace_entry["elapsed_ms"] = elapsed
            trace_entry["error"] = str(e)
            self.trace.append(trace_entry)
            logger.error(f"[{self.identity.name}] '{skill_name}' failed: {e}")
            if self.identity.failure_strategy == "degrade":
                fallback = getattr(skill, "fallback", None)
                if fallback:
                    logger.warning(f"[{self.identity.name}] Degrading after '{skill_name}' failure")
                    return fallback(**kwargs)
            raise

    def call_mcp(self, server_name: str, method: str, **params) -> Any:
        if server_name not in self.mcp_clients:
            raise ValueError(f"MCP server '{server_name}' not connected")
        client = self.mcp_clients[server_name]
        start = time.time()
        result = client.call(method, **params)
        elapsed = round((time.time() - start) * 1000, 2)
        self.trace.append({
            "agent": self.identity.name, "mcp_server": server_name,
            "method": method, "elapsed_ms": elapsed,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "success",
        })
        return result

    def get_trace(self) -> list[dict]:
        return self.trace

    def execute(self, context: dict) -> dict:
        raise NotImplementedError
