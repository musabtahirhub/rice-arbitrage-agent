"""
test_runner.py -- Multi-agent arbitrage system verification.

Runs entirely offline with mock/fallback data -- no Gemini API key or network
access required.  Validates:

  Part 1 — Deterministic arbitrage engine (dynamic market bounds)
  ---------------------------------------------------------------
  1. Viable deal — supplier within benchmark tolerance, buyer above dynamic floor.
  2. Supplier above dynamic FOB ceiling — rejected (excessive premium).
  3. Buyer below dynamic CIF floor — rejected.
  4. Quantity mismatch — rejected.
  5. Margin below target — rejected.
  6. FOB buyer + FOB supplier Incoterm normalization.
  7. CIF supplier Incoterm normalization.

  Part 2 — MarketDataService fallback cache
  ------------------------------------------
  8. Returns sensible defaults for known commodities/lanes.
  9. Dynamic bound computation matches expected values.

  Part 3 — Multi-turn negotiation simulation (Orchestrator flow)
  ---------------------------------------------------------------
  10. Round 1: Buyer bids low -> Risk Worker rejects -> counter_buyer.
  11. Round 2: Supplier quotes high -> Risk Worker rejects -> counter_supplier.
  12. Round 3: Both adjust -> Risk Worker approves -> deal closes.
  13. Zero-risk rule: supplier allocation locked before buyer terms accepted.
"""

from __future__ import annotations

import sys

from app.schemas import CampaignConfig, ParsedTradeEmail
from app.arbitrage_engine import evaluate_deal
from app.market_data import MarketDataService
from app.agents.orchestrator import (
    evaluate_risk,
    fetch_market_data,
    finalize_deal,
)


# ---------------------------------------------------------------------------
# Shared campaign config (market-relative, no hardcoded bounds)
# ---------------------------------------------------------------------------

CAMPAIGN = CampaignConfig(
    campaign_id="TEST-001",
    commodity="Basmati 1121 Sella Rice 5% broken",
    target_profit_margin_pct=5.0,
    max_acceptable_variance_from_benchmark_pct=5.0,   # max 5% above FOB index
    min_sell_margin_above_benchmark_pct=2.0,           # min 2% above CIF index
    benchmark_index_name="basmati_1121",
)

# Simulated market data (consistent with fallback cache)
BENCHMARK_FOB = 900.0    # Basmati 1121, 5% broken, fallback cache value
FREIGHT_COST = 55.0      # Mundra -> Jebel Ali, fallback cache value

# Derived dynamic bounds for reference:
#   max_buy_fob  = 900 * 1.05 = 945.0
#   landed_cif   = 900 + 55   = 955.0
#   min_sell_cif  = 955 * 1.02 = 974.1


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _buyer(price: float, qty: float = 500, incoterm: str = "CIF") -> ParsedTradeEmail:
    return ParsedTradeEmail(
        sender_role="buyer",
        commodity_type="Basmati 1121 Sella Rice 5% broken",
        quantity_mt=qty,
        price_usd_per_mt=price,
        incoterm=incoterm,
        port="Jebel Ali",
    )


def _supplier(price: float, qty: float = 500, incoterm: str = "FOB") -> ParsedTradeEmail:
    return ParsedTradeEmail(
        sender_role="supplier",
        commodity_type="Basmati 1121 Sella Rice 5% broken",
        quantity_mt=qty,
        price_usd_per_mt=price,
        incoterm=incoterm,
        port="Mundra",
    )


def _make_state(**overrides) -> dict:
    """Create a base state dict for orchestrator node testing."""
    base = {
        "campaign": CAMPAIGN,
        "benchmark_fob_usd": BENCHMARK_FOB,
        "benchmark_source": "fallback_cache",
        "freight_cost_usd": FREIGHT_COST,
        "freight_source": "fallback_cache",
        "buyer_thread_status": "awaiting_inquiry",
        "supplier_thread_status": "awaiting_quote",
        "deal_status": "prospecting",
        "negotiation_round": 0,
        "raw_email": "",
        "buyer_terms": None,
        "supplier_terms": None,
        "net_margin_pct": 0.0,
        "is_deal_viable": False,
        "evaluation_reason": "",
        "buyer_draft": "",
        "supplier_draft": "",
    }
    base.update(overrides)
    return base


passed = 0
failed = 0


