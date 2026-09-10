---
trigger: always_on
---

# Multi-Agent Commodity Arbitrage Architecture Spec

## Agents & Isolation
1. **Orchestrator Agent:** Maintains global state, synchronization between buyer and seller threads, and hand-off triggers.
2. **Buyer Agent:** Handles Middle East client relations, extracts CIF specs, and issues non-binding SCOs.
3. **Supplier Agent:** Handles Southeast Asian exporter relations, negotiates FOB rates, and validates volume allocation.
4. **Market & Risk Worker:** Deterministic service calculating landed costs, freight rates, and net margins against live benchmark feeds.

## Core Rules
- Agents communicate state changes through LangGraph shared state dictionaries.
- Neither the Buyer Agent nor Supplier Agent can finalize terms autonomously; finalization requires approval from the deterministic Risk Worker.
- Zero-risk short squeeze rule: The Buyer Agent must never confirm an order until the Supplier Agent has locked allocation.