# 🌾 Commodity Arbitrage Multi-Agent System — Codebase Documentation

This document contains the complete architectural guide and source code for the **Commodity Arbitrage Multi-Agent System** upgraded with **Proactive Buyer-First Origination**, **Tactical Margin Maximization (Hard & Soft Limits)**, **RFC 5322 Thread-Preserving Email Bridge**, **Semantic LLM-Driven Email Parser**, and **Centralized Structured Logging System**.

---

## Table of Contents
1. [Architectural Principles](#1-architectural-principles)
2. [Project Layout](#2-project-layout)
3. [Configuration (`app/config.py`)](#3-configuration-appconfigpy)
4. [Centralized Structured Logger (`app/logger.py`)](#4-centralized-structured-logger-apploggerpy)
5. [Data Models (`app/models.py`)](#5-data-models-appmodelspy)
6. [Counterparty Directory (`app/directory.py`)](#6-counterparty-directory-appdirectorypy)
7. [Market Benchmarks & Freight (`app/market.py`)](#7-market-benchmarks--freight-appmarketpy)
8. [Deterministic Math Engine (`app/math_engine.py`)](#8-deterministic-math-engine-appmath_enginepy)
9. [Gemini Dynamic Prompts (`app/prompts.py`)](#9-gemini-dynamic-prompts-apppromptspy)
10. [LangGraph State Machine (`app/workflow.py`)](#10-langgraph-state-machine-appworkflowpy)
11. [Email Transport Bridge (`app/email_service.py`)](#11-email-transport-bridge-appemail_servicepy)
12. [Live Gmail Negotiation Runner (`run_live_email_test.py`)](#12-live-gmail-negotiation-runner-run_live_email_testpy)
13. [FastAPI Web Application (`app/main.py`)](#13-fastapi-web-application-appmainpy)
14. [Testing Dashboard UI (`static/index.html`)](#14-testing-dashboard-ui-staticindexhtml)
15. [Verification Results (`test_runner.py`)](#15-verification-results-test_runnerpy)
16. [Environment & Dependencies](#16-environment--dependencies)

---

## 1. Architectural Principles

### 1. Separation of Brains
- **Large Language Model (Gemini)**: Confined strictly to semantic unstructured text parsing (extracting commercial intents, volumes, prices, incoterms, and ports from messy email bodies) and authentic business correspondence drafting.
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
- **Zero-Risk Invariant**: The desk never commits to a buyer or confirms a purchase order until matching supplier allocation is secured. In live email worker polling, the Autonomous Supplier Agent proactively secures supplier volume allocation before evaluating buyer acceptance terms.

### 4. Tactical Margin Maximization (Tiered Profit Hurdles)
Arbitrage profitability is evaluated using a deterministic three-tier hurdle strategy:
1. **Landed Cost**: $\text{Supplier FOB} + \text{Ocean Freight} + \text{Buffer}$
2. **Net Spread**: $\text{Buyer CIF} - \text{Landed Cost}$
3. **Hard Limit Check**: If $\text{Net Spread} < \text{Hard Floor}$ ($\$50.00/\text{MT}$), reject deal immediately ($\text{action} = \texttt{REJECT\_HARD}$, $\text{viable} = \text{False}$).
4. **Buyer Acceptance Close**: If the buyer accepted desk terms and $\text{Net Spread} \ge \text{Hard Floor}$, close immediately ($\text{action} = \texttt{ACCEPT\_AND\_CLOSE}$, $\text{viable} = \text{True}$) without demanding further concessions.
5. **Soft Concession Zone**: If $\text{Net Spread} < \text{Soft Target}$ ($\$120.00/\text{MT}$) and $\text{Round} < \text{Max Rounds}$ ($3$), counter to maximize margin ($\text{action} = \texttt{COUNTER\_TO\_MAXIMIZE}$, $\text{viable} = \text{True}$).
6. **Optimal / Final Close**: If $\text{Net Spread} \ge \text{Soft Target}$ or $\text{Round} \ge \text{Max Rounds}$, close deal ($\text{action} = \texttt{ACCEPT\_AND\_CLOSE}$, $\text{viable} = \text{True}$).

### 5. RFC 5322 Thread-Preserving Conversation Continuity
- Every outbound email generates a unique RFC 5322 `<Message-ID>`.
- Outbound responses attach `In-Reply-To` and `References` headers referencing the counterparty's message ID and prior thread lineage.
- Subjects are normalized (`Re: <clean_subject>`) to prevent subject duplication, ensuring multi-round bargaining stays grouped in a single Gmail conversation thread.

### 6. Semantic LLM-Driven Intent Understanding
- Eliminates brittle regex keyword matching. Gemini evaluates deal context (counterparty role, last proposed rate, commodity, volume) and classifies intent (`ACCEPTANCE`, `COUNTER_OFFER`, `REJECTION`, `INQUIRY`).
- Seamlessly resolves counterparty agreements (*"We accept your counter proposal"*) to the desk's proposed target CIF rate even if numeric digits are omitted.
- Transparent offline regex fallback ensures zero crashes during quota limits or network partitions.

### 7. Centralized Structured Logging System
- Captures all system events (IMAP detections, campaign matching, LLM parsing results, margin evaluations, node decisions, and outbound dispatches) in both formatted console stdout and a rotating file log at `logs/arbitrage_desk.log`.

---

## 2. Project Layout

```text
rice-arbitrage-agent/
├── app/
│   ├── __init__.py
│   ├── config.py            # Pydantic BaseSettings: Gemini, server & email transport config
│   ├── logger.py            # Centralized structured logger: stdout & rotating file logs/arbitrage_desk.log
│   ├── models.py            # Schemas: Campaign, ParsedEmail (intent, acceptance), DealState (threading)
│   ├── directory.py         # Directory of verified Middle East buyers and Asian export mills
│   ├── market.py            # Live Yahoo Rough Rice index scaler & container freight estimator
│   ├── math_engine.py       # Deterministic calculations: evaluate_deal_strategy, bounds, margins
│   ├── prompts.py           # 100% LLM dynamic prompts: Semantic parser, Cold SCO, counter, RFQ, close
│   ├── email_service.py     # RFC 5322 SMTP/IMAP bridge for thread-preserving live Gmail negotiation
│   ├── workflow.py          # LangGraph StateGraph (semantic parser, dynamic writers, risk routing)
│   ├── fixtures.py          # Realistic trade correspondence fixtures
│   └── main.py              # FastAPI app: email_polling_worker, thread correlation, live ledger
├── logs/
│   └── arbitrage_desk.log   # Rotating structured log file (10MB max, 5 backups, UTF-8)
├── static/
│   └── index.html           # Modern dashboard with 4-step pipeline, proactive Cold SCO & stepper
├── run_live_email_test.py   # Standalone interactive/live Gmail negotiation testing CLI
├── test_runner.py           # Self-contained unit & integration test suite (155 passing tests)
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

## 4. Centralized Structured Logger (`app/logger.py`)

```python
"""
Centralized structured logger for the Commodity Arbitrage Desk.
Provides colorized/formatted console output and rotating file logging to logs/arbitrage_desk.log.
"""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOGS_DIR / "arbitrage_desk.log"

_CONFIGURED = False


def setup_logger(name: str = "arbitrage_desk", level: int = logging.INFO) -> logging.Logger:
    """
    Configure and return a standardized logger instance.
    Configures parent 'arbitrage_desk' hierarchy logger with handlers so all child
    loggers (e.g. 'arbitrage_desk.workflow', 'arbitrage_desk.main') inherit handlers automatically.
    """
    global _CONFIGURED

    # Always ensure the root parent logger 'arbitrage_desk' has the handlers
    parent_logger = logging.getLogger("arbitrage_desk")
    parent_logger.setLevel(level)

    if not _CONFIGURED or not parent_logger.handlers:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        formatter = logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)-7s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Remove existing handlers to avoid duplication if any
        parent_logger.handlers.clear()

        # 1. Console Handler (stdout so it always appears in terminal)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        parent_logger.addHandler(console_handler)

        # 2. Rotating File Handler (10MB max, keep 5 backups)
        try:
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            parent_logger.addHandler(file_handler)
        except Exception as e:
            parent_logger.warning(f"Could not initialize file handler at {LOG_FILE}: {e}")

        # Stop propagation to root so root uvicorn logger doesn't duplicate it
        parent_logger.propagate = False
        _CONFIGURED = True

    # If requested logger is a child or the parent itself
    target_logger = logging.getLogger(name)
    target_logger.setLevel(level)
    # Ensure child loggers propagate to 'arbitrage_desk'
    if name != "arbitrage_desk":
        target_logger.propagate = True

    return target_logger


def get_logger(name: str = "arbitrage_desk") -> logging.Logger:
    """Retrieve or initialize the desk logger."""
    return setup_logger(name)
```

---

## 5. Data Models (`app/models.py`)

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
    min_profit_per_mt_hard: float = Field(default=50.0, ge=0.0, description="Non-negotiable floor; reject < $50/MT")
    min_profit_per_mt_soft: float = Field(default=120.0, ge=0.0, description="Target ideal spread; counter if below")
    max_negotiation_rounds: int = Field(default=3, ge=1, description="Max bargaining rounds before settling")


class ParsedEmail(BaseModel):
    """Structured commercial terms extracted from incoming trade correspondence."""
    sender_role: str = Field(..., description="'buyer' or 'supplier'")
    commodity: str = Field(default="Basmati 1121")
    quantity_mt: float = Field(default=500.0, gt=0)
    price_usd_per_mt: float = Field(..., ge=0, description="Unit price quoted in USD per Metric Ton")
    incoterm: str = Field(default="CIF", description="'FOB' or 'CIF'")
    port: Optional[str] = Field(default=None)
    payment_terms: Optional[str] = Field(default="LC")
    intent: Optional[str] = Field(default="COUNTER_OFFER", description="'ACCEPTANCE', 'COUNTER_OFFER', 'REJECTION', 'INQUIRY'")
    is_acceptance: bool = Field(default=False, description="Whether sender accepts desk terms")
    summary: Optional[str] = Field(default=None, description="Semantic summary of message intent")


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
    audit_transcript: list[dict]
    thread_subject: Optional[str]
    last_buyer_message_id: Optional[str]
    buyer_references: Optional[str]
    last_supplier_message_id: Optional[str]
    supplier_references: Optional[str]
    last_counter_cif_usd: Optional[float]
    buyer_accepted: Optional[bool]
    target_buyer_name: Optional[str]
    target_buyer_email: Optional[str]
    discovered_buyers: Optional[list[dict]]
    discovered_suppliers: Optional[list[dict]]
    trigger: Optional[str]
    skip_email_dispatch: Optional[bool]


class CreateCampaignRequest(BaseModel):
    commodity: str = Field(default_factory=lambda: settings.default_commodity)
    target_volume_mt: float = Field(default_factory=lambda: settings.default_target_volume_mt)
    target_margin_pct: float = Field(default_factory=lambda: settings.default_target_margin_pct)
    max_variance_from_benchmark_pct: float = Field(default_factory=lambda: settings.default_max_variance_pct)
    destination_port: str = Field(default_factory=lambda: settings.default_destination_port)
    min_profit_per_mt_hard: float = Field(default=50.0)
    min_profit_per_mt_soft: float = Field(default=120.0)
    max_negotiation_rounds: int = Field(default=3)
    auto_run: bool = Field(default=True, description="Whether to run negotiation lifecycle automatically")


class SimulateTurnRequest(BaseModel):
    campaign_id: str
    sender_role: str  # 'buyer' or 'supplier'
    raw_email: str
```

---

## 6. Counterparty Directory (`app/directory.py`)

Maintains directory of verified counterparties:
- **Middle East Buyers**: Gulf Food Trading LLC (Jebel Ali, UAE), Al-Barakah Foods (Dammam, Saudi Arabia), Emirates Grain Importers (Jebel Ali, UAE).
- **Asian Export Mills**: Indus Rice Mills (Karachi, Pakistan), Thai Grain Corp (Bangkok, Thailand), Mekong Delta Agro Exporters (Ho Chi Minh, Vietnam).
- Includes query helpers: `get_buyers_for_commodity` and `get_suppliers_for_commodity`.

---

## 7. Market Benchmarks & Freight (`app/market.py`)

Fetches live FOB export and commodity futures prices directly from the **Yahoo Finance CBOT Rough Rice (`ZR=F`)** endpoint with a 6-hour local disk cache (`market_cache.json`) and resilient static fallbacks (`STATIC_BENCHMARKS`):
- **Live Feed URL**: `https://query1.finance.yahoo.com/v8/finance/chart/ZR=F?interval=1d&range=5d`
- **Unit Conversion**: Converts raw futures quoted in USD per hundredweight (cwt) to USD per Metric Ton:
  $$\text{Rough Rice (USD/MT)} = \text{regularMarketPrice (USD/cwt)} \times 22.046$$
- **Dynamic Commodity Scaling**: Uses live rough rice index ratio relative to base reference ($16.00/cwt $\approx$ \$352.74/MT) to dynamically scale milled export benchmarks (Basmati 1121, Super Kernel, Thai White, Jasmine, Vietnam 5%).
- **Container Freight Matrix**: Karachi, Mundra, Bangkok, Ho Chi Minh to Jebel Ali, Dammam with dynamic bunker fuel surcharges (BAF).

---

## 8. Deterministic Math Engine (`app/math_engine.py`)

```python
def evaluate_deal_strategy(
    buyer_cif: float,
    supplier_fob: float,
    freight: float,
    buffer_usd: float,
    round_num: int,
    campaign: Campaign,
    buyer_accepted: bool = False,
) -> dict:
    """
    Evaluate tactical margin maximization strategy with hard and soft hurdles:
    1. Landed cost: supplier_fob + freight + buffer_usd
    2. Net spread: buyer_cif - landed_cost
    3. Hard Limit: net_spread < min_profit_per_mt_hard -> REJECT_HARD, viable=False
    4. Buyer Acceptance: buyer_accepted and net_spread >= hard floor -> ACCEPT_AND_CLOSE
    5. Soft Concession: net_spread < min_profit_per_mt_soft AND round_num < max_rounds -> COUNTER_TO_MAXIMIZE
    6. Optimal / Final Close: net_spread >= min_profit_per_mt_soft OR round_num >= max_rounds -> ACCEPT_AND_CLOSE
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

    # Immediate close if buyer accepted and spread clears hard floor
    if buyer_accepted:
        return {
            "action": "ACCEPT_AND_CLOSE",
            "viable": True,
            "landed_cost": landed_cost,
            "net_spread": net_spread,
            "net_margin_pct": net_margin_pct,
            "reason": (
                f"Deal accepted: Buyer accepted desk proposal at net spread of ${net_spread:.2f}/MT "
                f"(Hard floor: ${campaign.min_profit_per_mt_hard:.2f}/MT, Landed: ${landed_cost:.2f}, Buyer CIF: ${buyer_cif:.2f})."
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

## 9. Gemini Dynamic Prompts (`app/prompts.py`)

All trade correspondence is **100% LLM-driven** with **zero hardcoded email templates or static subject lines**. Every generation requires the model to compose both a contextual subject line and an authentic business body:

```text
SUBJECT: <dynamic contextual subject line>
BODY:
<dynamic authentic email body>
```

### Upgraded Semantic Parser Prompt (`EMAIL_PARSER_PROMPT`)
```text
You are an expert commodity trading desk assistant.
Analyze the following incoming trade email semantically to classify counterparty intent and extract commercial trade terms.

Deal Context:
- Counterparty Role: {role}
- Desk's Last Proposed Rate: USD {last_proposed_price:.2f}/MT (Use this price if counterparty accepts or confirms without repeating digits)
- Commodity Default: {commodity}
- Target Volume Default: {default_volume_mt:,.0f} MT

Output a single valid JSON object matching this exact schema:
{
  "sender_role": "{role}",
  "intent": "ACCEPTANCE" | "COUNTER_OFFER" | "REJECTION" | "INQUIRY",
  "is_acceptance": true or false,
  "commodity": "<commodity name>",
  "quantity_mt": <number in metric tons>,
  "price_usd_per_mt": <numeric unit price in USD per MT>,
  "incoterm": "CIF" or "FOB",
  "port": "<port name>",
  "payment_terms": "<e.g. 100% LC at sight, CAD>",
  "summary": "<1-2 sentence semantic summary of counterparty intent>"
}
```

---

## 10. LangGraph State Machine (`app/workflow.py`)

### Semantic Parsing Function (`_parse_email_content`)
```python
def _parse_email_content(raw_text: str, role: str, deal_context: Optional[dict] = None) -> ParsedEmail:
    """
    Extract commercial trade terms and counterparty intent using Gemini LLM if configured,
    otherwise fallback gracefully to deterministic regex pattern matching.
    """
    ctx = deal_context or {}
    last_proposed_price = ctx.get("last_proposed_price", 1111.0)
    commodity = ctx.get("commodity", settings.default_commodity)
    default_volume_mt = ctx.get("default_volume_mt", settings.default_target_volume_mt)

    if settings.gemini_api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.gemini_api_key)
            model = genai.GenerativeModel(
                settings.gemini_model,
                generation_config={"temperature": settings.llm_temperature},
            )
            prompt = EMAIL_PARSER_PROMPT.format(
                role=role,
                last_proposed_price=last_proposed_price,
                commodity=commodity,
                default_volume_mt=default_volume_mt,
                raw_email=raw_text,
            )
            resp = model.generate_content(prompt)
            clean_text = resp.text.strip().removeprefix("```json").removesuffix("```").strip()
            data = json.loads(clean_text)
            parsed = ParsedEmail(**data)
            logger.info(
                f"[LLM PARSER] Inbound email parsed for role='{role}': "
                f"intent='{parsed.intent}', is_acceptance={parsed.is_acceptance}, "
                f"price=${parsed.price_usd_per_mt:.2f}/MT, qty={parsed.quantity_mt:,.0f}MT, "
                f"port='{parsed.port}', summary='{parsed.summary}'"
            )
            return parsed
        except Exception as exc:
            logger.warning(
                f"[LLM PARSER] Semantic LLM extraction unavailable ({exc.__class__.__name__}: {exc}). "
                f"Switching to deterministic regex parser."
            )

    # Deterministic Regex Fallback Parser
    p_match = re.search(r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)", raw_text, re.IGNORECASE)
    if not p_match:
        p_match = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$|\$|per\s*MT|/\s*MT)", raw_text, re.IGNORECASE)
    price = float(p_match.group(1).replace(",", "")) if p_match else 0.0

    is_acceptance = bool(re.search(r"\b(accept|agreed|agreement|confirm|proceed|deal|order|allocation)\b", raw_text, re.IGNORECASE))
    intent = "ACCEPTANCE" if is_acceptance else ("COUNTER_OFFER" if price > 0 else "INQUIRY")
    if price <= 0.0 and is_acceptance:
        price = last_proposed_price

    qty_match = re.search(r"(\d[\d,]*)\s*(?:MT|metric\s*ton)", raw_text, re.IGNORECASE)
    quantity = float(qty_match.group(1).replace(",", "")) if qty_match else default_volume_mt

    incoterm = "CIF" if "cif" in raw_text.lower() else ("FOB" if "fob" in raw_text.lower() else ("CIF" if role == "buyer" else "FOB"))
    port = settings.default_destination_port if role == "buyer" else settings.default_origin_port
    return ParsedEmail(
        sender_role=role,
        commodity=commodity,
        quantity_mt=quantity,
        price_usd_per_mt=price,
        incoterm=incoterm,
        port=port,
        intent=intent,
        is_acceptance=is_acceptance,
    )
```

### Inbound Email Node (`parse_incoming_email_node`)
- Passes `deal_context` (including `last_counter_cif_usd` and baseline prices) to `_parse_email_content`.
- When buyer acceptance is detected, resolves `terms.price_usd_per_mt` to the desk's proposed target CIF rate and sets `buyer_accepted = True`.
- Logs structured events through `logger.info()`.

### Proactive Outreach Node (`proactive_outreach_node`)
Handles trade origination natively as a first-class citizen of the LangGraph state machine:
1. **Counterparty Discovery**: Queries `get_buyers_for_commodity()` and `get_suppliers_for_commodity()` to locate verified Middle East buyers.
2. **Dynamic Anchor CIF Calculation**: Computes live market benchmarks and container freight:
   $$\text{Anchor CIF} = \text{Benchmark FOB} + \text{Freight} + \text{Operating Buffer} + \text{Soft Target Hurdle} + \$30.00$$
3. **Dynamic Cold SCO Composition**: Calls Gemini with `PROACTIVE_COLD_SCO_PROMPT` to synthesize an authentic, non-binding corporate offer.
4. **RFC 5322 Thread-Preserving Dispatch**: Generates a cryptographically unique `<Message-ID>` and dispatches email via `email_service.send_email()`.
5. **State Ledger Transition**: Sets `pipeline_step = 1`, `deal_status = "prospecting"`, appends the outbound offer to `audit_transcript`, and caches `anchor_cif_usd`.

### Conditional Entry Point Routing & Graph Architecture
The trade graph dynamically selects its execution path based on the presence of inbound correspondence:

```python
def route_entry_point(state: DealState) -> Literal["proactive_outreach", "parse_incoming_email", "__end__"]:
    """
    Operational entry point router:
    - On inbound negotiation turn (incoming email text present): routes to parse_incoming_email.
    - On campaign launch (no incoming email, pipeline_step == 0): routes to proactive_outreach (Step 1).
    - If outreach has already been done and waiting for buyer reply (no incoming email, pipeline_step >= 1): routes directly to END.
    """
    latest_email = (state.get("latest_email") or "").strip()
    if latest_email:
        return "parse_incoming_email"

    # Idempotency guard: If outreach has already completed and we are waiting for buyer reply, halt at END
    if state.get("pipeline_step", 0) >= 1 or state.get("deal_status") in ["prospecting", "counter_sent", "approved", "closed", "rejected"]:
        camp = state.get("campaign")
        cid = getattr(camp, "campaign_id", None) or (camp.get("campaign_id") if isinstance(camp, dict) else "unknown")
        logger.info(
            f"[WORKFLOW:Router] Campaign '{cid}' is waiting for counterparty reply (status: '{state.get('deal_status')}'). "
            f"No inbound email present; halting at END without duplicate outreach."
        )
        return END

    return "proactive_outreach"


def build_trade_graph():
    """Construct and compile the LangGraph StateGraph."""
    builder = StateGraph(DealState)

    builder.add_node("proactive_outreach", proactive_outreach_node)
    builder.add_node("parse_incoming_email", parse_incoming_email_node)
    builder.add_node("fetch_market_data", fetch_market_data_node)
    builder.add_node("evaluate_risk", evaluate_risk_node)
    builder.add_node("confirm_deal", confirm_deal_node)
    builder.add_node("counter_buyer", counter_buyer_node)
    builder.add_node("counter_supplier", counter_supplier_node)
    builder.add_node("reject_deal", reject_deal_node)

    # Conditional entry point determines initial workflow path:
    builder.set_conditional_entry_point(
        route_entry_point,
        {
            "proactive_outreach": "proactive_outreach",
            "parse_incoming_email": "parse_incoming_email",
            END: END,
        },
    )

    # Path A: Proactive outreach finishes Step 1 and pauses for buyer response
    builder.add_edge("proactive_outreach", END)

    # Path B: Standard multi-turn negotiation pipeline
    builder.add_edge("parse_incoming_email", "fetch_market_data")
    builder.add_edge("fetch_market_data", "evaluate_risk")
    builder.add_conditional_edges(
        "evaluate_risk",
        route_negotiation_step,
        {
            "confirm_deal": "confirm_deal",
            "counter_buyer": "counter_buyer",
            "counter_supplier": "counter_supplier",
            "reject_deal": "reject_deal",
        },
    )
    builder.add_edge("confirm_deal", END)
    builder.add_edge("counter_buyer", END)
    builder.add_edge("counter_supplier", END)
    builder.add_edge("reject_deal", END)

    return builder.compile()
```

---

## 11. Email Transport Bridge (`app/email_service.py`)

A lightweight, zero-external-dependency email bridge utilizing Python standard libraries (`smtplib`, `imaplib`, `email`):

### String Subclass with Threading Metadata (`EmailReply`)
```python
class EmailReply(str):
    """
    Subclass of str representing the email body with attached metadata.
    Ensures 100% backward compatibility with string operations while exposing email headers.
    """
    subject: str = ""
    sender: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""

    def __new__(cls, body: str, subject: str = "", sender: str = "", message_id: str = "", in_reply_to: str = "", references: str = ""):
        obj = super().__new__(cls, body)
        obj.subject = subject
        obj.sender = sender
        obj.message_id = message_id
        obj.in_reply_to = in_reply_to
        obj.references = references
        return obj
```

### Thread-Preserving Dispatch (`send_email`)
```python
def send_email(
    to_email: str,
    raw_draft: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    thread_subject: Optional[str] = None,
    custom_message_id: Optional[str] = None,
) -> bool:
    """Dispatches email using SMTP STARTTLS while setting RFC 5322 In-Reply-To and References headers."""
```
- Includes subject normalization: `normalize_thread_subject()` avoids duplicate `Re: Re: ...` chains.
- Simulated domain safety: Directory domains (`@indusrice.pk`, `@thaigrain.co.th`, `@gulffood.ae`) are safely mocked without sending real SMTP packets.

---

## 12. Live Gmail Negotiation Runner (`run_live_email_test.py`)

A standalone CLI utility for executing real-world, multi-turn email negotiations against a live inbox. Campaign ignition delegates trade origination directly to `trade_graph.invoke(initial_state)` which executes `proactive_outreach_node` natively.

```bash
# Target live inbox from arguments
python run_live_email_test.py buyer@yourcompany.com

# Or configure via .env (MY_TEST_EMAIL) and run directly
python run_live_email_test.py
```

---

## 13. FastAPI Web Application (`app/main.py`)

- **`lifespan`**: Initializes parent logger, starts background `email_polling_worker`, and snapshots existing inbox unread IDs.
- **`email_polling_worker`**:
  - Continuously polls IMAP inbox for counterparty replies.
  - Matches message to active campaign in `CAMPAIGN_LEDGER`.
  - Proactively secures supplier volume allocation before evaluating buyer terms (Zero-Risk Invariant).
  - Invokes `trade_graph` and dispatches response within the exact same Gmail conversation thread using RFC 5322 headers.
- **`POST /api/campaigns`**: Launches campaign by delegating directly to `trade_graph.invoke(initial_state)`, which executes `proactive_outreach_node` natively to discover Middle East buyers, compute benchmark & container freight bounds, auto-draft the dynamic Cold SCO with Gemini, dispatch outbound email with RFC 5322 headers, and initialize the campaign ledger.
- **`POST /api/negotiate`**: Executes LangGraph turn, updates ledger, and logs spread, margin %, and tactical decisions.
- **`GET /api/campaigns/{id}`**: Returns live campaign ledger state and complete audit transcripts.

---

## 14. Testing Dashboard UI (`static/index.html`)

Modern responsive UI featuring:
- **Proactive Outreach Display**: Dynamic Cold SCO rendered immediately on campaign launch.
- **4-Step Visual Pipeline Indicator**:
  `[1. Buyer Discovered & Pitched] ➔ [2. Buyer Terms Locked] ➔ [3. Supplier RFQ Dispatched] ➔ [4. Margin Optimized & Closed]`.
- **Automated Stepper**: "Simulate Next Negotiation Turn" button steps through counter-offers.
- **Visual Progress Bar**: Shows profit spread climbing toward the soft target ($120/MT).
- **Dual Outbound Correspondence**: Real-time drafting of buyer SCO/counters and supplier RFQ/allocation locks.

---

## 15. Verification Results (`test_runner.py`)

All **155 unit and integration tests** pass cleanly with 0 failures:

```text
====================================================================
  Test Results: 155 passed, 0 failed
====================================================================

  ALL TESTS PASSED SUCCESSFULLY! ***
```

### Coverage Highlights
1. **Directory & Market Services**: Live Yahoo Rough Rice index scaling, container freight calculations with bunker fuel adjustments.
2. **Deterministic Math Engine**: Landed cost, net spread, dynamic bounds, hard/soft profit hurdles, and immediate close on buyer acceptance.
3. **Risk Worker & Invariant Gates**: Zero-Risk Invariant strictly enforced before purchase commitment.
4. **LangGraph State Machine**: Sequential 4-step pipeline, dynamic counter drafting, and concession loops.
5. **FastAPI Endpoints**: REST lifecycle endpoints, ledger persistence, and autonomous end-to-end execution (`auto_run=True`).
6. **RFC 5322 Thread Continuity**: Message-ID tracking, `In-Reply-To`, `References`, and `Re:` subject normalization.
7. **Semantic LLM Email Parser**: Intent extraction, deal context grounding, and graceful quota fallback.
8. **Centralized Logging System**: Rotating file handler and formatted stdout console emission.

---

## 16. Environment & Dependencies

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
