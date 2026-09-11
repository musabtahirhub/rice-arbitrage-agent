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
   - A single, self-contained `StateGraph` in `app/workflow.py` coordinates the entire negotiation pipeline:
     - `parse_incoming_email` ➔ `fetch_market_data` ➔ `evaluate_risk` ➔ Conditional Router:
       - If viable: `confirm_deal` (locks supplier first, then confirms buyer)
       - If buyer price low: `counter_buyer` (counters firmly at CIF floor)
       - If supplier price high: `counter_supplier` (counters firmly at FOB ceiling)

---

## 📂 Project Structure

```text
rice-arbitrage-agent/
├── app/
│   ├── __init__.py
│   ├── config.py            # Simple Pydantic BaseSettings loading from .env
│   ├── models.py            # Clean schemas: Campaign, ParsedEmail, DealState
│   ├── directory.py         # In-memory dictionary of 3 Middle East buyers and 3 Asian suppliers
│   ├── market.py            # Benchmark index lookup and simple port-to-port freight estimator
│   ├── math_engine.py       # Deterministic calculations: Net Margin = (Buyer CIF - Landed Cost) / Landed Cost
│   ├── prompts.py           # Clean Gemini prompts: parser prompt, buyer counter prompt, supplier RFQ prompt
│   ├── workflow.py          # Complete LangGraph StateGraph (State, Nodes, Routing Edges)
│   ├── fixtures.py          # Sample realistic trade emails for easy testing
│   └── main.py              # FastAPI app with 3 endpoints: create campaign, simulate turn, view status
├── static/
│   └── index.html           # Simple UI to launch campaigns and step through negotiation rounds
├── test_runner.py           # Self-contained unit test script verifying the math and graph logic
├── requirements.txt
└── .env.example
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
python test_runner.py
```
Expected output:
```text
====================================================================
  Commodity Arbitrage Multi-Agent System — Self-Contained Test Suite
====================================================================

--- 1. Testing Market & Directory Services ---
  [PASS] Found matching Basmati buyers
  [PASS] Gulf Food Trading found in directory
  [PASS] Found matching Basmati suppliers
  [PASS] Indus Rice Mills found in directory
  [PASS] Basmati benchmark is $900/MT (got 900.0)
  [PASS] Thai White benchmark is $520/MT (got 520.0)
  [PASS] Karachi -> Jebel Ali freight is $45/MT (got 45.0)

--- 2. Testing Deterministic Math Engine ---
  [PASS] Landed cost is $965.00/MT (got 965.0)
  [PASS] Net margin is 19.17% (got 19.17)
  [PASS] FOB ceiling is $945.00 (got 945.0)
  [PASS] CIF floor is $1111.00 (got 1111.0)

--- 3. Testing Risk Evaluation & Invariant Gates ---
  [PASS] Zero-Risk Invariant: Deal not viable without supplier
  [PASS] Reason cites Zero-Risk Invariant
  [PASS] Supplier above ceiling: Deal rejected
  [PASS] Reason cites FOB ceiling
  [PASS] Buyer below floor: Deal rejected
  [PASS] Reason cites CIF floor
  [PASS] Quantity mismatch: Deal rejected
  [PASS] Reason cites quantity mismatch
  [PASS] Viable deal accepted
  [PASS] Net margin exceeds target (got 19.17%)

--- 4. Testing LangGraph State Machine Negotiation Loop ---
  [Step 1] Ingesting low buyer inquiry...
  [PASS] Buyer terms extracted
  [PASS] Negotiation round = 1
  [PASS] Deal not viable yet (supplier allocation missing & price low)
  [PASS] Deal status is counter_sent
  [PASS] Counter-offer drafted to buyer
  [Step 2] Ingesting competitive supplier quote...
  [PASS] Supplier terms extracted
  [PASS] Negotiation round = 2
  [PASS] Deal still not viable because buyer price is still $950
  [Step 3] Buyer increases bid to USD 1150.00 CIF Jebel Ali...
  [PASS] Negotiation round = 3
  [PASS] Deal is now viable!
  [PASS] Net margin exceeds target (got 19.17%)
  [PASS] Deal closed formally
  [PASS] Supplier allocation locked first
  [PASS] Buyer confirmed second

====================================================================
  Test Results: 35 passed, 0 failed
====================================================================

  ALL TESTS PASSED SUCCESSFULLY! ***
```

### 4. Launch the Web Application
```bash
uvicorn app.main:app --port 8000 --reload
```
Open [http://localhost:8000](http://localhost:8000) to view the interactive dashboard.
