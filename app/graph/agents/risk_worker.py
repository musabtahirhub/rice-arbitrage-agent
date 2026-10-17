"""Nodes: market data and deterministic risk evaluation, plus the post-evaluation router."""
from typing import Literal, Optional

from langchain_core.runnables import RunnableConfig

from app.agents.state import DealState, coerce_campaign, coerce_terms
from app.agents.updates import EvaluateRiskUpdate, FetchMarketDataUpdate
from app.logger import get_logger
from app.services.deal_evaluation import evaluate_deal
from app.services.freight import estimate_freight
from app.services.market import get_benchmark_rate
from app.services.pricing import calculate_campaign_bounds

logger = get_logger("arbitrage_desk.workflow")


def fetch_market_data_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    campaign = coerce_campaign(state["campaign"])
    benchmark_fob = get_benchmark_rate(campaign.commodity, campaign.broken_percentage)
    freight = estimate_freight(campaign.origin_port_default, campaign.destination_port)
    bounds = calculate_campaign_bounds(campaign, benchmark_fob, freight)

    update = FetchMarketDataUpdate(
        benchmark_fob_usd=benchmark_fob,
        freight_cost_usd=freight,
        dynamic_fob_ceiling=bounds.dynamic_fob_ceiling,
        dynamic_cif_floor=bounds.dynamic_cif_floor,
    )
    return update.model_dump()


def evaluate_risk_node(state: DealState, config: Optional[RunnableConfig] = None) -> dict:
    curr_round = state.get("negotiation_round", 0) + 1
    result = evaluate_deal(
        campaign=coerce_campaign(state["campaign"]),
        buyer_terms=coerce_terms(state.get("buyer_terms")),
        supplier_terms=coerce_terms(state.get("supplier_terms")),
        benchmark_fob=state.get("benchmark_fob_usd", 900.0),
        freight=state.get("freight_cost_usd", 50.0),
        round_num=curr_round,
        buyer_accepted=bool(state.get("buyer_accepted", False)),
    )

    logger.info(
        f"[WORKFLOW:Node 3 Risk] Round {curr_round} Evaluation: action='{result.action}', "
        f"viable={result.viable}, net_spread=${result.net_spread:.2f}/MT, margin={result.net_margin_pct:.2f}%, "
        f"reason='{result.reason}'"
    )

    update = EvaluateRiskUpdate(
        is_deal_viable=result.viable,
        action=result.action,
        net_spread_usd=result.net_spread,
        net_margin_pct=result.net_margin_pct,
        evaluation_reason=result.reason,
        negotiation_round=curr_round,
    )
    return update.model_dump()


def _counter_node_for(active_role: str) -> Literal["counter_buyer", "counter_supplier"]:
    return "counter_buyer" if active_role == "buyer" else "counter_supplier"


def route_after_evaluation(
    state: DealState,
) -> Literal["approval_gate", "confirm_deal", "counter_buyer", "counter_supplier", "reject_deal"]:
    action = state.get("action")
    has_supplier = state.get("supplier_terms") is not None
    active_role = state.get("active_role", "buyer")

    if action == "REJECT_HARD":
        return "reject_deal"
    if action == "ACCEPT_AND_CLOSE" and has_supplier:
        return "approval_gate"
    if action == "COUNTER_TO_MAXIMIZE":
        return _counter_node_for(active_role)
    if state.get("is_deal_viable") and has_supplier:
        return "approval_gate"
    return _counter_node_for(active_role)
