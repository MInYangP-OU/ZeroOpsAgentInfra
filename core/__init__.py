"""Core framework: AgentTeams coordination, Agent base, context bundle, state machine."""
from .agent_base import AgentBase, AgentIdentity
from .agent_teams import AgentTeams
from .context_bundle import IncidentContextBundle
from .state_machine import IncidentStateMachine, IncidentState