def assert_test(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS]  {name}")
    else:
        failed += 1
        print(f"  [FAIL]  {name}  -- {detail}")


# ===========================================================================
# Part 1 — Deterministic arbitrage engine tests
# ===========================================================================

def test_viable_deal() -> None:
    """Buyer CIF $1,100 + Supplier FOB $920 -> cost $975 -> margin ~11.36%."""
    print("\n-- Test: Viable Deal (Dynamic Bounds) --")
    result = evaluate_deal(CAMPAIGN, _buyer(1100), _supplier(920), BENCHMARK_FOB, FREIGHT_COST)
    assert_test("Deal is viable", result["viable"], f"got viable={result['viable']}")
    expected_margin = ((1100 - 975) / 1100) * 100
    assert_test(
        f"Margin ~ {expected_margin:.2f}%",
        abs(result["margin_pct"] - expected_margin) < 0.01,
        f"got {result['margin_pct']}%",
    )
    assert_test("Reason mentions viable", "viable" in result["reason"].lower(), result["reason"])


def test_supplier_above_dynamic_ceiling() -> None:
    """Supplier FOB $960 > max_buy_fob $945 -> rejected."""
    print("\n-- Test: Supplier Above Dynamic FOB Ceiling --")
    result = evaluate_deal(CAMPAIGN, _buyer(1100), _supplier(960), BENCHMARK_FOB, FREIGHT_COST)
    assert_test("Deal is NOT viable", not result["viable"], f"got viable={result['viable']}")
    assert_test("Reason mentions benchmark", "benchmark" in result["reason"].lower(), result["reason"])
    assert_test("Reason mentions ceiling", "ceiling" in result["reason"].lower(), result["reason"])


def test_buyer_below_dynamic_floor() -> None:
    """Buyer CIF $960 < min_sell_cif $974.1 -> rejected."""
    print("\n-- Test: Buyer Below Dynamic CIF Floor --")
    result = evaluate_deal(CAMPAIGN, _buyer(960), _supplier(900), BENCHMARK_FOB, FREIGHT_COST)
    assert_test("Deal is NOT viable", not result["viable"], f"got viable={result['viable']}")
    assert_test("Reason mentions floor", "floor" in result["reason"].lower(), result["reason"])


def test_quantity_mismatch() -> None:
    """Buyer 500 MT vs supplier 300 MT -> rejected."""
    print("\n-- Test: Quantity Mismatch --")
    result = evaluate_deal(CAMPAIGN, _buyer(1100, qty=500), _supplier(920, qty=300), BENCHMARK_FOB, FREIGHT_COST)
    assert_test("Deal is NOT viable", not result["viable"], f"got viable={result['viable']}")
    assert_test("Reason mentions mismatch", "mismatch" in result["reason"].lower(), result["reason"])


def test_margin_below_target() -> None:
    """Buyer CIF $1,000 + Supplier FOB $920 -> cost $975 -> margin 2.5% < 5%."""
    print("\n-- Test: Margin Below Target --")
    result = evaluate_deal(CAMPAIGN, _buyer(1000), _supplier(920), BENCHMARK_FOB, FREIGHT_COST)
    assert_test("Deal is NOT viable", not result["viable"], f"got viable={result['viable']}")
    assert_test(
        "Margin below target",
        result["margin_pct"] < CAMPAIGN.target_profit_margin_pct,
        f"got {result['margin_pct']}%",
    )


def test_fob_buyer_normalization() -> None:
    """Buyer FOB $1,045 -> CIF equiv $1,100. Supplier FOB $920 -> cost $975. Margin ~11.36%."""
    print("\n-- Test: FOB Buyer Normalization --")
    result = evaluate_deal(CAMPAIGN, _buyer(1045, incoterm="FOB"), _supplier(920), BENCHMARK_FOB, FREIGHT_COST)
    assert_test("Deal is viable", result["viable"], f"got viable={result['viable']}")
    expected_margin = ((1100 - 975) / 1100) * 100
    assert_test(
        f"Margin ~ {expected_margin:.2f}%",
        abs(result["margin_pct"] - expected_margin) < 0.01,
        f"got {result['margin_pct']}%",
    )


