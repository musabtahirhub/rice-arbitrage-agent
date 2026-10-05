"""Backward-compatibility shim — all logic has moved to app.graph, app.agents.*, and app.services.llm_service."""

# Re-export graph objects
from app.graph import (
    build_trade_graph,
    build_arbitrage_graph,
    checkpointer,
    route_entry_point,
    setup_checkpointer,
    trade_graph,
)

# Re-export LLM service functions
from app.services.llm_service import (
    generate_dynamic_llm_draft,
    generate_proactive_sco_draft,
    split_subject_and_body,
    _parse_email_content,
)

# Re-export node functions
from app.agents.buyer_agent import proactive_outreach_node, counter_buyer_node
from app.agents.supplier_agent import counter_supplier_node
from app.agents.risk_worker import (
    parse_incoming_email_node,
    fetch_market_data_node,
    evaluate_risk_node,
    route_after_evaluation,
)
from app.agents.finalization import (
    approval_gate_node,
    confirm_deal_node,
    reject_deal_node,
)


def run_full_autonomous_campaign(campaign_id: str):
    from app.services.campaign_runner import run_full_autonomous_campaign as _runner
    return _runner(campaign_id)
