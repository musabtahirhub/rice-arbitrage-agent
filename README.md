# 🌾 Autonomous Commodity Arbitrage Desk (Rice Trading Agent)

An autonomous multi-agent physical commodity arbitrage system engineered for international rice trading between **South/Southeast Asian origin mills** (Pakistan, India, Thailand, Vietnam) and **Middle Eastern import hubs** (UAE, Saudi Arabia).

The system integrates **dynamic market benchmark grounding**, **autonomous counterparty discovery**, **parallel agent negotiation**, and **deterministic zero-risk enforcement**.

---

## 🚀 Key Architectural Pillars

### 1. Dynamic Market Grounding (`app/market_data.py`)
- Replaces hardcoded price boundaries with live benchmark indices and container freight estimation.
- Supports canonical indices:
  - **Basmati 1121 Sella Rice** (FOB Karachi / Mundra)
  - **Thai White Rice 5% Broken** (FOB Bangkok)
  - **Thai Hom Mali Jasmine Rice** (FOB Bangkok)
  - **Vietnam White Rice 5% Broken** (FOB Ho Chi Minh)
- Ocean container freight matrix (TEU 20ft container lots) between origination ports and destination ports (Jebel Ali, Dammam).
- Integrated resilient fallback caching with timestamped offline market defaults.

### 2. Autonomous Counterparty Discovery & Campaign Ignition (`app/directory.py`, `app/agents/discovery.py`)
- **Marketplace Directory**: Pre-verified data store of Middle Eastern buyers and Asian rice mills/exporters, containing port specifications, preferred commodity varieties, volume capacities, and reputation ratings.
- **Automated Campaign Ignition**:
  - Automatically queries directory for counterparties matching campaign specifications.
  - Computes dynamic indicative pricing:
    - **Landed Benchmark CIF** = $\text{Benchmark FOB} + \text{Ocean Freight}$
    - **Indicative Buyer Offer CIF** = $\text{Landed CIF} \times (1 + \text{Target Margin} + \text{Sell Premium})$
    - **Supplier Target FOB Ceiling** = $\text{Benchmark FOB} \times (1 + \text{Max Variance Tolerance})$
  - Concurrently drafts tailored **Buyer Cold Outreach** emails and **Supplier RFQs** (Request for Quotations).

### 3. Multi-Agent Orchestration & Deterministic Risk Barrier (`app/agents/orchestrator.py`, `app/arbitrage_engine.py`)
- **Buyer Agent (`app/agents/buyer_agent.py`)**: Parses buyer CIF inquiries and drafts non-binding Soft Corporate Offers (SCO) or counter-offers.
- **Supplier Agent (`app/agents/supplier_agent.py`)**: Parses supplier FOB quotes and drafts RFQ counters or Proforma Invoice (PI) confirmation requests.
- **Deterministic Risk Worker Barrier**:
  - LLMs only parse text and draft letters; **all mathematical calculations and deal evaluations are executed strictly in pure Python**.
  - **Zero-Risk Invariant**: Supplier allocation **must be locked before** buyer terms are accepted to eliminate short squeeze exposure.
  - Rejects supplier quotes exceeding the dynamic FOB ceiling and buyer bids below the dynamic CIF floor.

### 4. Interactive Web Dashboard & FastAPI Backend (`app/main.py`, `app/static/index.html`)
- High-performance FastAPI application serving a modern single-page dashboard.
- Styled with Tailwind CSS CDN and custom dark glassmorphism.
- Features:
  - **Marketplace Directory Explorer**: Browse registered buyers and suppliers with badges for ports, commodities, and ratings.
  - **One-Click Campaign Ignition & Auto-Outreach**: Launches discovery and renders outbound buyer offers and supplier RFQs.
  - **Simulation Engine**: Triggers realistic counterparty negotiation responses (`viable`, `lowball_buyer`, `high_supplier`) and visualizes real-time margin tracking and contract drafts.

---

## 📂 Project Structure

```text
rice-arbitrage-agent/
├── app/
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── buyer_agent.py        # Buyer CIF negotiation & SCO drafting
│   │   ├── supplier_agent.py     # Supplier FOB negotiation & PI request drafting
│   │   ├── discovery.py          # Counterparty matching & automated outreach
│   │   └── orchestrator.py       # LangGraph multi-agent coordination graph
│   ├── static/
│   │   └── index.html            # Dark-mode dashboard with Tailwind CSS & Glassmorphism
│   ├── arbitrage_engine.py       # Pure Python deterministic risk & boundary engine
│   ├── directory.py              # Counterparty data store & discovery helpers
│   ├── fixtures.py               # Pre-configured trade email templates
│   ├── main.py                   # FastAPI application & REST endpoints
│   ├── market_data.py            # Live benchmark indices & container freight matrix
│   ├── prompts.py                # Structured extraction and negotiation prompt templates
│   ├── schemas.py                # Strict Pydantic v2 domain models & trade state
│   └── workflow.py               # Orchestrator entrypoint wrapper
├── .env.example                  # Environment variable template
├── .gitignore                    # Git ignore file (excludes secrets and cache)
├── requirements.txt              # Core project dependencies
└── test_runner.py                # 75-assertion offline test suite
```

---

## 🛠️ Quick Start

### 1. Clone & Setup Environment

```bash
git clone https://github.com/musabtahirhub/rice-arbitrage-agent.git
cd rice-arbitrage-agent

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment (Optional)

Copy the environment template:

```bash
cp .env.example .env
```

If you wish to use live Google Gemini Flash extraction, populate `GEMINI_API_KEY` in `.env`. The core system and test runner are designed to operate fully offline using deterministic engines and regex parsers.

### 3. Run the Test Suite

Run the 75-assertion comprehensive test suite:

```bash
python test_runner.py
```

**Verification Highlights:**
- **Part 1**: Deterministic arbitrage engine & Incoterm normalization (15 tests)
- **Part 2**: MarketDataService dynamic bound computation (11 tests)
- **Part 3**: Multi-turn negotiation simulation & zero-risk rule (20 tests)
- **Part 4**: Marketplace directory querying, campaign ignition, and response loop (29 tests)

### 4. Launch the Web Application

Start the FastAPI application with Uvicorn:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open your browser and navigate to:
👉 **`http://127.0.0.1:8000`**

---

## 🔌 API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Single-page interactive testing dashboard |
| `GET` | `/api/directory` | Returns all registered buyers and suppliers from directory |
| `POST` | `/api/campaigns` | Initializes campaign & triggers autonomous discovery + outreach |
| `GET` | `/api/campaigns/{id}` | Retrieves campaign ledger state and thread history |
| `POST` | `/api/campaigns/{id}/simulate-responses` | Simulates realistic counterparty replies (`viable`, `lowball`, etc.) |
| `POST` | `/api/simulate-email` | Ingests and processes an email through the risk barrier |
| `GET` | `/api/fixtures` | Sample email templates for manual testing |

---

## 🛡️ License

MIT License. Designed for algorithmic commodity trade desks and physical grain arbitrage.
