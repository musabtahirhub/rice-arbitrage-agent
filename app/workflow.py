import app.graph
from app.graph import (
    build_trade_graph,
    build_arbitrage_graph,
    route_entry_point,
    setup_checkpointer,
    get_trade_graph,
    get_checkpointer,
)

checkpointer = app.graph.checkpointer
trade_graph = app.graph.trade_graph


def __getattr__(name: str):
    if name == "trade_graph":
        return app.graph.get_trade_graph()
    if name == "checkpointer":
        return app.graph.get_checkpointer()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
from langchain_google_genai import ChatGoogleGenerativeAI
from app.services.llm_service import (
    _get_llm,
    get_llm,
    generate_dynamic_llm_draft,
    generate_proactive_sco_draft,
    split_subject_and_body,
    _parse_email_content,
    _extract_terms_via_gemini_llm,
)
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
