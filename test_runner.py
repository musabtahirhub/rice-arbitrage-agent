"""
Self-contained unit and integration test runner for the commodity arbitrage system.
Verifies:
1. Deterministic Math Engine (bounds, margins, invariant gates)
2. Market Data & Freight Estimator
3. Counterparty Directory Lookups
4. Complete LangGraph State Machine Negotiation Loop
"""
import sys
from app.models import Campaign, DealState, ParsedEmail
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import (
    calculate_dynamic_bounds,
    calculate_landed_cost,
    calculate_net_margin,
    evaluate_deal,
    normalize_terms,
)
from app.workflow import trade_graph

PASS_COUNT = 0
FAIL_COUNT = 0


def assert_true(condition: bool, msg: str):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  [PASS] {msg}")
    else:
        FAIL_COUNT += 1
        print(f"  [FAIL] {msg}")


def test_market_and_directory():
    print("\n--- 1. Testing Market & Directory Services ---")
    # Directory
    buyers = get_buyers_for_commodity("Basmati 1121")
    assert_true(len(buyers) >= 2, "Found matching Basmati buyers")
    assert_true(any("Gulf Food" in b.name for b in buyers), "Gulf Food Trading found in directory")

    suppliers = get_suppliers_for_commodity("Basmati 1121")
    assert_true(len(suppliers) >= 1, "Found matching Basmati suppliers")
    assert_true(any("Indus Rice" in s.name for s in suppliers), "Indus Rice Mills found in directory")

    # Market Benchmark & Live Web Scraper
    from app.market import fetch_live_market_data, parse_thai_rice_html, CACHE_FILE
    rates = fetch_live_market_data()
    assert_true(len(rates) >= 5, "Market rates dictionary loaded")
    assert_true(CACHE_FILE.exists(), "market_cache.json created on disk")

    # Verify HTML parser on Thai Rice Exporters format
    sample_html = """
    <table>
    <tr><td>Item</td><td>9 Sep 2026</td></tr>
    <tr><td><img src="img/price_eng-1.jpg"></td><td>1264</td></tr>
    <tr><td>1170</td></tr>
    <tr><td>478</td></tr>
    <tr><td>640</td></tr>
    <tr><td>496</td></tr>
    <tr><td>490</td></tr>
    <tr><td>469</td></tr>
    <tr><td>397</td></tr>
    </table>
    """
    parsed = parse_thai_rice_html(sample_html)
    assert_true(parsed.get("thai white 5%") == 496.0, f"HTML parser extracted Thai White 5%: {parsed.get('thai white 5%')}")
    assert_true(parsed.get("pathumthani fragrant") == 478.0, "HTML parser extracted Pathumthani Fragrant")

    basmati_rate = get_benchmark_rate("Basmati 1121", 5.0)
    assert_true(basmati_rate == 900.0, f"Basmati benchmark is $900/MT (got {basmati_rate})")

    thai_rate = get_benchmark_rate("Thai White 5%", 5.0)
    assert_true(thai_rate > 0.0, f"Thai White benchmark is valid (got ${thai_rate}/MT)")

    # Dynamic Freight with fuel surcharge
    freight_base = estimate_freight("Karachi", "Jebel Ali")
    assert_true(freight_base == 45.0, f"Karachi -> Jebel Ali base freight is $45/MT (got {freight_base})")

    freight_baf = estimate_freight("Karachi", "Jebel Ali", fuel_surcharge_pct=4.0)
    assert_true(freight_baf == 46.8, f"Karachi -> Jebel Ali freight with +4% fuel surcharge is $46.80/MT (got {freight_baf})")