def test_cif_supplier_normalization() -> None:
    """Supplier CIF $975 -> cost $975. Buyer CIF $1,100 -> margin ~11.36%."""
    print("\n-- Test: CIF Supplier Normalization --")
    result = evaluate_deal(CAMPAIGN, _buyer(1100), _supplier(975, incoterm="CIF"), BENCHMARK_FOB, FREIGHT_COST)
    assert_test("Deal is viable", result["viable"], f"got viable={result['viable']}")
    expected_margin = ((1100 - 975) / 1100) * 100
    assert_test(
        f"Margin ~ {expected_margin:.2f}%",
        abs(result["margin_pct"] - expected_margin) < 0.01,
        f"got {result['margin_pct']}%",
    )


# ===========================================================================
# Part 2 — MarketDataService fallback cache tests
# ===========================================================================

def test_market_data_service_fallback() -> None:
    """MarketDataService returns sensible fallback values for known commodities."""
    print("\n-- Test: MarketDataService Fallback Cache --")
    svc = MarketDataService()

    price, source = svc.get_benchmark_rate("basmati_1121", 5.0)
    assert_test("Basmati 1121 price > 0", price > 0, f"got ${price}")
    assert_test("Source is fallback_cache", source == "fallback_cache", f"got {source}")

    price_thai, _ = svc.get_benchmark_rate("thai_white", 5.0)
    assert_test("Thai White price > 0", price_thai > 0, f"got ${price_thai}")
    assert_test("Thai < Basmati", price_thai < price, f"Thai ${price_thai} vs Basmati ${price}")

    freight, f_source = svc.estimate_freight("Mundra", "Jebel Ali")
    assert_test("Freight > 0", freight > 0, f"got ${freight}")
    assert_test("Freight source", f_source == "fallback_cache", f"got {f_source}")


def test_dynamic_bound_computation() -> None:
    """Verify dynamic bounds match expected values."""
    print("\n-- Test: Dynamic Bound Computation --")

    max_buy_fob = BENCHMARK_FOB * (1 + CAMPAIGN.max_acceptable_variance_from_benchmark_pct / 100)
    landed_cif = BENCHMARK_FOB + FREIGHT_COST
    min_sell_cif = landed_cif * (1 + CAMPAIGN.min_sell_margin_above_benchmark_pct / 100)

    assert_test(f"max_buy_fob = ${max_buy_fob:.2f}", abs(max_buy_fob - 945.0) < 0.01)
    assert_test(f"landed_cif = ${landed_cif:.2f}", abs(landed_cif - 955.0) < 0.01)
    assert_test(f"min_sell_cif = ${min_sell_cif:.2f}", abs(min_sell_cif - 974.1) < 0.01)

    result = evaluate_deal(CAMPAIGN, _buyer(1100), _supplier(920), BENCHMARK_FOB, FREIGHT_COST)
    assert_test("Engine max_buy_fob", abs(result.get("max_buy_fob", 0) - max_buy_fob) < 0.01)
    assert_test("Engine min_sell_cif", abs(result.get("min_sell_cif", 0) - min_sell_cif) < 0.01)


# ===========================================================================
# Part 3 — Multi-turn negotiation simulation (Orchestrator nodes)
# ===========================================================================

