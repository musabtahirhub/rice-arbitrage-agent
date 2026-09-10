"""
LangGraph workflow — thin entry point for the multi-agent arbitrage system.

Delegates all coordination to the Orchestrator, which manages the Buyer Agent,
Supplier Agent, and deterministic Risk Worker.  This module exists for backward
compatibility and as a single import point.

For the full multi-agent graph topology, see ``app.agents.orchestrator``.
"""

from __future__ import annotations

from langgraph.graph import StateGraph

from app.agents.orchestrator import build_orchestrator


def build_workflow() -> StateGraph:
    """
    Construct and compile the multi-agent LangGraph state graph.

    Delegates to ``orchestrator.build_orchestrator()`` which assembles
    the full Buyer Agent → Supplier Agent → Risk Worker pipeline.
    """
    return build_orchestrator()
