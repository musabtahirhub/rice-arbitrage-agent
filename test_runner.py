import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from langgraph.checkpoint.memory import MemorySaver

import app.database
import app.main
import app.workflow
from app.db_models import Base, CampaignModel, TradeAuditModel

# Setup isolated test database and test checkpointer
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
Base.metadata.create_all(bind=test_engine)

app.database.engine = test_engine
app.database.SessionLocal = TestSessionLocal
app.main.SessionLocal = TestSessionLocal


def _override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.main.app.dependency_overrides[app.database.get_db] = _override_get_db

test_checkpointer = MemorySaver()
test_checkpointer.setup = lambda: None
app.workflow.checkpointer = test_checkpointer
app.workflow.trade_graph = app.workflow.build_trade_graph(checkpointer=test_checkpointer)
app.main.trade_graph = app.workflow.trade_graph
trade_graph = app.workflow.trade_graph

from app.models import Campaign, DealState, ParsedEmail
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import (
    calculate_dynamic_bounds,
    calculate_landed_cost,
    calculate_net_margin,
    evaluate_deal,
    evaluate_deal_strategy,
    normalize_terms,
)

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
    buyers = get_buyers_for_commodity("Basmati 1121")
    assert_true(len(buyers) >= 2, "Found matching Basmati buyers")
    assert_true(any("Gulf Food" in b.name for b in buyers), "Gulf Food Trading found in directory")

    suppliers = get_suppliers_for_commodity("Basmati 1121")
    assert_true(len(suppliers) >= 1, "Found matching Basmati suppliers")
    assert_true(any("Indus Rice" in s.name for s in suppliers), "Indus Rice Mills found in directory")

    from app.market import fetch_live_market_data, parse_thai_rice_html, CACHE_FILE
    rates = fetch_live_market_data()
    assert_true(len(rates) >= 5, "Market rates dictionary loaded")
    assert_true(CACHE_FILE.exists(), "market_cache.json created on disk")

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

    from app.market import parse_yahoo_finance_json
    sample_yahoo = {
        "chart": {
            "result": [{
                "meta": {
                    "regularMarketPrice": 16.0,
                    "currency": "USD",
                    "symbol": "ZR=F"
                }
            }]
        }
    }
    cwt_mt, scaled_dict = parse_yahoo_finance_json(sample_yahoo)
    assert_true(cwt_mt == round(16.0 * 22.046, 2), f"Yahoo parser converted 16.0 cwt to ${cwt_mt}/MT")
    assert_true(scaled_dict.get("basmati 1121") == 900.0, "Scaled Basmati matches baseline at 16.0 cwt")

    basmati_rate = get_benchmark_rate("Basmati 1121", 5.0)
    assert_true(basmati_rate > 0.0, f"Basmati benchmark is valid (got ${basmati_rate}/MT)")
    assert_true(abs(basmati_rate - 900.0) < 50.0, f"Basmati benchmark dynamically scaled near baseline (got ${basmati_rate}/MT)")

    thai_rate = get_benchmark_rate("Thai White 5%", 5.0)
    assert_true(thai_rate > 0.0, f"Thai White benchmark is valid (got ${thai_rate}/MT)")

    freight_base = estimate_freight("Karachi", "Jebel Ali")
    assert_true(freight_base == 45.0, f"Karachi -> Jebel Ali base freight is $45/MT (got {freight_base})")

    freight_baf = estimate_freight("Karachi", "Jebel Ali", fuel_surcharge_pct=4.0)
    assert_true(freight_baf == 46.8, f"Karachi -> Jebel Ali freight with +4% fuel surcharge is $46.80/MT (got {freight_baf})")