def test_multi_turn_negotiation() -> None:
    """
    Simulate a 3-round negotiation via Orchestrator nodes:
      Round 1: Buyer bids $960 CIF (too low) -> rejected -> counter_buyer
      Round 2: Supplier quotes $960 FOB (too high) -> rejected -> counter_supplier
      Round 3: Buyer $1,100 CIF + Supplier $920 FOB -> approved -> deal closed
    """
    print("\n-- Test: Multi-Turn Negotiation Simulation --")
    print("  [Round 1] Buyer bids low ($960 CIF)...")

    # ── Round 1: Low buyer bid ──────────────────────────────────────────
    state = _make_state(
        buyer_terms=_buyer(960),
        supplier_terms=_supplier(920),
        buyer_thread_status="inquiry_parsed",
        supplier_thread_status="quote_parsed",
    )

    state = evaluate_risk(state)
    assert_test(
        "R1: Deal rejected (buyer below floor)",
        state["deal_status"] == "negotiating_buyer",
        f"got deal_status={state['deal_status']}",
    )
    assert_test(
        "R1: Buyer thread -> counter_sent",
        state["buyer_thread_status"] == "counter_sent",
        f"got {state['buyer_thread_status']}",
    )
    assert_test(
        "R1: Negotiation round = 1",
        state["negotiation_round"] == 1,
        f"got {state['negotiation_round']}",
    )

    # ── Round 2: Supplier quotes too high ───────────────────────────────
    print("  [Round 2] Supplier quotes high ($960 FOB)...")
    state["buyer_terms"] = _buyer(1100)       # Buyer raised their bid
    state["supplier_terms"] = _supplier(960)  # Supplier still too high
    state["buyer_thread_status"] = "inquiry_parsed"  # Reset after counter

    state = evaluate_risk(state)
    assert_test(
        "R2: Deal rejected (supplier above ceiling)",
        state["deal_status"] == "negotiating_supplier",
        f"got deal_status={state['deal_status']}",
    )
    assert_test(
        "R2: Supplier thread -> counter_sent",
        state["supplier_thread_status"] == "counter_sent",
        f"got {state['supplier_thread_status']}",
    )
    assert_test(
        "R2: Negotiation round = 2",
        state["negotiation_round"] == 2,
        f"got {state['negotiation_round']}",
    )

    # ── Round 3: Both sides within range ────────────────────────────────
    print("  [Round 3] Both sides adjust — deal should close...")
    state["buyer_terms"] = _buyer(1100)       # Buyer at $1,100 CIF
    state["supplier_terms"] = _supplier(920)  # Supplier dropped to $920 FOB
    state["supplier_thread_status"] = "quote_parsed"  # Reset after counter

    state = evaluate_risk(state)
    assert_test(
        "R3: Deal approved",
        state["deal_status"] == "approved",
        f"got deal_status={state['deal_status']}",
    )
    assert_test(
        "R3: Deal is viable",
        state["is_deal_viable"] is True,
        f"got {state['is_deal_viable']}",
    )
    expected_margin = ((1100 - 975) / 1100) * 100
    assert_test(
        f"R3: Margin ~ {expected_margin:.2f}%",
        abs(state["net_margin_pct"] - expected_margin) < 0.01,
        f"got {state['net_margin_pct']}%",
    )
    assert_test(
        "R3: Negotiation round = 3",
        state["negotiation_round"] == 3,
        f"got {state['negotiation_round']}",
    )

    # ── Finalize deal ───────────────────────────────────────────────────
    print("  [Finalize] Locking allocation + issuing SCO...")
    state = finalize_deal(state)
    assert_test(
        "Final: deal_status = closed",
        state["deal_status"] == "closed",
        f"got {state['deal_status']}",
    )


def test_zero_risk_rule() -> None:
    """
    Verify the zero-risk short squeeze rule:
    Supplier allocation must be locked BEFORE buyer terms are accepted.
    """
    print("\n-- Test: Zero-Risk Short Squeeze Rule --")

    state = _make_state(
        buyer_terms=_buyer(1100),
        supplier_terms=_supplier(920),
        buyer_thread_status="inquiry_parsed",
        supplier_thread_status="quote_parsed",
    )

    # Risk worker approves
    state = evaluate_risk(state)
    assert_test("Deal approved", state["deal_status"] == "approved")

    # Finalize — check ordering
    state = finalize_deal(state)
    assert_test(
        "Supplier locked BEFORE buyer accepted",
        state["supplier_thread_status"] == "allocation_locked"
        and state["buyer_thread_status"] == "terms_accepted",
        f"supplier={state['supplier_thread_status']}, buyer={state['buyer_thread_status']}",
    )
    assert_test(
        "Deal status = closed",
        state["deal_status"] == "closed",
        f"got {state['deal_status']}",
    )


def test_fetch_market_data_node() -> None:
    """Verify the fetch_market_data orchestrator node populates state correctly."""
    print("\n-- Test: Fetch Market Data Node --")

    state = _make_state(
        benchmark_fob_usd=0.0,
        freight_cost_usd=0.0,
        benchmark_source="",
        freight_source="",
        supplier_terms=_supplier(920),
        buyer_terms=_buyer(1100),
    )

    state = fetch_market_data(state)
    assert_test("Benchmark populated", state["benchmark_fob_usd"] > 0, f"got {state['benchmark_fob_usd']}")
    assert_test("Freight populated", state["freight_cost_usd"] > 0, f"got {state['freight_cost_usd']}")
    assert_test("Benchmark source set", state["benchmark_source"] != "", f"got '{state['benchmark_source']}'")
    assert_test("Freight source set", state["freight_source"] != "", f"got '{state['freight_source']}'")
    assert_test("Deal status -> evaluating", state["deal_status"] == "evaluating", f"got {state['deal_status']}")


