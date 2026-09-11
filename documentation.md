# 🌾 Commodity Arbitrage Multi-Agent System — Codebase Documentation

This document contains the complete architectural guide and source code for the **Commodity Arbitrage Multi-Agent System** refactored into a clean, educational, mid-level architecture.

---

## Table of Contents
1. [Architectural Principles](#1-architectural-principles)
2. [Project Layout](#2-project-layout)
3. [Configuration (`app/config.py`)](#3-configuration-appconfigpy)
4. [Data Models (`app/models.py`)](#4-data-models-appmodelspy)
5. [Counterparty Directory (`app/directory.py`)](#5-counterparty-directory-appdirectorypy)
6. [Market Benchmarks & Freight (`app/market.py`)](#6-market-benchmarks--freight-appmarketpy)
7. [Deterministic Math Engine (`app/math_engine.py`)](#7-deterministic-math-engine-appmath_enginepy)
8. [Gemini Prompt Templates (`app/prompts.py`)](#8-gemini-prompt-templates-apppromptspy)
9. [LangGraph State Machine (`app/workflow.py`)](#9-langgraph-state-machine-appworkflowpy)
10. [FastAPI Web Application (`app/main.py`)](#10-fastapi-web-application-appmainpy)
11. [Testing Dashboard UI (`static/index.html`)](#11-testing-dashboard-ui-staticindexhtml)
12. [Self-Contained Test Suite (`test_runner.py`)](#12-self-contained-test-suite-test_runnerpy)
13. [Environment & Dependencies](#13-environment--dependencies)

---

## 1. Architectural Principles

### 1. Separation of Brains
- **Large Language Model (Gemini)**: Confined strictly to unstructured text parsing (extracting volumes, prices, incoterms from messy email bodies) and polite business correspondence drafting.
- **Deterministic Math Engine (Pure Python)**: Owns 100% of the financial calculations, ocean freight normalization, dynamic price boundary enforcement, and net margin spread math.

### 2. The Zero-Risk Invariant
- The trading desk **never commits to a buyer** or accepts a buyer purchase order until a matching supplier allocation is formally locked. This eliminates short squeeze risk in volatile physical commodity markets.

### 3. Dynamic Market Grounding
- Eliminates hardcoded price limits. Floors and ceilings adapt dynamically based on benchmark market feeds and container shipping matrices:
  - **Dynamic FOB Ceiling** = $\text{Benchmark FOB} \times (1 + \text{Max Variance Tolerance \%})$
  - **Total Landed Cost** = $\text{Supplier FOB} + \text{Ocean Freight} + \text{Operating Buffer}$
  - **Dynamic CIF Floor** = $\text{Landed Cost at Ceiling} \times (1 + \text{Target Profit Margin \%})$
  - **Net Profit Margin \%** = $\frac{\text{Buyer CIF} - \text{Landed Cost}}{\text{Landed Cost}} \times 100$

### 4. Transparent LangGraph State Machine
- A single, transparent `StateGraph` in `app/workflow.py` coordinates:
  `parse_incoming_email` ➔ `fetch_market_data` ➔ `evaluate_risk` ➔ Conditional Router (`confirm_deal` | `counter_buyer` | `counter_supplier`).

---

## 2. Project Layout

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
│   ├── fixtures.py          # Realistic email fixtures for simulation
│   └── main.py              # FastAPI app with 3 endpoints: create campaign, simulate turn, view status
├── static/
│   └── index.html           # Simple UI to launch campaigns and step through negotiation rounds
├── test_runner.py           # Self-contained unit test script verifying the math and graph logic
├── requirements.txt
└── .env.example
```

---

## 3. Configuration (`app/config.py`)

```python
"""
Application configuration loaded from environment variables using pydantic-settings.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gemini LLM configuration
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Server settings
    port: int = 8000
    host: str = "127.0.0.1"

    # Default campaign arbitrage parameters
    default_target_margin_pct: float = 10.0
    default_max_variance_pct: float = 5.0
    default_buffer_usd_per_mt: float = 20.0


settings = Settings()
```

---

## 4. Data Models (`app/models.py`)

```python
"""
Domain data models and schemas for the commodity arbitrage system.
"""
from typing import Optional, TypedDict
from pydantic import BaseModel, Field


class Campaign(BaseModel):
    """Configuration parameters for a back-to-back commodity arbitrage campaign."""
    campaign_id: str = Field(..., description="Unique campaign ID (e.g. CAMP-001)")
    commodity: str = Field(default="Basmati 1121", description="Commodity variety")
    target_volume_mt: float = Field(default=500.0, gt=0, description="Target volume in Metric Tons")
    target_margin_pct: float = Field(default=10.0, ge=0.0, description="Minimum acceptable net profit margin %")
    max_variance_from_benchmark_pct: float = Field(default=5.0, ge=0.0, description="Max acceptable variance from benchmark %")
    buffer_usd_per_mt: float = Field(default=20.0, ge=0.0, description="Operating buffer and financing cost per MT")
    destination_port: str = Field(default="Jebel Ali", description="Destination port for buyer delivery")
    origin_port_default: str = Field(default="Karachi", description="Default supplier origin port")
    broken_percentage: float = Field(default=5.0, ge=0.0, description="Max broken grain tolerance %")


class ParsedEmail(BaseModel):
    """Structured commercial terms extracted from incoming trade correspondence."""
    sender_role: str = Field(..., description="'buyer' or 'supplier'")
    commodity: str = Field(default="Basmati 1121")
    quantity_mt: float = Field(default=500.0, gt=0)
    price_usd_per_mt: float = Field(..., ge=0, description="Unit price quoted in USD per Metric Ton")
    incoterm: str = Field(default="CIF", description="'FOB' or 'CIF'")
    port: Optional[str] = Field(default=None, description="Port cited in the quote")
    payment_terms: Optional[str] = Field(default="LC", description="e.g., 'LC at sight', 'CAD'")


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
    buyer_terms: Optional[ParsedEmail]
    supplier_terms: Optional[ParsedEmail]
    negotiation_round: int
    deal_status: str  # 'prospecting', 'counter_sent', 'approved', 'closed', 'rejected'
    is_deal_viable: bool
    net_margin_pct: float
    evaluation_reason: str
    latest_email: str
    active_role: str  # 'buyer' or 'supplier'
    buyer_draft: str
    supplier_draft: str


class CreateCampaignRequest(BaseModel):
    commodity: str = "Basmati 1121"
    target_volume_mt: float = 500.0
    target_margin_pct: float = 10.0
    max_variance_from_benchmark_pct: float = 5.0
    destination_port: str = "Jebel Ali"


class SimulateTurnRequest(BaseModel):
    campaign_id: str
    sender_role: str
    raw_email: str
```

---

## 5. Counterparty Directory (`app/directory.py`)

Provides verified buyers across the Middle East (UAE, Saudi Arabia) and Asian mills (Pakistan, Thailand, Vietnam):
- Buyers: Gulf Food Trading LLC (Jebel Ali), Al-Barakah Foods (Dammam), Emirates Grain Importers (Jebel Ali).
- Suppliers: Indus Rice Mills (Karachi), Thai Grain Corp (Bangkok), Mekong Delta Agro Exporters (Ho Chi Minh).
- Includes commodity matching helpers `get_buyers_for_commodity` and `get_suppliers_for_commodity`.

---

## 6. Market Benchmarks & Freight (`app/market.py`)

Provides live FOB benchmark prices:
- `Basmati 1121`: $900.00/MT FOB
- `Super Kernel Basmati`: $980.00/MT FOB
- `Thai White 5%`: $520.00/MT FOB
- `Jasmine Rice`: $780.00/MT FOB
- `Vietnam 5%`: $490.00/MT FOB

Container freight estimator with port pairs (Karachi, Mundra, Bangkok, Ho Chi Minh to Jebel Ali, Dammam).

---

## 7. Deterministic Math Engine (`app/math_engine.py`)

Pure Python calculations enforcing all pricing and invariant rules:
- `calculate_landed_cost(supplier_fob, freight, buffer_usd)`: Computes landed CIF cost.
- `calculate_net_margin(buyer_cif, landed_cost)`: Computes `((buyer_cif - landed_cost) / landed_cost) * 100`.
- `calculate_dynamic_bounds(...)`: Grounded dynamic FOB ceiling and CIF floor.
- `normalize_terms(...)`: Normalizes FOB/CIF incoterms.
- `evaluate_deal(...)`: Evaluates Zero-Risk Invariant, quantity alignment, supplier ceiling, buyer floor, and net margin hurdle.

---

## 8. Gemini Prompt Templates (`app/prompts.py`)

- `EMAIL_PARSER_PROMPT`: Instructs Gemini to parse raw emails into strict `ParsedEmail` JSON.
- `BUYER_COUNTER_PROMPT`: Drafts commercial CIF counter-offers or SCO confirmations.
- `SUPPLIER_RFQ_PROMPT`: Drafts target FOB counter-bids or volume allocation locks.

---

## 9. LangGraph State Machine (`app/workflow.py`)

Complete `StateGraph` compiling into `trade_graph`:
- Nodes:
  - `parse_incoming_email`: Extracts terms using Gemini or regex fallback.
  - `fetch_market_data`: Looks up benchmark rates and ocean freight.
  - `evaluate_risk`: Evaluates viability using the deterministic math engine.
  - `confirm_deal`: Locks supplier allocation first, then buyer confirmation.
  - `counter_buyer`: Counters firmly at dynamic CIF floor.
  - `counter_supplier`: Counters firmly at dynamic FOB ceiling.

---

## 10. FastAPI Web Application (`app/main.py`)

Exposes 3 core REST endpoints:
1. `POST /api/campaigns`: Launches campaign, queries directory, and initializes state.
2. `POST /api/negotiate`: Executes LangGraph turn for an incoming buyer/supplier email.
3. `GET /api/campaigns/{id}`: Returns live campaign metrics and transcripts.
- Mounts `static/index.html` at `GET /`.

---

## 11. Verification Results (`test_runner.py`)

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

====================================================================
  Test Results: 53 passed, 0 failed
====================================================================

  ALL TESTS PASSED SUCCESSFULLY! ***
```