def test_deterministic_math_engine():
    print("\n--- 2. Testing Deterministic Math Engine ---")
    # Landed cost: FOB 900 + Freight 45 + Buffer 20 = 965
    landed = calculate_landed_cost(supplier_fob=900.0, freight=45.0, buffer_usd=20.0)
    assert_true(landed == 965.0, f"Landed cost is $965.00/MT (got {landed})")

    # Net margin: ((1150 - 965) / 965) * 100 = 19.17%
    margin = calculate_net_margin(buyer_cif=1150.0, landed_cost=965.0)
    assert_true(margin == 19.17, f"Net margin is 19.17% (got {margin})")

    # Dynamic Bounds: Benchmark 900, Variance 5%, Margin 10%, Freight 45, Buffer 20
    # FOB Ceiling = 900 * 1.05 = 945.00
    # Landed at Ceiling = 945 + 45 + 20 = 1010.00
    # CIF Floor = 1010 * 1.10 = 1111.00
    bounds = calculate_dynamic_bounds(
        benchmark_fob=900.0,
        freight=45.0,
        max_variance_pct=5.0,
        target_margin_pct=10.0,
        buffer_usd=20.0,
    )
    assert_true(bounds["dynamic_fob_ceiling"] == 945.0, f"FOB ceiling is $945.00 (got {bounds['dynamic_fob_ceiling']})")
    assert_true(bounds["dynamic_cif_floor"] == 1111.0, f"CIF floor is $1111.00 (got {bounds['dynamic_cif_floor']})")


def test_risk_evaluation_gates():
    print("\n--- 3. Testing Risk Evaluation & Invariant Gates ---")
    campaign = Campaign(
        campaign_id="TEST-001",
        commodity="Basmati 1121",
        target_volume_mt=500.0,
        target_margin_pct=10.0,
        max_variance_from_benchmark_pct=5.0,
        buffer_usd_per_mt=20.0,
        destination_port="Jebel Ali",
        origin_port_default="Karachi",
    )
    bench_fob = 900.0
    freight = 45.0

    # Gate 1: Zero-Risk Invariant — Missing supplier allocation
    buyer_terms = ParsedEmail(
        sender_role="buyer",
        commodity="Basmati 1121",
        quantity_mt=500.0,
        price_usd_per_mt=1150.0,
        incoterm="CIF",
        port="Jebel Ali",
    )
    res_no_supp = evaluate_deal(campaign, buyer_terms, None, bench_fob, freight)
    assert_true(not res_no_supp["viable"], "Zero-Risk Invariant: Deal not viable without supplier")
    assert_true("Zero-Risk Invariant" in res_no_supp["reason"], "Reason cites Zero-Risk Invariant")

    # Gate 2: Supplier price exceeds FOB ceiling ($960 > $945)
    supplier_high = ParsedEmail(
        sender_role="supplier",
        commodity="Basmati 1121",
        quantity_mt=500.0,
        price_usd_per_mt=960.0,
        incoterm="FOB",
        port="Karachi",
    )
    res_supp_high = evaluate_deal(campaign, buyer_terms, supplier_high, bench_fob, freight)
    assert_true(not res_supp_high["viable"], "Supplier above ceiling: Deal rejected")
    assert_true("exceeds dynamic ceiling" in res_supp_high["reason"], "Reason cites FOB ceiling")

    # Gate 3: Buyer price below CIF floor ($1050 < $1111)
    supplier_good = ParsedEmail(
        sender_role="supplier",
        commodity="Basmati 1121",
        quantity_mt=500.0,
        price_usd_per_mt=900.0,
        incoterm="FOB",
        port="Karachi",
    )
    buyer_low = ParsedEmail(
        sender_role="buyer",
        commodity="Basmati 1121",
        quantity_mt=500.0,
        price_usd_per_mt=1050.0,
        incoterm="CIF",
        port="Jebel Ali",
    )
    res_buyer_low = evaluate_deal(campaign, buyer_low, supplier_good, bench_fob, freight)
    assert_true(not res_buyer_low["viable"], "Buyer below floor: Deal rejected")
    assert_true("below minimum viable floor" in res_buyer_low["reason"], "Reason cites CIF floor")

    # Gate 4: Quantity Mismatch (500 MT vs 400 MT)
    supplier_mismatch = ParsedEmail(
        sender_role="supplier",
        commodity="Basmati 1121",
        quantity_mt=400.0,
        price_usd_per_mt=900.0,
        incoterm="FOB",
        port="Karachi",
    )
    res_mismatch = evaluate_deal(campaign, buyer_terms, supplier_mismatch, bench_fob, freight)
    assert_true(not res_mismatch["viable"], "Quantity mismatch: Deal rejected")
    assert_true("Quantity mismatch" in res_mismatch["reason"], "Reason cites quantity mismatch")

    # Gate 5: Fully Viable Deal (Supplier $900 FOB, Buyer $1150 CIF)
    # Landed = 900 + 45 + 20 = 965. Margin = (1150 - 965) / 965 = 19.17% >= 10.0%
    res_viable = evaluate_deal(campaign, buyer_terms, supplier_good, bench_fob, freight)
    assert_true(res_viable["viable"], "Viable deal accepted")
    assert_true(res_viable["net_margin_pct"] >= 10.0, f"Net margin exceeds target (got {res_viable['net_margin_pct']}%)")