def test_deterministic_math_engine():
    print("\n--- 2. Testing Deterministic Math Engine ---")
    landed = calculate_landed_cost(supplier_fob=900.0, freight=45.0, buffer_usd=20.0)
    assert_true(landed == 965.0, f"Landed cost is $965.00/MT (got {landed})")

    margin = calculate_net_margin(buyer_cif=1150.0, landed_cost=965.0)
    assert_true(margin == 19.17, f"Net margin is 19.17% (got {margin})")

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

    cfg = {"configurable": {"thread_id": campaign.campaign_id}}
    print("  [Step 1] Ingesting low buyer inquiry...")
    state["latest_email"] = "We offer USD 950.00 per MT CIF Jebel Ali for 500 MT Basmati 1121."
    state["active_role"] = "buyer"
    state = trade_graph.invoke(state, config=cfg)

    assert_true(state["buyer_terms"] is not None, "Buyer terms extracted")
    assert_true(state["negotiation_round"] == 1, "Negotiation round = 1")
    assert_true(not state["is_deal_viable"], "Deal not viable yet (supplier allocation missing & price low)")
    assert_true(state["deal_status"] == "counter_sent", "Deal status is counter_sent")
    assert_true(len(state["buyer_draft"]) > 0, "Counter-offer drafted to buyer")

    print("  [Step 2] Ingesting competitive supplier quote...")
    state["latest_email"] = "We quote 500 MT Basmati 1121 at USD 900.00/MT FOB Karachi. LC payment."
    state["active_role"] = "supplier"
    state = trade_graph.invoke(state, config=cfg)

    assert_true(state["supplier_terms"] is not None, "Supplier terms extracted")
    assert_true(state["negotiation_round"] == 2, "Negotiation round = 2")
    assert_true(not state["is_deal_viable"], "Deal still not viable because buyer price is still $950")

    print("  [Step 3] Buyer increases bid to USD 1150.00 CIF Jebel Ali...")
    state["latest_email"] = "We agree to increase our bid to USD 1150.00/MT CIF Jebel Ali for 500 MT."
    state["active_role"] = "buyer"
    state = trade_graph.invoke(state, config=cfg)

    assert_true(state["negotiation_round"] == 3, "Negotiation round = 3")
    assert_true(state["is_deal_viable"], "Deal is now viable!")
    assert_true(state["deal_status"] == "closed", "Deal closed formally")
    supp_draft_lower = state["supplier_draft"].lower()
    buyer_draft_lower = state["buyer_draft"].lower()
    assert_true(
        "lock" in supp_draft_lower or "confirm" in supp_draft_lower or "accept" in supp_draft_lower or "proforma" in supp_draft_lower,
        "Supplier allocation locked first",
    )
    assert_true(
        "sco" in buyer_draft_lower or "accept" in buyer_draft_lower or "confirm" in buyer_draft_lower or "order" in buyer_draft_lower,
        "Buyer confirmed second",
    )


def test_fastapi_endpoints():
    print("\n--- 5. Testing FastAPI REST Endpoints & UI Serving ---")
    from starlette.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    res = client.get("/")
    assert_true(res.status_code == 200, f"GET / returns 200 OK (got {res.status_code})")

    res = client.get("/api/directory")
    assert_true(res.status_code == 200, "GET /api/directory returns 200 OK")
    dir_data = res.json()
    assert_true(len(dir_data.get("buyers", [])) >= 3, "Directory lists at least 3 buyers")
    assert_true(len(dir_data.get("suppliers", [])) >= 3, "Directory lists at least 3 suppliers")

    camp_payload = {
        "commodity": "Basmati 1121",
        "target_volume_mt": 500.0,
        "target_margin_pct": 10.0,
        "max_variance_from_benchmark_pct": 5.0,
        "destination_port": "Jebel Ali",
        "auto_run": False,
    }
    res = client.post("/api/campaigns", json=camp_payload)
    assert_true(res.status_code == 200, f"POST /api/campaigns returns 200 OK: {res.text}")
    camp_data = res.json()
    cid = camp_data["campaign_id"]
    assert_true(len(camp_data["discovered_buyers"]) >= 1, "Matching buyers discovered")
    assert_true(len(camp_data["discovered_suppliers"]) >= 1, "Matching suppliers discovered")

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

    turn2_payload = {
        "campaign_id": cid,
        "sender_role": "supplier",
        "raw_email": "We quote USD 900.00/MT FOB Karachi for 500 MT Basmati 1121.",
    }
    res = client.post("/api/negotiate", json=turn2_payload)
    assert_true(res.status_code == 200, "POST /api/negotiate Turn 2 returns 200")

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

    res = client.get(f"/api/campaigns/{cid}")
    assert_true(res.status_code == 200, "GET /api/campaigns/{id} returns 200")
    status_data = res.json()
    assert_true(status_data["deal_status"] == "closed", "Ledger persists closed status")
    assert_true(status_data["negotiation_round"] == 3, "Ledger tracks round 3")