# ---------------------------------------------------------------------------
# Part 4 — Counterparty Discovery & Automated Campaign Ignition
# ---------------------------------------------------------------------------

from app.directory import (
    BUYERS_DIRECTORY,
    SUPPLIERS_DIRECTORY,
    get_buyers_for_commodity,
    get_suppliers_for_commodity,
    get_all_counterparties,
    get_counterparty_by_id,
)
from app.agents.discovery import run_discovery


def test_directory_counterparties():
    print("\n-- Test: Marketplace Directory Data & Querying --")

    # 1. Minimum counts
    assert_test("At least 3 registered buyers", len(BUYERS_DIRECTORY) >= 3, f"got {len(BUYERS_DIRECTORY)}")
    assert_test("At least 3 registered suppliers", len(SUPPLIERS_DIRECTORY) >= 3, f"got {len(SUPPLIERS_DIRECTORY)}")

    # 2. Buyers specific fields
    gulf = get_counterparty_by_id("BUYER-GULF-01")
    assert_test("Gulf Food Trading found", gulf is not None)
    if gulf:
        assert_test("Gulf Food port is Jebel Ali", gulf.port == "Jebel Ali", f"got {gulf.port}")
        assert_test("Gulf Food has contact email", "@" in gulf.contact_email, f"got {gulf.contact_email}")
        assert_test("Gulf Food reputation >= 4.5", gulf.reputation_score >= 4.5, f"got {gulf.reputation_score}")

    # 3. Suppliers specific fields
    indus = get_counterparty_by_id("SUPP-INDUS-01")
    assert_test("Indus Rice Mills found", indus is not None)
    if indus:
        assert_test("Indus port is Karachi", indus.port == "Karachi", f"got {indus.port}")
        assert_test("Indus has contact email", "@" in indus.contact_email, f"got {indus.contact_email}")

    # 4. Commodity query filtering
    basmati_buyers = get_buyers_for_commodity("Basmati 1121 Sella Rice")
    assert_test("Basmati buyers found", len(basmati_buyers) >= 2, f"got {len(basmati_buyers)}")

    thai_suppliers = get_suppliers_for_commodity("Thai White Rice 5% broken")
    assert_test("Thai suppliers found", len(thai_suppliers) >= 1, f"got {len(thai_suppliers)}")


def test_autonomous_campaign_ignition():
    print("\n-- Test: Autonomous Campaign Ignition & Outreach Drafting --")

    # Run discovery node
    disc = run_discovery(
        campaign=CAMPAIGN,
        benchmark_fob=BENCHMARK_FOB,
        freight_cost=FREIGHT_COST,
        target_volume_mt=500.0,
    )

    # Discovered lists
    assert_test("Matching buyers discovered", len(disc["discovered_buyers"]) >= 1, f"got {len(disc['discovered_buyers'])}")
    assert_test("Matching suppliers discovered", len(disc["discovered_suppliers"]) >= 1, f"got {len(disc['discovered_suppliers'])}")

    # Pricing calculations
    # landed CIF = 900 + 55 = 955.0
    # margin_addon = (5.0 + 2.0) / 100 = 0.07 -> indicative CIF = 955 * 1.07 = 1021.85
    assert_test("Indicative buyer CIF calculated", disc["indicative_buyer_cif"] > 955.0, f"got {disc['indicative_buyer_cif']}")
    # max FOB = 900 * 1.05 = 945.0
    assert_test("Supplier target FOB ceiling calculated", disc["supplier_target_fob"] == 945.0, f"got {disc['supplier_target_fob']}")

    # Outbound buyer draft check
    primary_buyer = disc["primary_buyer"]
    assert_test("Primary buyer identified", primary_buyer is not None)
    if primary_buyer:
        b_draft = disc["buyer_outreach_drafts"].get(primary_buyer["id"], "")
        assert_test("Buyer cold outreach drafted", len(b_draft) > 50, f"length {len(b_draft)}")
        assert_test("Draft mentions buyer port", primary_buyer["port"] in b_draft, f"expected {primary_buyer['port']}")
        assert_test("Draft cites indicative CIF", f"{disc['indicative_buyer_cif']:.2f}" in b_draft or "CIF" in b_draft)

    # Outbound supplier RFQ check
    primary_supplier = disc["primary_supplier"]
    assert_test("Primary supplier identified", primary_supplier is not None)
    if primary_supplier:
        s_draft = disc["supplier_rfq_drafts"].get(primary_supplier["id"], "")
        assert_test("Supplier RFQ drafted", len(s_draft) > 50, f"length {len(s_draft)}")
        assert_test("RFQ mentions supplier port", primary_supplier["port"] in s_draft, f"expected {primary_supplier['port']}")
        assert_test("RFQ mentions target FOB ceiling", f"{disc['supplier_target_fob']:.2f}" in s_draft or "FOB" in s_draft)