def test_langgraph_workflow():
    print("\n--- 4. Testing LangGraph State Machine Negotiation Loop ---")
    campaign = Campaign(
        campaign_id="CAMP-TEST",
        commodity="Basmati 1121",
        target_volume_mt=500.0,
        target_margin_pct=10.0,
        destination_port="Jebel Ali",
        origin_port_default="Karachi",
    )

    state: DealState = {
        "campaign": campaign,
        "negotiation_round": 0,
        "deal_status": "prospecting",
        "is_deal_viable": False,
        "net_margin_pct": 0.0,
        "buyer_terms": None,
        "supplier_terms": None,
    }

    # Turn 1: Low Buyer Email ($950 CIF)
    print("  [Step 1] Ingesting low buyer inquiry...")
    state["latest_email"] = "We offer USD 950.00 per MT CIF Jebel Ali for 500 MT Basmati 1121."
    state["active_role"] = "buyer"
    state = trade_graph.invoke(state)

    assert_true(state["buyer_terms"] is not None, "Buyer terms extracted")
    assert_true(state["negotiation_round"] == 1, "Negotiation round = 1")
    assert_true(not state["is_deal_viable"], "Deal not viable yet (supplier allocation missing & price low)")
    assert_true(state["deal_status"] == "counter_sent", "Deal status is counter_sent")
    assert_true(len(state["buyer_draft"]) > 0, "Counter-offer drafted to buyer")

    # Turn 2: Supplier Quotes ($900 FOB)
    print("  [Step 2] Ingesting competitive supplier quote...")
    state["latest_email"] = "We quote 500 MT Basmati 1121 at USD 900.00/MT FOB Karachi. LC payment."
    state["active_role"] = "supplier"
    state = trade_graph.invoke(state)

    assert_true(state["supplier_terms"] is not None, "Supplier terms extracted")
    assert_true(state["negotiation_round"] == 2, "Negotiation round = 2")
    assert_true(not state["is_deal_viable"], "Deal still not viable because buyer price is still $950")

    # Turn 3: Buyer increases bid to $1150 CIF (Viable!)
    print("  [Step 3] Buyer increases bid to USD 1150.00 CIF Jebel Ali...")
    state["latest_email"] = "We agree to increase our bid to USD 1150.00/MT CIF Jebel Ali for 500 MT."
    state["active_role"] = "buyer"
    state = trade_graph.invoke(state)

    assert_true(state["negotiation_round"] == 3, "Negotiation round = 3")
    assert_true(state["is_deal_viable"], "Deal is now viable!")
    assert_true(state["net_margin_pct"] >= 10.0, f"Net margin exceeds target (got {state['net_margin_pct']}%)")
    assert_true(state["deal_status"] == "closed", "Deal closed formally")
    assert_true("Deal Confirmation & Volume Lock" in state["supplier_draft"], "Supplier allocation locked first")
    assert_true("Soft Corporate Offer (SCO)" in state["buyer_draft"], "Buyer confirmed second")


