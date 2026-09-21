# 🌾 Commodity Arbitrage Multi-Agent System — Codebase Documentation

This document contains the complete architectural guide and source code for the **Commodity Arbitrage Multi-Agent System** upgraded with **Proactive Buyer-First Origination** and **Tactical Margin Maximization (Hard & Soft Limits)**.

---

## Table of Contents
1. [Architectural Principles](#1-architectural-principles)
2. [Project Layout](#2-project-layout)
3. [Configuration (`app/config.py`)](#3-configuration-appconfigpy)
4. [Data Models (`app/models.py`)](#4-data-models-appmodelspy)
5. [Counterparty Directory (`app/directory.py`)](#5-counterparty-directory-appdirectorypy)
6. [Market Benchmarks & Freight (`app/market.py`)](#6-market-benchmarks--freight-appmarketpy)
7. [Deterministic Math Engine (`app/math_engine.py`)](#7-deterministic-math-engine-appmath_enginepy)
8. [Gemini Dynamic Prompts (`app/prompts.py`)](#8-gemini-dynamic-prompts-apppromptspy)
9. [LangGraph State Machine (`app/workflow.py`)](#9-langgraph-state-machine-appworkflowpy)
10. [Email Transport Bridge (`app/email_service.py`)](#10-email-transport-bridge-appemail_servicepy)
11. [Live Gmail Negotiation Runner (`run_live_email_test.py`)](#11-live-gmail-negotiation-runner-run_live_email_testpy)
12. [FastAPI Web Application (`app/main.py`)](#12-fastapi-web-application-appmainpy)
13. [Testing Dashboard UI (`static/index.html`)](#13-testing-dashboard-ui-staticindexhtml)
14. [Verification Results (`test_runner.py`)](#14-verification-results-test_runnerpy)
15. [Environment & Dependencies](#15-environment--dependencies)

---

## 1. Architectural Principles

### 1. Separation of Brains
- **Large Language Model (Gemini)**: Confined strictly to unstructured text parsing (extracting volumes, prices, incoterms from messy email bodies) and polite business correspondence drafting.
- **Deterministic Math Engine (Pure Python)**: Owns 100% of the financial calculations, ocean freight normalization, dynamic price boundary enforcement, and tactical net spread margin math.

### 2. Proactive Buyer-First Origination
- Rather than idling passively for inbound buyer emails, the system proactively initiates trade origination:
  1. Queries the counterparty directory for verified Middle East buyers.
  2. Grounded in live market benchmark rates and container ocean freight, establishes an initial high-anchor CIF pitch:
     $$\text{Anchor CIF} = \text{Benchmark FOB} + \text{Freight} + \text{Operating Buffer} + \text{Soft Target Hurdle} + \$30.00$$
  3. Auto-drafts an outbound Cold Soft Corporate Offer (SCO) immediately upon campaign ignition.

### 3. Sequential Sourcing Progression & The Zero-Risk Invariant
- When the buyer signals interest and sets their CIF target:
  1. Buyer terms are locked in state.
  2. The system queries Asian rice mills and calculates our **Target FOB Acquisition Ceiling**:
     $$\text{Target FOB Ceiling} = \text{Buyer CIF} - \text{Freight} - \text{Operating Buffer} - \text{Soft Target Hurdle}$$
  3. Auto-drafts an urgent RFQ to the supplier asking for quotes at or below this ceiling.
- **Zero-Risk Invariant**: The desk never commits to a buyer or confirms a purchase order until matching supplier allocation is secured.

### 4. Tactical Margin Maximization (Tiered Profit Hurdles)
Arbitrage profitability is evaluated using a deterministic three-tier hurdle strategy:
1. **Landed Cost**: $\text{Supplier FOB} + \text{Ocean Freight} + \text{Buffer}$
2. **Net Spread**: $\text{Buyer CIF} - \text{Landed Cost}$
3. **Hard Limit Check**: If $\text{Net Spread} < \text{Hard Floor}$ ($\$50.00/\text{MT}$), reject deal immediately ($\text{action} = \texttt{REJECT\_HARD}$, $\text{viable} = \text{False}$).
4. **Soft Concession Zone**: If $\text{Net Spread} < \text{Soft Target}$ ($\$120.00/\text{MT}$) and $\text{Round} < \text{Max Rounds}$ ($3$), counter to maximize margin ($\text{action} = \texttt{COUNTER\_TO\_MAXIMIZE}$, $\text{viable} = \text{True}$).
5. **Optimal / Final Close**: If $\text{Net Spread} \ge \text{Soft Target}$ or $\text{Round} \ge \text{Max Rounds}$, close deal ($\text{action} = \texttt{ACCEPT\_AND\_CLOSE}$, $\text{viable} = \text{True}$).

### 5. 4-Step Sequential Pipeline
```text
[1. Buyer Discovered & Pitched] ➔ [2. Buyer Terms Locked] ➔ [3. Supplier RFQ Dispatched] ➔ [4. Margin Optimized & Closed]
```

### 6. End-to-End Autonomous Multi-Turn Execution (`auto_run=True`)
- When launching a campaign with `auto_run=True` (the default), the system autonomously executes the entire physical commodity arbitrage negotiation lifecycle end-to-end:
  1. Selects verified counterparties from `app/directory.py` and dispatches the proactive Cold SCO.
  2. Ingests realistic buyer interest with an initial CIF counter-bid.
  3. Loops through negotiation rounds:
     - Dispatches RFQ to supplier and ingests supplier FOB quotation.
     - Evaluates the deal through `evaluate_deal_strategy()`.
     - Automatically feeds tactical concessions into subsequent rounds if `action == "COUNTER_TO_MAXIMIZE"`.
     - Once `action == "ACCEPT_AND_CLOSE"`, locks supplier allocation first (enforcing the Zero-Risk Invariant) and confirms buyer acceptance.
  4. Stores the complete correspondence audit transcript and finalized metrics in the ledger and returns the closed deal immediately.
- When `auto_run=False`, the system operates in interactive single-turn mode for manual UI testing and custom email simulation.

---

## 2. Project Layout

```text
rice-arbitrage-agent/
├── app/
│   ├── __init__.py
│   ├── config.py            # Pydantic BaseSettings: Gemini, server & email transport config
│   ├── models.py            # Schemas: Campaign (tiered hurdles), ParsedEmail, DealState (actions)
│   ├── directory.py         # Directory of verified Middle East buyers and Asian export mills
│   ├── market.py            # Live Yahoo Rough Rice index scaler & container freight estimator
│   ├── math_engine.py       # Deterministic calculations: evaluate_deal_strategy, bounds, margins
│   ├── prompts.py           # 100% LLM dynamic prompts: Cold SCO, counter, RFQ, close, reject
│   ├── email_service.py     # Lightweight SMTP/IMAP bridge for live Gmail negotiation testing
│   ├── workflow.py          # Complete LangGraph StateGraph (dynamic writer nodes & risk routing)
│   ├── fixtures.py          # Realistic trade correspondence fixtures
│   └── main.py              # FastAPI app: proactive launch, negotiate turns, live ledger
├── static/
│   └── index.html           # Modern dashboard with 4-step pipeline, proactive Cold SCO & stepper
├── run_live_email_test.py   # Standalone interactive/live Gmail negotiation testing CLI
├── test_runner.py           # Self-contained unit & integration test suite (138 passing tests)
├── requirements.txt
└── .env.example
```

---

## 3. Configuration (`app/config.py`)

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gemini LLM configuration
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    llm_temperature: float = 0.1

    # Server settings
    port: int = 8000
    host: str = "127.0.0.1"

    # Default campaign arbitrage parameters
    desk_name: str = "Global Agro Arbitrage Desk"
    default_commodity: str = "Basmati 1121"
    default_target_volume_mt: float = 500.0
    default_target_margin_pct: float = 10.0
    default_max_variance_pct: float = 5.0
    default_buffer_usd_per_mt: float = 20.0
    default_destination_port: str = "Jebel Ali"
    default_origin_port: str = "Karachi"
    default_broken_percentage: float = 5.0
    default_payment_terms: str = "100% LC at sight"

    # Live Email Transport Configuration
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    imap_server: str = "imap.gmail.com"
    email_user: str = ""
    email_pass: str = ""
    my_test_email: str = ""
```

---

## 4. Data Models (`app/models.py`)

```python
from typing import Literal, Optional, TypedDict
from pydantic import BaseModel, Field

from app.config import settings


class Campaign(BaseModel):
    """Configuration parameters for a back-to-back commodity arbitrage campaign."""
    campaign_id: str = Field(..., description="Unique campaign ID (e.g. CAMP-001)")
    commodity: str = Field(default_factory=lambda: settings.default_commodity)
    target_volume_mt: float = Field(default_factory=lambda: settings.default_target_volume_mt, gt=0)
    target_margin_pct: float = Field(default_factory=lambda: settings.default_target_margin_pct, ge=0.0)
    max_variance_from_benchmark_pct: float = Field(default_factory=lambda: settings.default_max_variance_pct, ge=0.0)
    buffer_usd_per_mt: float = Field(default_factory=lambda: settings.default_buffer_usd_per_mt, ge=0.0)
    destination_port: str = Field(default_factory=lambda: settings.default_destination_port)
    origin_port_default: str = Field(default_factory=lambda: settings.default_origin_port)
    broken_percentage: float = Field(default_factory=lambda: settings.default_broken_percentage, ge=0.0)
    # Tiered Profit Hurdles & Negotiation Cap
    min_profit_per_mt_hard: float = Field(default=50.0, ge=0.0, description="Non-negotiable floor; reject < $50/MT")
    min_profit_per_mt_soft: float = Field(default=120.0, ge=0.0, description="Target ideal spread; counter if below")
    max_negotiation_rounds: int = Field(default=3, ge=1, description="Max bargaining rounds before accepting floor")


class ParsedEmail(BaseModel):
    """Structured commercial terms extracted from incoming trade correspondence."""
    sender_role: str = Field(..., description="'buyer' or 'supplier'")
    commodity: str = Field(default="Basmati 1121")
    quantity_mt: float = Field(default=500.0, gt=0)
    price_usd_per_mt: float = Field(..., ge=0)
    incoterm: str = Field(default="CIF", description="'FOB' or 'CIF'")
    port: Optional[str] = Field(default=None)
    payment_terms: Optional[str] = Field(default="LC")


class Counterparty(BaseModel):
    """Buyer or supplier listed in the marketplace directory."""
    id: str
    name: str
    role: str  # 'buyer' or 'supplier'
    country: str
    primary_port: str
    preferred_commodities: list[str]
    contact_email: str
    reputation_score: float = Field(default=4.5, ge=1.0, le=5.0)


class DealState(TypedDict, total=False):
    """Complete LangGraph state dictionary shared across workflow nodes."""
    campaign: Campaign
    benchmark_fob_usd: float
    freight_cost_usd: float
    dynamic_fob_ceiling: float
    dynamic_cif_floor: float
    target_fob_ceiling: float
    anchor_cif_usd: float
    buyer_terms: Optional[ParsedEmail]
    supplier_terms: Optional[ParsedEmail]
    negotiation_round: int
    deal_status: str  # 'prospecting', 'counter_sent', 'approved', 'closed', 'rejected'
    action: Optional[Literal["REJECT_HARD", "COUNTER_TO_MAXIMIZE", "ACCEPT_AND_CLOSE"]]
    pipeline_step: int  # 1 to 4
    is_deal_viable: bool
    net_spread_usd: float
    net_margin_pct: float
    evaluation_reason: str
    latest_email: str
    active_role: str  # 'buyer' or 'supplier'
    buyer_draft: str
    supplier_draft: str


class CreateCampaignRequest(BaseModel):
    commodity: str = Field(default_factory=lambda: settings.default_commodity)
    target_volume_mt: float = Field(default_factory=lambda: settings.default_target_volume_mt)
    target_margin_pct: float = Field(default_factory=lambda: settings.default_target_margin_pct)
    max_variance_from_benchmark_pct: float = Field(default_factory=lambda: settings.default_max_variance_pct)
    destination_port: str = Field(default_factory=lambda: settings.default_destination_port)
    min_profit_per_mt_hard: float = Field(default=50.0)
    min_profit_per_mt_soft: float = Field(default=120.0)
    max_negotiation_rounds: int = Field(default=3)


class SimulateTurnRequest(BaseModel):
    campaign_id: str
    sender_role: str  # 'buyer' or 'supplier'
    raw_email: str
```

---

## 5. Counterparty Directory (`app/directory.py`)

Maintains directory of verified counterparties:
- **Middle East Buyers**: Gulf Food Trading LLC (Jebel Ali, UAE), Al-Barakah Foods (Dammam, Saudi Arabia), Emirates Grain Importers (Jebel Ali, UAE).
- **Asian Export Mills**: Indus Rice Mills (Karachi, Pakistan), Thai Grain Corp (Bangkok, Thailand), Mekong Delta Agro Exporters (Ho Chi Minh, Vietnam).
- Includes query helpers: `get_buyers_for_commodity` and `get_suppliers_for_commodity`.

---

## 6. Market Benchmarks & Freight (`app/market.py`)

Fetches live FOB export and commodity futures prices directly from the **Yahoo Finance CBOT Rough Rice (`ZR=F`)** endpoint with a 6-hour local disk cache (`market_cache.json`) and resilient static fallbacks (`STATIC_BENCHMARKS`):
- **Live Feed URL**: `https://query1.finance.yahoo.com/v8/finance/chart/ZR=F?interval=1d&range=5d`
- **Unit Conversion**: Converts raw futures quoted in USD per hundredweight (cwt) to USD per Metric Ton:
  $$\text{Rough Rice (USD/MT)} = \text{regularMarketPrice (USD/cwt)} \times 22.046$$
  *(For example: \$15.985/cwt $\times$ 22.046 $\approx$ \$352.41/MT)*
- **Dynamic Commodity Scaling**: Uses the live rough rice index ratio relative to base reference ($16.00/cwt $\approx$ \$352.74/MT) to dynamically scale milled export benchmarks:
  - `Basmati 1121`: Dynamically scaled around baseline ($900.00/MT FOB)
  - `Super Kernel Basmati`: Dynamically scaled around baseline ($980.00/MT FOB)
  - `Thai White 5%`: Dynamically scaled around baseline ($496.00/MT FOB)
  - `Thai White 25%`: Dynamically scaled around baseline ($397.00/MT FOB)
  - `Jasmine Rice`: Dynamically scaled around baseline ($780.00/MT FOB)
  - `Thai Hom Mali`: Dynamically scaled around baseline ($1,170.00/MT FOB)
  - `Pathumthani Fragrant`: Dynamically scaled around baseline ($478.00/MT FOB)
  - `Vietnam 5%`: Dynamically scaled around baseline ($490.00/MT FOB)
- **Offline Resiliency**: If external feeds are unreachable or timeout, the desk seamlessly falls back to `STATIC_BENCHMARKS` without interrupting trading operations.
- **Fallback HTML Scraper**: Maintains `parse_thai_rice_html` for historical TREA table compatibility.

Dynamic port-to-port container ocean freight matrix (Karachi, Mundra, Bangkok, Ho Chi Minh to Jebel Ali, Dammam) with dynamic bunker fuel surcharges (BAF).

---

## 7. Deterministic Math Engine (`app/math_engine.py`)

### `evaluate_deal_strategy`
```python
def evaluate_deal_strategy(
    buyer_cif: float,
    supplier_fob: float,
    freight: float,
    buffer_usd: float,
    round_num: int,
    campaign: Campaign,
) -> dict:
    """
    Evaluate tactical margin maximization strategy with hard and soft hurdles:
    1. Landed cost: supplier_fob + freight + buffer_usd
    2. Net spread: buyer_cif - landed_cost
    3. Hard Limit: net_spread < min_profit_per_mt_hard -> REJECT_HARD, viable=False
    4. Soft Concession: net_spread < min_profit_per_mt_soft AND round_num < max_rounds -> COUNTER_TO_MAXIMIZE, viable=True
    5. Optimal / Final Close: net_spread >= min_profit_per_mt_soft OR round_num >= max_rounds -> ACCEPT_AND_CLOSE, viable=True
    """
    landed_cost = round(supplier_fob + freight + buffer_usd, 2)
    net_spread = round(buyer_cif - landed_cost, 2)
    net_margin_pct = calculate_net_margin(buyer_cif, landed_cost)

    # 3. Hard Limit Check
    if net_spread < campaign.min_profit_per_mt_hard:
        return {
            "action": "REJECT_HARD",
            "viable": False,
            "landed_cost": landed_cost,
            "net_spread": net_spread,
            "net_margin_pct": net_margin_pct,
            "reason": (
                f"Net spread ${net_spread:.2f}/MT is below non-negotiable hard floor of "
                f"${campaign.min_profit_per_mt_hard:.2f}/MT (Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f}). Deal rejected."
            ),
        }

    # 4. Soft Concession Zone
    if net_spread < campaign.min_profit_per_mt_soft and round_num < campaign.max_negotiation_rounds:
        return {
            "action": "COUNTER_TO_MAXIMIZE",
            "viable": True,
            "landed_cost": landed_cost,
            "net_spread": net_spread,
            "net_margin_pct": net_margin_pct,
            "reason": (
                f"Net spread ${net_spread:.2f}/MT clears hard floor (${campaign.min_profit_per_mt_hard:.2f}/MT) "
                f"but is below soft target (${campaign.min_profit_per_mt_soft:.2f}/MT) at round {round_num}/{campaign.max_negotiation_rounds}. "
                f"Countering to maximize margin."
            ),
        }

    # 5. Optimal / Final Close
    is_optimal = net_spread >= campaign.min_profit_per_mt_soft
    close_desc = (
        f"clears soft target of ${campaign.min_profit_per_mt_soft:.2f}/MT"
        if is_optimal
        else f"accepted at round {round_num}/{campaign.max_negotiation_rounds} above hard floor (${campaign.min_profit_per_mt_hard:.2f}/MT)"
    )
    return {
        "action": "ACCEPT_AND_CLOSE",
        "viable": True,
        "landed_cost": landed_cost,
        "net_spread": net_spread,
        "net_margin_pct": net_margin_pct,
        "reason": (
            f"Deal viable with net spread of ${net_spread:.2f}/MT ({close_desc}). "
            f"Supplier FOB: ${supplier_fob:.2f}, Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f}."
        ),
    }
```

---

## 8. Gemini Dynamic Prompts (`app/prompts.py`)

All trade correspondence is **100% LLM-driven** with **zero hardcoded email templates or static subject lines**. Every generation requires the model to compose both a contextual, professional subject line and an authentic business body using the strict delimiter contract:

```text
SUBJECT: <dynamic contextual subject line>
BODY:
<dynamic authentic email body>
```

### Prompt Inventory
1. **`EMAIL_PARSER_PROMPT`**: Instructs Gemini to parse unstructured trade correspondence into strict `ParsedEmail` JSON.
2. **`PROACTIVE_COLD_SCO_PROMPT`**: Directs Gemini to compose a high-anchor, non-binding Cold Soft Corporate Offer (SCO) for Middle East buyers citing specification, target CIF price, payment terms, and validity.
3. **`BUYER_COUNTER_PROMPT`**: Generates persuasive counter-bids defending our minimum CIF target floor by citing real logistics constraints, bunker fuel surcharges, packaging, or export mill availability.
4. **`SUPPLIER_RFQ_PROMPT`**: Formats structured, assertive RFQs targeting our maximum FOB acquisition ceiling and requesting prompt dispatch.
5. **`DEAL_CONFIRMATION_PROMPT`**: Composes formal commercial closing notices locking volume allocation and requesting draft Letter of Credit (LC) issuance.
6. **`DEAL_REJECTION_PROMPT`**: Composes polite commercial decline notices citing margin divergences while preserving the counterparty relationship for future fixtures.

---

## 9. LangGraph State Machine (`app/workflow.py`)

### Architecture & Nodes
- **`parse_incoming_email_node`**: Parses correspondence into `ParsedEmail`. When the buyer signals target CIF, locks `buyer_terms`, calculates `target_fob_ceiling`, and auto-drafts an urgent RFQ to the Asian supplier via Gemini.
- **`fetch_market_data_node`**: Looks up dynamic Yahoo Rough Rice index scaling and container ocean freight.
- **`evaluate_risk_node`**: Executes pure Python math engine with tiered profit hurdles ($50/MT hard floor, $120/MT soft target).
- **`confirm_deal_node`**: Enforces Zero-Risk Invariant: invokes Gemini to dynamically draft supplier allocation lock first, then buyer deal confirmation second.
- **`counter_buyer_node`**: Invokes Gemini dynamically to compose counter-offer defending our minimum CIF floor.
- **`counter_supplier_node`**: Invokes Gemini dynamically to compose supplier counter-bid defending our FOB ceiling.
- **`reject_deal_node`**: Invokes Gemini dynamically to compose formal commercial decline citing margin hurdles.

### Parsing & Safe Fallback Helper
```python
def split_subject_and_body(raw_text: str, default_subject: str = "Commercial Trade Notice") -> tuple[str, str]:
    """Extract SUBJECT: and BODY: cleanly from LLM output with robust regex & fallback."""
    # Matches 'SUBJECT: <line>\nBODY:\n<rest>'
    match = re.search(r"SUBJECT:\s*(.*?)\r?\n+BODY:\s*(.*)", raw_text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    # Fallback to standard email headers or clean splitting
    lines = raw_text.strip().splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).strip()
        return subject, body
    return default_subject, raw_text.strip()
```

### Conditional Routing (`route_after_evaluation`)
```python
def route_after_evaluation(state: DealState) -> Literal["confirm_deal", "counter_buyer", "counter_supplier", "reject_deal"]:
    action = state.get("action")
    has_supplier = state.get("supplier_terms") is not None
    active_role = state.get("active_role", "buyer")

    if action == "REJECT_HARD":
        return "reject_deal"
    if action == "ACCEPT_AND_CLOSE" and has_supplier:
        return "confirm_deal"
    if action == "COUNTER_TO_MAXIMIZE":
        return "counter_buyer" if active_role == "buyer" else "counter_supplier"
    if state.get("is_deal_viable") and has_supplier:
        return "confirm_deal"
    return "counter_buyer" if active_role == "buyer" else "counter_supplier"
```

---

## 10. Email Transport Bridge (`app/email_service.py`)

A lightweight, zero-external-dependency email bridge utilizing Python standard libraries (`smtplib`, `imaplib`, `email`):

- **`parse_email_draft(raw_draft: str, default_subject: str)`**: Parses `SUBJECT:` and `BODY:` from agent-generated drafts.
- **`send_email(to_email: str, raw_draft: str) -> bool`**:
  - Connects to SMTP server (`smtp.gmail.com:587`) with `STARTTLS`.
  - Authenticates using `settings.email_user` and `settings.email_pass` (Gmail App Password).
  - Formats clean MIME plaintext message (`Content-Type: text/plain; charset="utf-8"`).
  - Dispatches message. If credentials are unset, safely logs a mock dispatch and returns `True` for offline testing.
- **`check_latest_reply(expected_subject_snippet: str) -> str | None`**:
  - Connects to IMAP server (`imap.gmail.com:993`) via SSL.
  - Authenticates and searches `INBOX` for `UNSEEN` messages.
  - Matches message headers against the subject snippet.
  - Extracts clean plaintext body across multipart MIME structures, strips quoted replies (`On ... wrote:` / `>`), marks message as `\Seen`, and returns the extracted body text.

---

## 11. Live Gmail Negotiation Runner (`run_live_email_test.py`)

A standalone CLI utility for executing real-world, multi-turn email negotiations against a live inbox:

### Usage
```bash
# Target live inbox from arguments
python run_live_email_test.py buyer@yourcompany.com

# Or configure via .env (MY_TEST_EMAIL) and run directly
python run_live_email_test.py
```

### Execution Flow
1. Initializes `DealState` with `MY_TEST_EMAIL` as the active buyer counterparty.
2. Dynamically queries Gemini to compose an authentic high-anchor Cold SCO.
3. Dispatches the Cold SCO via SMTP to the target inbox.
4. Enters a polling loop (checking IMAP every 10 seconds for `UNSEEN` replies).
5. If Gmail credentials are not configured, gracefully falls back to interactive console prompts where the operator can enter buyer counter-offers directly.
6. Each inbound reply triggers the LangGraph state machine:
   - Ingests and parses buyer counter-bid.
   - Evaluates landed costs and tactical profit hurdles ($50/MT hard, $120/MT soft).
   - Generates dynamic LLM counter or deal acceptance notice.
   - Dispatches outbound reply via SMTP.
   - Continues loop until deal status reaches `closed` or `rejected`.

---

## 12. FastAPI Web Application (`app/main.py`)

- `POST /api/campaigns`: Launches campaign, automatically targets Middle East buyers, computes high-anchor CIF pitch ($1,115.00/MT), auto-drafts Cold SCO dynamically via Gemini, and initializes `pipeline_step = 1`. Supports `auto_run=True` for autonomous multi-turn settlement.
- `POST /api/negotiate`: Executes LangGraph turn, updating state with `action`, `pipeline_step`, `net_spread_usd`, and dynamic drafts.
- `GET /api/campaigns/{id}`: Returns live ledger status, metrics, and complete audit transcripts.
- `GET /api/directory`: Returns registered counterparties.
- `GET /`: Serves interactive dashboard `static/index.html`.

---

## 13. Testing Dashboard UI (`static/index.html`)

Modern responsive UI featuring:
- **Proactive Outreach Display**: Dynamic Cold SCO rendered immediately on campaign launch.
- **4-Step Visual Pipeline Indicator**:
  `[1. Buyer Discovered & Pitched] ➔ [2. Buyer Terms Locked] ➔ [3. Supplier RFQ Dispatched] ➔ [4. Margin Optimized & Closed]`.
- **Automated Stepper**: "Simulate Next Negotiation Turn" button steps through counter-offers.
- **Visual Progress Bar**: Shows profit spread climbing toward the soft target ($120/MT).
- **Dual Outbound Correspondence**: Real-time drafting of buyer SCO/counters and supplier RFQ/allocation locks.

---

## 14. Verification Results (`test_runner.py`)

All **138 unit and integration tests** pass cleanly with 0 failures:

```text
====================================================================
  Commodity Arbitrage Multi-Agent System — Self-Contained Test Suite
====================================================================

--- 1. Testing Market & Directory Services ---
  [PASS] Found matching Basmati buyers
  [PASS] Gulf Food Trading found in directory
  [PASS] Found matching Basmati suppliers
  [PASS] Indus Rice Mills found in directory
  [PASS] Market rates dictionary loaded
  [PASS] market_cache.json created on disk
  [PASS] HTML parser extracted Thai White 5%: 496.0
  [PASS] HTML parser extracted Pathumthani Fragrant
  [PASS] Yahoo parser converted 16.0 cwt to $352.74/MT
  [PASS] Scaled Basmati matches baseline at 16.0 cwt
  [PASS] Basmati benchmark is valid (got $899.16/MT)
  [PASS] Basmati benchmark dynamically scaled near baseline (got $899.16/MT)
  [PASS] Thai White benchmark is valid (got $495.53/MT)
  [PASS] Karachi -> Jebel Ali base freight is $45/MT (got 45.0)
  [PASS] Karachi -> Jebel Ali freight with +4% fuel surcharge is $46.80/MT (got 46.8)

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

--- 5. Testing FastAPI REST Endpoints & UI Serving ---
  [PASS] GET / returns 200 OK (got 200)
  [PASS] GET /api/directory returns 200 OK
  [PASS] Directory lists at least 3 buyers
  [PASS] Directory lists at least 3 suppliers
  [PASS] POST /api/campaigns returns 200 OK
  [PASS] Matching buyers discovered
  [PASS] Matching suppliers discovered
  [PASS] POST /api/negotiate Turn 1 returns 200
  [PASS] Turn 1: Deal not viable
  [PASS] Turn 1: Counter-offer sent
  [PASS] POST /api/negotiate Turn 2 returns 200
  [PASS] POST /api/negotiate Turn 3 returns 200
  [PASS] Turn 3: Deal is fully viable
  [PASS] Turn 3: Deal status is closed
  [PASS] Turn 3: Net margin exceeds target (19.17%)
  [PASS] GET /api/campaigns/{id} returns 200
  [PASS] Ledger persists closed status
  [PASS] Ledger tracks round 3

--- 6. Testing Proactive Origination & Tactical Margin Maximization ---
  [PASS] POST /api/campaigns returns 200
  [PASS] Outbound buyer draft created automatically
  [PASS] Draft is a Cold Soft Corporate Offer (SCO)
  [PASS] Draft specifies CIF destination port
  [PASS] Pipeline starts at Step 1 (Buyer Discovered & Pitched)
  [PASS] Initial high-anchor CIF matches formula: $1114.16 (got 1114.16)
  [PASS] Deal yielding $30/MT is rejected under hard floor: REJECT_HARD
  [PASS] Deal yielding $30/MT is marked non-viable
  [PASS] Net spread is correctly calculated as $30.00/MT (got 30.0)
  [PASS] LangGraph sets action to REJECT_HARD
  [PASS] LangGraph routes to reject_deal node
  [PASS] Deal marked non-viable on hard rejection
  [PASS] Round 1: $80/MT is countered to maximize: COUNTER_TO_MAXIMIZE
  [PASS] Round 1: Deal is viable (profitable concession zone)
  [PASS] Net spread is $80.00/MT (got 80.0)
  [PASS] Round 2: $80/MT is countered in round 2: COUNTER_TO_MAXIMIZE
  [PASS] Round 3: $80/MT accepted once rounds expire: ACCEPT_AND_CLOSE
  [PASS] Round 3: Deal accepted and viable
  [PASS] Turn 1 API call succeeds
  [PASS] Buyer terms locked in state
  [PASS] Supplier RFQ auto-drafted upon buyer signal
  [PASS] Supplier draft is an RFQ
  [PASS] Target FOB ceiling is $860.00 (got 860.0)
  [PASS] Turn 2 API call succeeds
  [PASS] Supplier terms locked in state
  [PASS] Turn 2 action is COUNTER_TO_MAXIMIZE (got COUNTER_TO_MAXIMIZE)
  [PASS] Turn 2 net spread is $80.00/MT (got 80.0)
  [PASS] Turn 3 API call succeeds
  [PASS] Turn 3 action is ACCEPT_AND_CLOSE (got ACCEPT_AND_CLOSE)
  [PASS] Turn 3 deal status is closed
  [PASS] Turn 3 net spread reaches soft target $120.00/MT (got 120.0)

--- 7. Testing Autonomous End-to-End Campaign Execution (auto_run=True) ---
  [PASS] POST /api/campaigns with auto_run=True returns 200
  [PASS] Autonomous campaign deal_status is closed (got closed)
  [PASS] Action is ACCEPT_AND_CLOSE (got ACCEPT_AND_CLOSE)
  [PASS] Pipeline reached Step 4 (got 4)
  [PASS] Negotiation rounds completed >= 1 (got 3)
  [PASS] Final net spread >= hard floor $50/MT (got $120.0/MT)
  [PASS] Final net margin > 0% (got 12.59%)
  [PASS] Audit transcript contains full correspondence (got 8 messages)
  [PASS] Transcript records initial Cold SCO
  [PASS] Transcript records buyer initial bid
  [PASS] Transcript records supplier RFQ
  [PASS] Transcript records supplier quotation
  [PASS] Transcript records supplier allocation lock
  [PASS] Transcript records final buyer acceptance
  [PASS] GET /api/campaigns/{id} returns 200 for autonomous deal
  [PASS] Ledger confirms deal_status closed
  [PASS] Ledger confirms net spread $120.0/MT
  [PASS] Ledger contains full audit transcript
  [PASS] POST /api/campaigns with default auto_run=True for Thai White returns 200
  [PASS] Thai White autonomous campaign closed
  [PASS] Thai White final spread >= $40/MT (got $100.0/MT)
  [PASS] Thai White transcript recorded

--- 8. Testing Dynamic LLM Email Generation & Live Transport ---
  [PASS] split_subject_and_body extracts SUBJECT and BODY cleanly
  [PASS] Extracted subject matches expected
  [PASS] Extracted body matches expected
  [PASS] split_subject_and_body handles standard email headers
  [PASS] split_subject_and_body handles plain text fallback
  [PASS] parse_email_draft returns parsed subject and body
  [PASS] send_email mock succeeds when credentials missing
  [PASS] clean_reply_body strips quoted replies
  [PASS] clean_reply_body strips leading angle bracket quotes
  [PASS] check_latest_reply returns None when unconfigured
  [PASS] Proactive SCO prompt includes target price
  [PASS] Buyer counter prompt includes floor CIF
  [PASS] Supplier RFQ prompt includes FOB ceiling
  [PASS] Deal confirmation prompt includes spread
  [PASS] Deal rejection prompt includes reason
  [PASS] Dynamic proactive SCO generator produces subject and body
  [PASS] Dynamic SCO body mentions target volume or commodity
  [PASS] counter_buyer_node produces dynamic subject and body
  [PASS] counter_buyer_node draft defends CIF target floor
  [PASS] counter_supplier_node produces dynamic subject and body
  [PASS] counter_supplier_node draft targets FOB ceiling
  [PASS] confirm_deal_node produces dynamic confirmation drafts
  [PASS] reject_deal_node produces dynamic rejection draft
  [PASS] reject_deal_node cites margin divergence

====================================================================
  Test Results: 138 passed, 0 failed
====================================================================

  ALL TESTS PASSED SUCCESSFULLY! ***
```

---

## 15. Environment & Dependencies

```text
fastapi>=0.110.0
uvicorn>=0.28.0
langgraph>=0.0.30
pydantic>=2.6.0
pydantic-settings>=2.2.0
httpx>=0.27.0
beautifulsoup4>=4.12.0
google-generativeai>=0.4.0
pytest>=8.0.0
```
