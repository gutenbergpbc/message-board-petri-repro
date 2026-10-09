"""Shared schema for self-contained peer-condition definitions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    peer_prompted: bool
    actor_brief: str
    peer_request: str
    fixture_condition: str
    token_budget_notice: bool = False
    adaptive: bool = False
    system_prompt: str | None = None
    assignment: str | None = None
    source_checksums: bool = False
    turn_budget_notice: bool = False
