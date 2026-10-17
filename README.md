# 🌾 Autonomous Commodity Arbitrage Desk

An AI-assisted trading desk that runs physical **rice arbitrage** between Asian mills (Pakistan, Thailand, Vietnam) and Middle Eastern buyers (UAE, Saudi Arabia). It negotiates both sides by email, prices every deal with deterministic math, and only closes after a human approves.

**Stack:** Python 3.11 · FastAPI · LangGraph · Google Gemini 2.5 Flash · Langfuse · PostgreSQL · Gmail API

---

## 📉 The Business Problem

A rice trading intermediary buys from a mill and sells to an importer, and earns the **spread** between the two prices. In practice this is hard to run well:

- **Slow, manual negotiation.** A trader juggles dozens of email threads with suppliers and buyers across time zones. Each one needs a quote, a counter and a follow-up.
- **Thin, volatile margins.** Benchmark prices (Basmati 1121, Thai White 5%, Jasmine, Vietnam 5%) and ocean freight move constantly. A quote that was profitable this morning can lose money by the afternoon.
- **Short-squeeze risk.** If the desk promises volume to a buyer before the mill has locked allocation, it can end up obliged to deliver goods it cannot source.
- **Unstructured data.** Prices, quantities, incoterms and ports arrive as free-text email, and the counterparty also asks about payment, packing and shipment.
- **Pricing mistakes are costly.** Getting landed cost or margin wrong on a multi-tonne shipment is a direct loss, so this arithmetic cannot be left to a language model.

## 💡 The Solution

A **deterministic negotiation workflow** with an LLM used only where it is strong: reading and writing email.

> This is a LangGraph **state machine, not a ReAct agent**. The LLM never chooses tools or the next step. Routing and all financial decisions are plain Python.

```
inbound email ─► parse ─► market data ─► risk evaluation ─► route
                                                              ├─ viable ───────► human approval ─► confirm
                                                              ├─ buyer too low ► counter buyer
                                                              ├─ supplier high ► counter supplier
                                                              └─ rejection ────► courteous close-out
```

| Job | Done by |
|---|---|
| Extract price, quantity, incoterm and port from an email | Gemini (with a regex fallback) |
| Draft quotes, counters, confirmations and declines | Gemini (with template fallbacks) |
| Landed cost, margin, FOB ceiling and CIF floor | **Pure Python** |
| Routing, risk gating and deal finalization | **Pure Python + human approval** |

**Core pricing formulas**

- `Landed Cost = Supplier FOB + Freight + Buffer`
- `Net Margin % = (Buyer CIF − Landed) / Landed × 100`
- `FOB Ceiling = Benchmark × (1 + max variance %)`
- `CIF Floor = Landed at ceiling × (1 + target margin %)`

## ✨ Key Features

- **Two-sided email negotiation.** Sends RFQs and quotes, counters both buyer and supplier, and keeps their threads separate.
- **Deterministic pricing engine.** Margins and price bounds come from live market benchmarks and freight data, so the LLM never does the math.
- **Zero-risk invariant.** The supplier allocation is locked *before* the buyer is confirmed.
- **Human-in-the-loop approval.** The graph pauses at an `interrupt()` and resumes only when the trader approves, edits or rejects the deal.
- **Intent-aware replies.** It tells apart a price offer, a rejection and a general question. Questions about payment, shipment or specs are answered from deal facts without touching the price.
- **Gmail integration.** Pub/Sub push or IMAP polling, Primary-inbox filtering and exactly-once processing of every message.
- **Trading desk UI.** A web dashboard for launching campaigns, managing a buyer and supplier contact book, and reviewing and approving deals.
- **Durable and auditable.** Postgres checkpointing, so negotiations survive restarts, plus a full audit trail of every exchange.
- **Observability.** Langfuse traces every graph run and Gemini call, grouped by campaign.
- **Runs without keys.** With no Gemini, Langfuse or email credentials the app falls back to regex, templates and a no-op tracer, so demos and tests work offline.

## 🚀 Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env          # optional: add GEMINI_API_KEY, Langfuse and email settings
uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000> for the desk UI, or <http://localhost:8000/docs> for the API.

**Full stack with Postgres and migrations:** `docker compose up --build`

**Tests:** `pytest --ignore=tests/test_live_email.py` (`tests/test_live_email.py` sends real email, so run it only on purpose).

**Live Gmail:** run `python scripts/gmail_auth.py` once to create a refresh token. The server never opens a browser.

## 🗂️ Project Layout

```
app/
├── graph/        LangGraph state machine and nodes (buyer, supplier, risk, finalization, inquiry)
├── pricing/      Deterministic formulas, strategy, deal evaluation, market data
├── services/     Gemini client, campaign runner, mail ingestion (Gmail / IMAP / SMTP)
├── routes/       REST API: campaigns, negotiation and approval, webhooks
├── db/           SQLAlchemy models and audit trail
└── schemas.py    Typed node updates and approval payloads (Pydantic V2)
static/           Trading desk UI
migrations/       Alembic migrations
tests/
```

## 🔐 Security Note

`.env`, `credentials.json` and `token.json` hold secrets and are git-ignored. Never commit them.