def test_proactive_origination_and_tactical_maximization():
    print("\n--- 6. Testing Proactive Origination & Tactical Margin Maximization ---")
    from starlette.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    camp_payload = {
        "commodity": "Basmati 1121",
        "target_volume_mt": 500.0,
        "target_margin_pct": 10.0,
        "max_variance_from_benchmark_pct": 5.0,
        "destination_port": "Jebel Ali",
        "min_profit_per_mt_hard": 50.0,
        "min_profit_per_mt_soft": 120.0,
        "max_negotiation_rounds": 3,
        "auto_run": False,
    }
    res = client.post("/api/campaigns", json=camp_payload)
    assert_true(res.status_code == 200, "POST /api/campaigns returns 200")
    cdata = res.json()
    cid = cdata["campaign_id"]
    assert_true(len(cdata.get("buyer_draft", "")) > 0, "Outbound buyer draft created automatically")
    assert_true("Soft Corporate Offer" in cdata["buyer_draft"], "Draft is a Cold Soft Corporate Offer (SCO)")
    assert_true("CIF Jebel Ali" in cdata["buyer_draft"], "Draft specifies CIF destination port")
    assert_true(cdata.get("pipeline_step") == 1, "Pipeline starts at Step 1 (Buyer Discovered & Pitched)")
    assert_true(cdata.get("deal_status") == "prospecting", "POST /api/campaigns sets deal_status to prospecting")
    expected_anchor = round(cdata["benchmark_fob_usd"] + 45.0 + 20.0 + 120.0 + 30.0, 2)
    assert_true(cdata.get("anchor_cif_usd") == expected_anchor, f"Initial high-anchor CIF matches formula: ${expected_anchor} (got {cdata.get('anchor_cif_usd')})")

    campaign = Campaign(
        campaign_id="CAMP-HARD-TEST",
        commodity="Basmati 1121",
        target_volume_mt=500.0,
        target_margin_pct=10.0,
        buffer_usd_per_mt=20.0,
        min_profit_per_mt_hard=50.0,
        min_profit_per_mt_soft=120.0,
        max_negotiation_rounds=3,
        destination_port="Jebel Ali",
        origin_port_default="Karachi",
    )
    res_hard = evaluate_deal_strategy(
        buyer_cif=995.0,
        supplier_fob=900.0,
        freight=45.0,
        buffer_usd=20.0,
        round_num=1,
        campaign=campaign,
    )
    assert_true(res_hard["action"] == "REJECT_HARD", f"Deal yielding $30/MT is rejected under hard floor: {res_hard['action']}")
    assert_true(not res_hard["viable"], "Deal yielding $30/MT is marked non-viable")
    assert_true(res_hard["net_spread"] == 30.0, f"Net spread is correctly calculated as $30.00/MT (got {res_hard['net_spread']})")

    state_hard: DealState = {
        "campaign": campaign,
        "negotiation_round": 0,
        "deal_status": "prospecting",
        "is_deal_viable": False,
        "net_margin_pct": 0.0,
        "buyer_terms": None,
        "supplier_terms": ParsedEmail(
            sender_role="supplier",
            commodity="Basmati 1121",
            quantity_mt=500.0,
            price_usd_per_mt=900.0,
            incoterm="FOB",
            port="Karachi",
        ),
        "latest_email": "We bid USD 995.00/MT CIF Jebel Ali for 500 MT Basmati 1121.",
        "active_role": "buyer",
    }
    cfg_hard = {"configurable": {"thread_id": campaign.campaign_id}}
    state_hard = trade_graph.invoke(state_hard, config=cfg_hard)
    assert_true(state_hard["action"] == "REJECT_HARD", "LangGraph sets action to REJECT_HARD")
    assert_true(state_hard["deal_status"] == "rejected", "LangGraph routes to reject_deal node")
    assert_true(not state_hard["is_deal_viable"], "Deal marked non-viable on hard rejection")

    res_r1 = evaluate_deal_strategy(
        buyer_cif=1045.0,
        supplier_fob=900.0,
        freight=45.0,
        buffer_usd=20.0,
        round_num=1,
        campaign=campaign,
    )
    assert_true(res_r1["action"] == "COUNTER_TO_MAXIMIZE", f"Round 1: $80/MT is countered to maximize: {res_r1['action']}")
    assert_true(res_r1["viable"], "Round 1: Deal is viable (profitable concession zone)")
    assert_true(res_r1["net_spread"] == 80.0, f"Net spread is $80.00/MT (got {res_r1['net_spread']})")

    res_r2 = evaluate_deal_strategy(
        buyer_cif=1045.0,
        supplier_fob=900.0,
        freight=45.0,
        buffer_usd=20.0,
        round_num=2,
        campaign=campaign,
    )
    assert_true(res_r2["action"] == "COUNTER_TO_MAXIMIZE", f"Round 2: $80/MT is countered in round 2: {res_r2['action']}")

    res_r3 = evaluate_deal_strategy(
        buyer_cif=1045.0,
        supplier_fob=900.0,
        freight=45.0,
        buffer_usd=20.0,
        round_num=3,
        campaign=campaign,
    )
    assert_true(res_r3["action"] == "ACCEPT_AND_CLOSE", f"Round 3: $80/MT accepted once rounds expire: {res_r3['action']}")
    assert_true(res_r3["viable"], "Round 3: Deal accepted and viable")

    t1_res = client.post("/api/negotiate", json={
        "campaign_id": cid,
        "sender_role": "buyer",
        "raw_email": "We are interested and quote our target at USD 1045.00/MT CIF Jebel Ali for 500 MT.",
    })
    assert_true(t1_res.status_code == 200, "Turn 1 API call succeeds")
    t1_data = t1_res.json()
    assert_true(len(t1_data.get("supplier_draft", "")) > 0, "Supplier RFQ auto-drafted upon buyer signal")
    supp_rfq_lower = t1_data.get("supplier_draft", "").lower()
    assert_true(
        "rfq" in supp_rfq_lower or "quotation" in supp_rfq_lower or "request" in supp_rfq_lower,
        "Supplier draft is an RFQ",
    )
    assert_true(t1_data.get("target_fob_ceiling") == 860.0, f"Target FOB ceiling is $860.00 (got {t1_data.get('target_fob_ceiling')})")

    t2_res = client.post("/api/negotiate", json={
        "campaign_id": cid,
        "sender_role": "supplier",
        "raw_email": "We quote 500 MT Basmati 1121 at USD 900.00/MT FOB Karachi. LC payment.",
    })
    assert_true(t2_res.status_code == 200, "Turn 2 API call succeeds")
    t2_data = t2_res.json()
    assert_true(t2_data.get("supplier_terms") is not None, "Supplier terms locked in state")
    assert_true(t2_data.get("action") == "COUNTER_TO_MAXIMIZE", f"Turn 2 action is COUNTER_TO_MAXIMIZE (got {t2_data.get('action')})")
    assert_true(t2_data.get("net_spread_usd") == 80.0, f"Turn 2 net spread is $80.00/MT (got {t2_data.get('net_spread_usd')})")

    t3_res = client.post("/api/negotiate", json={
        "campaign_id": cid,
        "sender_role": "buyer",
        "raw_email": "We agree to increase our CIF price to USD 1085.00/MT CIF Jebel Ali for 500 MT.",
    })
    assert_true(t3_res.status_code == 200, "Turn 3 API call succeeds")
    t3_data = t3_res.json()
    assert_true(t3_data.get("action") == "ACCEPT_AND_CLOSE", f"Turn 3 action is ACCEPT_AND_CLOSE (got {t3_data.get('action')})")
    assert_true(t3_data.get("deal_status") == "closed", "Turn 3 deal status is closed")
    assert_true(t3_data.get("net_spread_usd") == 120.0, f"Turn 3 net spread reaches soft target $120.00/MT (got {t3_data.get('net_spread_usd')})")


