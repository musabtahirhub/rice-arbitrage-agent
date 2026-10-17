# 🌾 Autonomous Commodity Arbitrage Desk (Rice Trading Agent)

A clean, educational, mid-level multi-agent commodity arbitrage system demonstrating physical grain trading between **Asian mills** (Pakistan, Thailand, Vietnam) and **Middle Eastern institutional buyers** (UAE, Saudi Arabia).

---

## 🎯 Architectural Principles

1. **Separation of Brains**:
   - **Large Language Model (Gemini)**: Scoped strictly to unstructured text parsing (extracting quantities, prices, incoterms from raw emails) and polite business correspondence drafting.
   - **Deterministic Math Engine (Pure Python)**: Calculates landed costs, ocean freight, dynamic ceilings/floors, and net profit margins with zero risk of LLM hallucination.

2. **The Zero-Risk Invariant**:
   - The intermediary desk **never commits to a buyer** or accepts a purchase order until matching supplier volume allocation is confirmed and locked. This eliminates short squeeze risk in volatile physical commodity markets.

3. **Dynamic Market Grounding**:
   - Hardcoded price boundaries are replaced with live benchmark indices (Basmati 1121, Thai White 5%, Jasmine, Vietnam 5%) and container freight matrices.
   - **FOB Ceiling** = `Benchmark FOB * (1 + Max Variance %)`
   - **Landed Cost** = `Supplier FOB + Ocean Freight + Operating Buffer`
   - **CIF Floor** = `Landed Cost at Ceiling * (1 + Target Margin %)`
   - **Net Margin %** = `((Buyer CIF - Landed Cost) / Landed Cost) * 100`

4. **Transparent LangGraph State Machine**:
   - A deterministic `StateGraph` (`app/graph/graph.py`) coordinates the entire negotiation pipeline:
     - `parse_incoming_email` ➔ `fetch_market_data` ➔ `evaluate_risk` ➔ Conditional Router:
       - If viable: `confirm_deal` (locks supplier first, then confirms buyer)
       - If buyer price low: `counter_buyer` (counters firmly at CIF floor)
       - If supplier price high: `counter_supplier` (counters firmly at FOB ceiling)

---

## 📂 Project Structure

```text
rice-arbitrage-agent/
├── app/
│   ├── main.py              # FastAPI app + lifespan (DB, checkpointer, push-or-poll email ingestion)
│   ├── config.py, logger.py, telemetry.py
│   ├── models.py            # Campaign, ParsedEmail, DealState, API request bodies
│   ├── schemas.py           # Node updates, HITL payloads, pricing results, API responses
│   ├── directory.py         # In-memory buyer/supplier directory
│   ├── prompts.py           # Gemini prompt templates
│   ├── db/                  # database.py (engine, sessions, pool), db_models.py (ORM + audit helpers)
│   ├── graph/               # LangGraph: graph.py (StateGraph), workflow.py (live graph + checkpointer)
│   │   └── agents/          # Graph nodes: buyer_agent, supplier_agent, risk_worker, finalization
│   ├── pricing/             # formulas.py, strategy.py, evaluation.py (deal math), market.py (benchmarks, freight)
│   ├── services/            # llm_service.py, campaign_runner.py
│   │   └── mail/            # email_utils, smtp, imap, gmail_client, gmail_worker (push + polling),
│   │                        #   inbound_state, inbound_turn, inbound_dedupe, email_matcher
│   └── routes/              # campaigns, negotiation (incl. approval), webhooks
├── migrations/              # Alembic migrations
├── scripts/gmail_auth.py    # One-time headless Gmail OAuth setup
├── static/                  # Trading desk UI
├── tests/
└── requirements.txt
```

---

## 🚀 Quickstart

### 1. Installation
```bash
# Create virtual environment
python -m venv venv
venv\Scripts\activate   # Windows
source venv/bin/activate # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Setup
Copy `.env.example` to `.env`:
```bash
copy .env.example .env
```
*(Optional: Set `GEMINI_API_KEY` for live LLM extraction. If left blank, the system automatically uses reliable regex pattern matching.)*

### 3. Run Automated Tests
```bash
pytest --ignore=tests/test_live_email.py
# or, as VS Code runs them:
python -m unittest discover -s tests -p "test_*.py" -v
```
`tests/test_campaign_flow.py` and `tests/test_webhook.py` can also run as scripts (`python tests/test_webhook.py`).
`tests/test_live_email.py` sends real email; run it only on purpose.

### 4. Launch the Web Application
```bash
uvicorn app.main:app --port 8000 --reload
```
Open [http://localhost:8000](http://localhost:8000) to view the interactive dashboard.