def test_fastapi_endpoints():
    print("\n--- 5. Testing FastAPI REST Endpoints & UI Serving ---")
    from starlette.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # 1. UI Root
    res = client.get("/")
    assert_true(res.status_code == 200, f"GET / returns 200 OK (got {res.status_code})")

    # 2. Directory Listing
    res = client.get("/api/directory")
    assert_true(res.status_code == 200, "GET /api/directory returns 200 OK")
    dir_data = res.json()
    assert_true(len(dir_data.get("buyers", [])) >= 3, "Directory lists at least 3 buyers")
    assert_true(len(dir_data.get("suppliers", [])) >= 3, "Directory lists at least 3 suppliers")

    # 3. Create Campaign
    camp_payload = {
        "commodity": "Basmati 1121",
        "target_volume_mt": 500.0,
        "target_margin_pct": 10.0,
        "max_variance_from_benchmark_pct": 5.0,
        "destination_port": "Jebel Ali",
    }
    res = client.post("/api/campaigns", json=camp_payload)
    assert_true(res.status_code == 200, f"POST /api/campaigns returns 200 OK: {res.text}")
    camp_data = res.json()
    cid = camp_data["campaign_id"]
    assert_true(len(camp_data["discovered_buyers"]) >= 1, "Matching buyers discovered")
    assert_true(len(camp_data["discovered_suppliers"]) >= 1, "Matching suppliers discovered")

    # 4. Turn 1: Low Buyer Email
    turn1_payload = {
        "campaign_id": cid,
        "sender_role": "buyer",
        "raw_email": "We bid USD 950.00/MT CIF Jebel Ali for 500 MT Basmati 1121.",
    }
    res = client.post("/api/negotiate", json=turn1_payload)
    assert_true(res.status_code == 200, "POST /api/negotiate Turn 1 returns 200")
    t1_data = res.json()
    assert_true(not t1_data["is_deal_viable"], "Turn 1: Deal not viable")
    assert_true(t1_data["deal_status"] == "counter_sent", "Turn 1: Counter-offer sent")

    # 5. Turn 2: Competitive Supplier Quote
    turn2_payload = {
        "campaign_id": cid,
        "sender_role": "supplier",
        "raw_email": "We quote USD 900.00/MT FOB Karachi for 500 MT Basmati 1121.",
    }
    res = client.post("/api/negotiate", json=turn2_payload)
    assert_true(res.status_code == 200, "POST /api/negotiate Turn 2 returns 200")

    # 6. Turn 3: Buyer Accepts Viable Price
    turn3_payload = {
        "campaign_id": cid,
        "sender_role": "buyer",
        "raw_email": "We agree to increase bid to USD 1150.00/MT CIF Jebel Ali for 500 MT.",
    }
    res = client.post("/api/negotiate", json=turn3_payload)
    assert_true(res.status_code == 200, "POST /api/negotiate Turn 3 returns 200")
    t3_data = res.json()
    assert_true(t3_data["is_deal_viable"], "Turn 3: Deal is fully viable")
    assert_true(t3_data["deal_status"] == "closed", "Turn 3: Deal status is closed")
    assert_true(t3_data["net_margin_pct"] >= 10.0, f"Turn 3: Net margin exceeds target ({t3_data['net_margin_pct']}%)")

    # 7. Campaign Status
    res = client.get(f"/api/campaigns/{cid}")
    assert_true(res.status_code == 200, "GET /api/campaigns/{id} returns 200")
    status_data = res.json()
    assert_true(status_data["deal_status"] == "closed", "Ledger persists closed status")
    assert_true(status_data["negotiation_round"] == 3, "Ledger tracks round 3")


def run_all_tests():
    print("====================================================================")
    print("  Commodity Arbitrage Multi-Agent System — Self-Contained Test Suite")
    print("====================================================================")

    test_market_and_directory()
    test_deterministic_math_engine()
    test_risk_evaluation_gates()
    test_langgraph_workflow()
    test_fastapi_endpoints()

    print("\n====================================================================")
    print(f"  Test Results: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    print("====================================================================")

    if FAIL_COUNT > 0:
        sys.exit(1)
    else:
        print("\n  ALL TESTS PASSED SUCCESSFULLY! ***")


if __name__ == "__main__":
    run_all_tests()