def test_autonomous_end_to_end_campaign():
    print("\n--- 7. Testing Autonomous End-to-End Campaign Execution (auto_run=True) ---")
    from starlette.testclient import TestClient
    from app.main import app
    from app.workflow import run_full_autonomous_campaign

    client = TestClient(app)

    payload_auto = {
        "commodity": "Basmati 1121",
        "target_volume_mt": 500.0,
        "target_margin_pct": 10.0,
        "max_variance_from_benchmark_pct": 5.0,
        "destination_port": "Jebel Ali",
        "min_profit_per_mt_hard": 50.0,
        "min_profit_per_mt_soft": 120.0,
        "max_negotiation_rounds": 3,
        "auto_run": True,
    }
    res = client.post("/api/campaigns", json=payload_auto)
    assert_true(res.status_code == 200, f"POST /api/campaigns with auto_run=True returns 200: {res.text}")
    data = res.json()
    cid = data["campaign_id"]

    assert_true(data.get("deal_status") == "closed", f"Autonomous campaign deal_status is closed (got {data.get('deal_status')})")
    assert_true(data.get("action") == "ACCEPT_AND_CLOSE", f"Action is ACCEPT_AND_CLOSE (got {data.get('action')})")
    assert_true(data.get("pipeline_step") == 4, f"Pipeline reached Step 4 (got {data.get('pipeline_step')})")
    assert_true(data.get("negotiation_rounds_completed") >= 1, f"Negotiation rounds completed >= 1 (got {data.get('negotiation_rounds_completed')})")
    assert_true(data.get("final_net_spread_usd") >= 50.0, f"Final net spread >= hard floor $50/MT (got ${data.get('final_net_spread_usd')}/MT)")
    assert_true(data.get("final_net_margin_pct") > 0.0, f"Final net margin > 0% (got {data.get('final_net_margin_pct')}%)")

    transcript = data.get("audit_transcript", [])
    assert_true(len(transcript) >= 4, f"Audit transcript contains full correspondence (got {len(transcript)} messages)")

    actions = [m.get("action") for m in transcript]
    assert_true("OUTBOUND_SCO" in actions, "Transcript records initial Cold SCO")
    assert_true("INBOUND_BID" in actions, "Transcript records buyer initial bid")
    assert_true("OUTBOUND_RFQ" in actions, "Transcript records supplier RFQ")
    assert_true("INBOUND_QUOTE" in actions, "Transcript records supplier quotation")
    assert_true("LOCK_SUPPLIER_ALLOCATION" in actions, "Transcript records supplier allocation lock")
    assert_true("ACCEPT_AND_CLOSE" in actions, "Transcript records final buyer acceptance")

    res_get = client.get(f"/api/campaigns/{cid}")
    assert_true(res_get.status_code == 200, "GET /api/campaigns/{id} returns 200 for autonomous deal")
    ledger_data = res_get.json()
    assert_true(ledger_data["deal_status"] == "closed", "Ledger confirms deal_status closed")
    assert_true(ledger_data["final_net_spread_usd"] >= 50.0, f"Ledger confirms net spread ${ledger_data['final_net_spread_usd']}/MT")
    assert_true(len(ledger_data.get("audit_transcript", [])) == len(transcript), "Ledger contains full audit transcript")

    payload_thai = {
        "commodity": "Thai White 5%",
        "target_volume_mt": 1000.0,
        "destination_port": "Jebel Ali",
        "min_profit_per_mt_hard": 40.0,
        "min_profit_per_mt_soft": 100.0,
        "max_negotiation_rounds": 3,
    }
    res_thai = client.post("/api/campaigns", json=payload_thai)
    assert_true(res_thai.status_code == 200, "POST /api/campaigns with default auto_run=True for Thai White returns 200")
    thai_data = res_thai.json()
    assert_true(thai_data.get("deal_status") == "closed", "Thai White autonomous campaign closed")
    assert_true(thai_data.get("final_net_spread_usd") >= 40.0, f"Thai White final spread >= $40/MT (got ${thai_data.get('final_net_spread_usd')}/MT)")
    assert_true(len(thai_data.get("audit_transcript", [])) >= 4, "Thai White transcript recorded")