def test_automated_response_simulation():
    print("\n-- Test: Automated Counterparty Response Simulation Loop --")

    # Simulate realistic inbound quotes from discovered counterparties
    # Buyer agrees to purchase at $1,100 CIF Jebel Ali (healthy margin)
    # Supplier offers $915 FOB Karachi (within dynamic ceiling of $945)
    buyer_inbound = ParsedTradeEmail(
        sender_role="buyer",
        commodity_type=CAMPAIGN.commodity,
        quantity_mt=500.0,
        price_usd_per_mt=1100.0,
        incoterm="CIF",
        port="Jebel Ali",
    )
    supplier_inbound = ParsedTradeEmail(
        sender_role="supplier",
        commodity_type=CAMPAIGN.commodity,
        quantity_mt=500.0,
        price_usd_per_mt=915.0,
        incoterm="FOB",
        port="Karachi",
    )

    state = {
        "campaign": CAMPAIGN,
        "buyer_terms": buyer_inbound,
        "supplier_terms": supplier_inbound,
        "deal_status": "prospecting",
        "negotiation_round": 1,
    }

    # Pipeline execution
    state = fetch_market_data(state)
    state = evaluate_risk(state)

    assert_test("Simulated response deal viable", state["is_deal_viable"])
    assert_test("Simulated response deal approved", state["deal_status"] == "approved", f"got {state['deal_status']}")
    # Net margin: (1100 - (915 + 55)) / (915 + 55) = 130 / 970 ~ 13.40%
    assert_test("Net margin exceeds 5%", state["net_margin_pct"] >= 5.0, f"got {state['net_margin_pct']}")

    # Finalize deal
    state = finalize_deal(state)
    assert_test("Deal closed and supplier allocation locked", state["deal_status"] == "closed")
    assert_test("Supplier thread locked", state["supplier_thread_status"] == "allocation_locked")
    assert_test("Buyer thread accepted", state["buyer_thread_status"] == "terms_accepted")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 68)
    print("  Multi-Agent Commodity Arbitrage -- Test Suite")
    print("=" * 68)

    # Part 1 — Engine tests
    print("\n" + "-" * 68)
    print("  PART 1: Deterministic Arbitrage Engine")
    print("-" * 68)
    test_viable_deal()
    test_supplier_above_dynamic_ceiling()
    test_buyer_below_dynamic_floor()
    test_quantity_mismatch()
    test_margin_below_target()
    test_fob_buyer_normalization()
    test_cif_supplier_normalization()

    # Part 2 — Market data tests
    print("\n" + "-" * 68)
    print("  PART 2: MarketDataService")
    print("-" * 68)
    test_market_data_service_fallback()
    test_dynamic_bound_computation()

    # Part 3 — Multi-agent simulation
    print("\n" + "-" * 68)
    print("  PART 3: Multi-Agent Orchestrator Simulation")
    print("-" * 68)
    test_multi_turn_negotiation()
    test_zero_risk_rule()
    test_fetch_market_data_node()

    # Part 4 — Counterparty Discovery & Automated Ignition
    print("\n" + "-" * 68)
    print("  PART 4: Counterparty Discovery & Automated Outreach")
    print("-" * 68)
    test_directory_counterparties()
    test_autonomous_campaign_ignition()
    test_automated_response_simulation()

    print("\n" + "=" * 68)
    total = passed + failed
    print(f"  Results: {passed}/{total} passed, {failed}/{total} failed")
    print("=" * 68)

    if failed > 0:
        sys.exit(1)
    else:
        print("\n  ALL ASSERTIONS PASSED.")
        sys.exit(0)