def test_dynamic_llm_email_generation_and_transport():
    print("\n--- 8. Testing Dynamic LLM Email Generation & Transport Bridge ---")
    from app.email_service import check_latest_reply, parse_email_draft, send_email
    from app.workflow import (
        confirm_deal_node,
        counter_buyer_node,
        counter_supplier_node,
        generate_proactive_sco_draft,
        reject_deal_node,
        split_subject_and_body,
    )

    sample_strict = """SUBJECT: Commercial Firm Offer — Basmati 1121 CIF Jebel Ali
BODY:
Dear Procurement Partner,
We are pleased to present this parcel specification for 500 MT."""
    sub1, body1 = split_subject_and_body(sample_strict)
    assert_true(sub1 == "Commercial Firm Offer — Basmati 1121 CIF Jebel Ali", f"Strict subject extracted: {sub1}")
    assert_true("Dear Procurement Partner" in body1, "Strict body extracted")

    sample_rfc = """Subject: Urgent RFQ — Basmati 1121 FOB Karachi

Dear Mill Manager,
Please provide earliest shipping readiness for 500 MT."""
    sub2, body2 = parse_email_draft(sample_rfc)
    assert_true(sub2 == "Urgent RFQ — Basmati 1121 FOB Karachi", f"RFC subject extracted: {sub2}")
    assert_true("Dear Mill Manager" in body2, "RFC body extracted")

    sample_plain = "Hello, this is just a body text without any headers."
    sub3, body3 = parse_email_draft(sample_plain, default_subject="Default Subject")
    assert_true(sub3 == "Default Subject", "Fallback to default subject for plain text")
    assert_true(body3 == sample_plain, "Plain body preserved")

    campaign = Campaign(
        campaign_id="TEST-SCO",
        commodity="Basmati 1121",
        target_volume_mt=500.0,
        target_margin_pct=10.0,
        destination_port="Jebel Ali",
        origin_port_default="Karachi",
    )
    sco_draft = generate_proactive_sco_draft(campaign, 1114.16, "Gulf Food Trading", "gulf@trade.com")
    assert_true(len(sco_draft) > 50, "Proactive SCO draft generated")
    sco_sub, sco_body = parse_email_draft(sco_draft)
    assert_true(len(sco_sub) > 5, f"Cold SCO has contextual subject: '{sco_sub}'")
    assert_true("Basmati 1121" in sco_draft, "Cold SCO mentions commodity")
    assert_true("Jebel Ali" in sco_draft, "Cold SCO mentions destination port")

    buyer_t = ParsedEmail(
        sender_role="buyer",
        commodity="Basmati 1121",
        quantity_mt=500.0,
        price_usd_per_mt=1045.0,
        incoterm="CIF",
        port="Jebel Ali",
    )
    supplier_t = ParsedEmail(
        sender_role="supplier",
        commodity="Basmati 1121",
        quantity_mt=500.0,
        price_usd_per_mt=900.0,
        incoterm="FOB",
        port="Karachi",
    )

    state: DealState = {
        "campaign": campaign,
        "benchmark_fob_usd": 900.0,
        "freight_cost_usd": 45.0,
        "dynamic_fob_ceiling": 945.0,
        "dynamic_cif_floor": 1050.0,
        "target_fob_ceiling": 860.0,
        "anchor_cif_usd": 1114.16,
        "buyer_terms": buyer_t,
        "supplier_terms": supplier_t,
        "negotiation_round": 1,
        "deal_status": "prospecting",
        "action": "COUNTER_TO_MAXIMIZE",
        "pipeline_step": 2,
        "is_deal_viable": True,
        "net_spread_usd": 80.0,
        "net_margin_pct": 8.29,
        "evaluation_reason": "Spread $80.00/MT clears floor but below soft target $120.00/MT.",
        "latest_email": "",
        "active_role": "buyer",
        "buyer_draft": "",
        "supplier_draft": "",
        "audit_transcript": [],
    }

    cb_res = counter_buyer_node(state)
    assert_true(cb_res.get("deal_status") == "counter_sent", "counter_buyer_node sets counter_sent status")
    assert_true(len(cb_res.get("buyer_draft", "")) > 50, "counter_buyer_node produces rich draft")
    cb_sub, cb_body = parse_email_draft(cb_res["buyer_draft"])
    assert_true(len(cb_sub) > 5, f"counter_buyer_node produced dynamic subject: '{cb_sub}'")

    cs_res = counter_supplier_node(state)
    assert_true(cs_res.get("deal_status") == "counter_sent", "counter_supplier_node sets counter_sent status")
    assert_true(len(cs_res.get("supplier_draft", "")) > 50, "counter_supplier_node produces rich draft")
    cs_sub, cs_body = parse_email_draft(cs_res["supplier_draft"])
    assert_true(len(cs_sub) > 5, f"counter_supplier_node produced dynamic subject: '{cs_sub}'")

    cd_res = confirm_deal_node(state)
    assert_true(cd_res.get("deal_status") == "closed", "confirm_deal_node closes deal")
    assert_true(len(cd_res.get("supplier_draft", "")) > 50, "confirm_deal_node generates supplier volume lock")
    assert_true(len(cd_res.get("buyer_draft", "")) > 50, "confirm_deal_node generates buyer SCO acceptance")
    cd_supp_sub, _ = parse_email_draft(cd_res["supplier_draft"])
    cd_buyer_sub, _ = parse_email_draft(cd_res["buyer_draft"])
    assert_true(len(cd_supp_sub) > 5, f"Supplier confirmation subject: '{cd_supp_sub}'")
    assert_true(len(cd_buyer_sub) > 5, f"Buyer confirmation subject: '{cd_buyer_sub}'")

    rd_res = reject_deal_node(state)
    assert_true(rd_res.get("deal_status") == "rejected", "reject_deal_node sets rejected status")
    assert_true(rd_res.get("action") == "REJECT_HARD", "reject_deal_node sets action REJECT_HARD")
    assert_true(len(rd_res.get("buyer_draft", "")) > 50, "reject_deal_node generates decline correspondence")
    rd_sub, _ = parse_email_draft(rd_res["buyer_draft"])
    assert_true(len(rd_sub) > 5, f"Decline notice subject: '{rd_sub}'")

    from app.email_service import is_automated_or_bounce_message, is_simulated_email, normalize_thread_subject

    assert_true(is_simulated_email("export@indusrice.pk"), "indusrice.pk recognized as simulated domain")
    assert_true(is_simulated_email("sales@thaigrain.co.th"), "thaigrain.co.th recognized as simulated domain")
    assert_true(is_simulated_email("trade@mekongdelta-agro.vn"), "mekongdelta-agro.vn recognized as simulated domain")
    assert_true(is_simulated_email("procurement@gulffood.ae"), "gulffood.ae recognized as simulated domain")

    assert_true(is_automated_or_bounce_message("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)"), "mailer-daemon bounce detected")
    assert_true(is_automated_or_bounce_message("postmaster@domain.com", "Undeliverable mail"), "postmaster bounce detected")
    assert_true(is_automated_or_bounce_message("Instagram <notification@priority.instagram.com>", "rendersbymusab, catch up"), "automated newsletter detected")
    assert_true(not is_automated_or_bounce_message("buyer@realcompany.com", "Re: Soft Corporate Offer (SCO) — Basmati 1121"), "real buyer message not classified as bounce")

    norm1 = normalize_thread_subject("Counter-Offer — Basmati 1121 CIF Jebel Ali", "Soft Corporate Offer (SCO) — Basmati 1121 CIF Jebel Ali")
    assert_true(norm1 == "Re: Soft Corporate Offer (SCO) — Basmati 1121 CIF Jebel Ali", f"Normalized to Re: <thread_subject>: {norm1}")
    norm2 = normalize_thread_subject("Re: Soft Corporate Offer (SCO) — Basmati 1121 CIF Jebel Ali", "Re: Soft Corporate Offer (SCO) — Basmati 1121 CIF Jebel Ali")
    assert_true(norm2 == "Re: Soft Corporate Offer (SCO) — Basmati 1121 CIF Jebel Ali", f"Prevented double Re: prefix: {norm2}")

    sent_res = send_email(
        "test.partner@domain.com",
        sco_draft,
        in_reply_to="<parent-123@domain.com>",
        references="<root-001@domain.com>",
        thread_subject="Soft Corporate Offer (SCO) — Basmati 1121 CIF Jebel Ali",
    )
    assert_true(sent_res is True, "send_email executes with threading headers without exception in mock/live mode")

    from app.email_service import EmailReply
    from app.main import _match_email_to_campaign

    reply_mock = EmailReply(
        body="We counter at USD 1020/MT CIF Jebel Ali for 500 MT Basmati 1121.",
        subject="Re: Soft Corporate Offer (SCO) — Basmati 1121 CIF Jebel Ali",
        sender="buyer@gulffood.ae",
        message_id="<buyer-reply-001@gulffood.ae>",
        in_reply_to="<root-001@desk.com>",
        references="<root-001@desk.com>",
    )
    assert_true(isinstance(reply_mock, str), "EmailReply inherits from str for full backward compatibility")
    assert_true(reply_mock.subject.startswith("Re:"), "EmailReply provides subject metadata")
    assert_true(reply_mock.sender == "buyer@gulffood.ae", "EmailReply provides sender metadata")
    assert_true(reply_mock.message_id == "<buyer-reply-001@gulffood.ae>", "EmailReply provides message_id metadata")
    assert_true(reply_mock.in_reply_to == "<root-001@desk.com>", "EmailReply provides in_reply_to metadata")

    test_cid = "CAMP-MATCH-001"
    with TestSessionLocal() as db:
        existing = db.query(CampaignModel).filter(CampaignModel.id == test_cid).first()
        if not existing:
            db_c = CampaignModel(
                id=test_cid,
                commodity=campaign.commodity,
                target_volume_mt=campaign.target_volume_mt,
                destination_port=campaign.destination_port,
                origin_port_default=campaign.origin_port_default,
                deal_status="prospecting",
            )
            db.add(db_c)
            db.add(TradeAuditModel(
                campaign_id=test_cid,
                thread_id=test_cid,
                role="agent",
                counterparty_price=None,
                net_spread=None,
                raw_message="SCO to buyer@gulffood.ae",
                direction="OUTBOUND",
            ))
            db.commit()

    matched = _match_email_to_campaign(reply_mock.subject, str(reply_mock), reply_mock.sender)
    assert_true(matched == test_cid, f"_match_email_to_campaign matched incoming email to active campaign: {matched}")


def run_all_tests():
    print("====================================================================")
    print("  Commodity Arbitrage Multi-Agent System — Self-Contained Test Suite")
    print("====================================================================")

    test_market_and_directory()
    test_deterministic_math_engine()
    test_risk_evaluation_gates()
    test_langgraph_workflow()
    test_fastapi_endpoints()
    test_proactive_origination_and_tactical_maximization()
    test_autonomous_end_to_end_campaign()
    test_dynamic_llm_email_generation_and_transport()

    print("\n====================================================================")
    print(f"  Test Results: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    print("====================================================================")

    if FAIL_COUNT > 0:
        sys.exit(1)
    else:
        print("\n  ALL TESTS PASSED SUCCESSFULLY! ***")


if __name__ == "__main__":
    run_all_tests()


