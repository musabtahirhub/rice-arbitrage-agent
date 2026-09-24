import asyncio
from contextlib import asynccontextmanager
import email.utils
import logging
import os
from pathlib import Path
import re
import threading
from typing import Optional
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.directory import get_all_counterparties, get_buyers_for_commodity, get_suppliers_for_commodity
from app.email_service import (
    check_latest_reply,
    initialize_unseen_snapshot,
    normalize_thread_subject,
    parse_email_draft,
    send_email,
)
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import calculate_dynamic_bounds
from app.models import Campaign, CreateCampaignRequest, DealState, ParsedEmail, SimulateTurnRequest
from app.workflow import generate_proactive_sco_draft, split_subject_and_body, trade_graph

from app.logger import setup_logger

logger = setup_logger("arbitrage_desk")

CAMPAIGN_LEDGER: dict[str, DealState] = {}


def _match_email_to_campaign(subject: str, body: str, sender: str) -> Optional[str]:
    if not CAMPAIGN_LEDGER:
        return None

    sub_low = subject.lower()
    body_low = body.lower()
    sender_low = sender.lower()

    for cid in CAMPAIGN_LEDGER.keys():
        if cid.lower() in sub_low or cid.lower() in body_low:
            return cid

    for cid, state in reversed(list(CAMPAIGN_LEDGER.items())):
        if state.get("deal_status") in ["closed", "rejected"]:
            continue
        for entry in state.get("audit_transcript", []):
            recip = entry.get("recipient", "").lower()
            if sender_low and any(part in recip for part in sender_low.replace("<", " ").replace(">", " ").split() if "@" in part):
                return cid

    for cid, state in reversed(list(CAMPAIGN_LEDGER.items())):
        if state.get("deal_status") in ["closed", "rejected"]:
            continue
        comm = state["campaign"].commodity.lower()
        if comm in sub_low or comm in body_low or any(w in sub_low for w in comm.split()):
            return cid

    for cid, state in reversed(list(CAMPAIGN_LEDGER.items())):
        if state.get("deal_status") not in ["closed", "rejected"]:
            return cid

    return list(CAMPAIGN_LEDGER.keys())[-1]


async def email_polling_worker():
    poll_interval = 15
    logger.info(f"[EMAIL WORKER] Initialized live email polling worker (interval: {poll_interval}s)")

    await asyncio.to_thread(initialize_unseen_snapshot)

    while True:
        try:
            allowed_senders: set[str] = set()
            if settings.my_test_email:
                allowed_senders.add(settings.my_test_email.strip().lower())
            for cid, cstate in CAMPAIGN_LEDGER.items():
                camp_obj = cstate.get("campaign")
                if camp_obj:
                    for b in get_buyers_for_commodity(camp_obj.commodity):
                        if b.contact_email:
                            allowed_senders.add(b.contact_email.strip().lower())

            incoming = await asyncio.to_thread(
                check_latest_reply,
                allowed_senders=allowed_senders or None,
            )
            if incoming:
                raw_body = str(incoming).strip()
                sub = getattr(incoming, "subject", "")
                sender = getattr(incoming, "sender", "")
                msg_id = getattr(incoming, "message_id", "")
                in_reply_to = getattr(incoming, "in_reply_to", "")
                references = getattr(incoming, "references", "")
                logger.info(f"[EMAIL WORKER] Inbound email detected from '{sender}' | Subject: '{sub}' | Message-ID: '{msg_id}'")

                matched_cid = _match_email_to_campaign(sub, raw_body, sender)
                if matched_cid and matched_cid in CAMPAIGN_LEDGER:
                    state = dict(CAMPAIGN_LEDGER[matched_cid])
                    if state.get("deal_status") in ["closed", "rejected"]:
                        logger.info(f"[EMAIL WORKER] Campaign {matched_cid} is already {state.get('deal_status')}. Skipping.")
                    else:
                        logger.info(f"[EMAIL WORKER] Processing inbound email for campaign: {matched_cid}")
                        state["latest_email"] = raw_body
                        state["active_role"] = "buyer"

                        if msg_id:
                            state["last_buyer_message_id"] = msg_id
                            existing_refs = state.get("buyer_references") or references or ""
                            if msg_id not in existing_refs:
                                state["buyer_references"] = f"{existing_refs} {msg_id}".strip()
                        if not state.get("thread_subject") and sub:
                            clean_sub = re.sub(r"^(?:re|fwd|fw):\s*", "", sub, flags=re.IGNORECASE).strip()
                            state["thread_subject"] = clean_sub

                        camp: Campaign = state["campaign"]
                        buyers = get_buyers_for_commodity(camp.commodity)
                        suppliers = get_suppliers_for_commodity(camp.commodity)
                        me_buyers = [b for b in buyers if b.country in ["UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain"]]
                        primary_buyer = me_buyers[0] if me_buyers else (buyers[0] if buyers else None)
                        target_supplier = suppliers[0] if suppliers else None

                        buyer_name = primary_buyer.name if primary_buyer else "Procurement Partner"
                        buyer_email = settings.my_test_email or (primary_buyer.contact_email if primary_buyer else "procurement@domain.com")
                        supp_name = target_supplier.name if target_supplier else "Supplier Partner"
                        supp_email = target_supplier.contact_email if target_supplier else "export@supplier.com"

                        if state.get("supplier_terms") is None:
                            benchmark_fob = state.get("benchmark_fob_usd") or get_benchmark_rate(camp.commodity)
                            logger.info(
                                f"[SUPPLIER AGENT] Securing allocation for campaign '{matched_cid}': "
                                f"{camp.target_volume_mt:,.0f} MT {camp.commodity} @ USD {benchmark_fob:.2f}/MT FOB {camp.origin_port_default}"
                            )
                            state["supplier_terms"] = ParsedEmail(
                                sender_role="supplier",
                                commodity=camp.commodity,
                                quantity_mt=camp.target_volume_mt,
                                price_usd_per_mt=benchmark_fob,
                                incoterm="FOB",
                                port=camp.origin_port_default,
                                payment_terms=settings.default_payment_terms,
                            )
                            supplier_quote_email = (
                                f"Subject: Quotation — {camp.commodity} FOB {camp.origin_port_default}\n\n"
                                f"To: Procurement Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
                                f"From: {supp_name} <{supp_email}>\n\n"
                                f"Dear Procurement Desk,\n\n"
                                f"In response to your RFQ, we quote {camp.target_volume_mt:,.0f} MT of export grade {camp.commodity} at "
                                f"USD {benchmark_fob:.2f}/MT FOB {camp.origin_port_default}. Payment terms: {settings.default_payment_terms}. Ready for prompt loading.\n\n"
                                f"Best regards,\nExport Sales, {supp_name}"
                            )
                            if "audit_transcript" not in state or state["audit_transcript"] is None:
                                state["audit_transcript"] = []
                            state["audit_transcript"].append({
                                "turn": state.get("negotiation_round", 0),
                                "sender": f"{supp_name} <{supp_email}>",
                                "recipient": f"Trading Desk ({settings.desk_name})",
                                "role": "supplier",
                                "action": "INCOMING_SUPPLIER_QUOTE",
                                "subject": f"Quotation — {camp.commodity} FOB {camp.origin_port_default}",
                                "message": supplier_quote_email,
                            })

                        updated_state = await asyncio.to_thread(trade_graph.invoke, state)

                        if "audit_transcript" not in updated_state or updated_state["audit_transcript"] is None:
                            updated_state["audit_transcript"] = []

                        curr_round = updated_state.get("negotiation_round", 1)
                        updated_state["audit_transcript"].append({
                            "turn": curr_round,
                            "sender": f"{buyer_name} <{sender or buyer_email}>",
                            "recipient": f"Trading Desk ({settings.desk_name})",
                            "role": "buyer",
                            "action": "INCOMING_COUNTER",
                            "subject": sub or f"Re: {camp.commodity} CIF {camp.destination_port}",
                            "message": raw_body,
                        })

                        action = updated_state.get("action")
                        deal_status = updated_state.get("deal_status")
                        buyer_msg = updated_state.get("buyer_draft")
                        supp_msg = updated_state.get("supplier_draft")

                        if supp_msg and (action in ["ACCEPT_AND_CLOSE", "LOCK_SUPPLIER_ALLOCATION"] or "Volume Lock" in supp_msg or not state.get("supplier_terms")):
                            supp_thread_sub = f"Urgent RFQ — {camp.commodity} FOB {camp.origin_port_default}"
                            await asyncio.to_thread(
                                send_email,
                                supp_email,
                                supp_msg,
                                in_reply_to=updated_state.get("last_supplier_message_id"),
                                references=updated_state.get("supplier_references"),
                                thread_subject=supp_thread_sub,
                            )
                            logger.info(
                                f"[OUTBOUND EMAIL] Dispatched supplier message to '{supp_email}' | "
                                f"Action: {'LOCK_SUPPLIER_ALLOCATION' if action == 'ACCEPT_AND_CLOSE' else 'SUPPLIER_RFQ'} | "
                                f"Subject: '{supp_thread_sub}'"
                            )
                            supp_sub, _ = split_subject_and_body(
                                supp_msg,
                                default_subject=f"Urgent RFQ / Volume Lock — {camp.commodity} FOB {camp.origin_port_default}",
                            )
                            updated_state["audit_transcript"].append({
                                "turn": curr_round,
                                "sender": f"Trading Desk ({settings.desk_name})",
                                "recipient": f"{supp_name} <{supp_email}>",
                                "role": "agent",
                                "action": "LOCK_SUPPLIER_ALLOCATION" if action == "ACCEPT_AND_CLOSE" else "SUPPLIER_RFQ",
                                "subject": supp_sub,
                                "message": supp_msg,
                            })

                        if buyer_msg:
                            default_sub = (
                                f"Soft Corporate Offer (SCO) Acceptance — {camp.commodity}"
                                if action == "ACCEPT_AND_CLOSE"
                                else (
                                    f"Commercial Proposal Status — {camp.commodity}"
                                    if action == "REJECT_HARD"
                                    else f"Counter-Offer — {camp.commodity} CIF {camp.destination_port}"
                                )
                            )
                            buyer_sub, _ = split_subject_and_body(buyer_msg, default_subject=default_sub)
                            thread_sub = updated_state.get("thread_subject") or buyer_sub

                            clean_desk_user = settings.email_user.strip()
                            desk_domain = clean_desk_user.split("@")[1] if "@" in clean_desk_user else "gmail.com"
                            response_msg_id = email.utils.make_msgid(domain=desk_domain)

                            await asyncio.to_thread(
                                send_email,
                                buyer_email,
                                buyer_msg,
                                in_reply_to=updated_state.get("last_buyer_message_id"),
                                references=updated_state.get("buyer_references"),
                                thread_subject=thread_sub,
                                custom_message_id=response_msg_id,
                            )

                            actual_thread_sub = normalize_thread_subject(buyer_sub, thread_sub)
                            logger.info(
                                f"[OUTBOUND EMAIL] Dispatched buyer response to '{buyer_email}' | "
                                f"Action: {action} | Subject: '{actual_thread_sub}' | Message-ID: '{response_msg_id}'"
                            )

                            updated_state["last_buyer_message_id"] = response_msg_id
                            cur_refs = updated_state.get("buyer_references") or ""
                            updated_state["buyer_references"] = f"{cur_refs} {response_msg_id}".strip()

                            actual_thread_sub = normalize_thread_subject(buyer_sub, thread_sub)
                            updated_state["audit_transcript"].append({
                                "turn": curr_round,
                                "sender": f"Trading Desk ({settings.desk_name})",
                                "recipient": f"{buyer_name} <{buyer_email}>",
                                "role": "agent",
                                "action": action or "COUNTER_BUYER",
                                "subject": actual_thread_sub,
                                "message": buyer_msg,
                            })

                        CAMPAIGN_LEDGER[matched_cid] = updated_state
                        logger.info(f"[EMAIL WORKER] Campaign {matched_cid} updated to status: {deal_status} (action: {action})")
                else:
                    logger.warning(f"[EMAIL WORKER] Unread email detected, but no matching active campaign found in ledger. Subject: '{sub}'")

        except asyncio.CancelledError:
            logger.info("[EMAIL WORKER] Email polling worker cancelled.")
            break
        except Exception as e:
            logger.error(f"[EMAIL WORKER ERROR] Unexpected error in polling cycle: {e}", exc_info=True)

        await asyncio.sleep(poll_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"[LIFESPAN] Starting {settings.desk_name} Arbitrage Desk v2.0.0...")
    worker_task = None
    if settings.email_user and settings.email_pass:
        logger.info(f"[LIFESPAN] Email credentials configured ({settings.email_user}). Starting background email polling worker...")
        worker_task = asyncio.create_task(email_polling_worker())
    else:
        logger.warning("[LIFESPAN] Email credentials not configured (EMAIL_USER/EMAIL_PASS). Live email polling worker disabled.")
    yield
    if worker_task:
        logger.info("[LIFESPAN] Stopping background email polling worker...")
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title=f"{settings.desk_name} - Physical Commodity Arbitrage",
    description="Educational Mid-Level Autonomous Physical Commodity Arbitrage Agent",
    version="2.0.0",
    lifespan=lifespan,
)

cors_list = settings.cors_origins if isinstance(settings.cors_origins, list) else [s.strip() for s in settings.cors_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if not STATIC_DIR.exists():
    STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/api/directory")
def list_directory():
    return get_all_counterparties()


@app.post("/api/campaigns")
def create_campaign(req: CreateCampaignRequest):
    campaign_id = f"CAMP-{uuid.uuid4().hex[:6].upper()}"
    logger.info(
        f"[CAMPAIGN CREATE] Received request for '{req.commodity}' ({req.target_volume_mt:,.0f} MT) "
        f"-> Port: {req.destination_port} | Target Margin: {req.target_margin_pct}% | Auto-run: {req.auto_run}"
    )
    campaign = Campaign(
        campaign_id=campaign_id,
        commodity=req.commodity,
        target_volume_mt=req.target_volume_mt,
        target_margin_pct=req.target_margin_pct,
        max_variance_from_benchmark_pct=req.max_variance_from_benchmark_pct,
        destination_port=req.destination_port,
        min_profit_per_mt_hard=req.min_profit_per_mt_hard,
        min_profit_per_mt_soft=req.min_profit_per_mt_soft,
        max_negotiation_rounds=req.max_negotiation_rounds,
    )

    initial_state: DealState = {
        "campaign": campaign,
        "negotiation_round": 0,
        "deal_status": "initiating",
        "action": None,
        "pipeline_step": 0,
        "is_deal_viable": False,
        "net_spread_usd": 0.0,
        "net_margin_pct": 0.0,
        "latest_email": "",
        "active_role": "buyer",
        "buyer_terms": None,
        "supplier_terms": None,
        "buyer_draft": "",
        "supplier_draft": "",
        "audit_transcript": [],
    }

    launched_state = trade_graph.invoke(initial_state)
    CAMPAIGN_LEDGER[campaign_id] = launched_state

    logger.info(
        f"[CAMPAIGN CREATED] ID='{campaign_id}' | Benchmark FOB: ${launched_state.get('benchmark_fob_usd', 0.0):.2f}/MT | "
        f"Freight: ${launched_state.get('freight_cost_usd', 0.0):.2f}/MT | Dynamic FOB Ceiling: ${launched_state.get('dynamic_fob_ceiling', 0.0):.2f}/MT | "
        f"Anchor CIF: ${launched_state.get('anchor_cif_usd', 0.0):.2f}/MT"
    )

    if req.auto_run:
        logger.info(f"[AUTONOMOUS CAMPAIGN] Auto-running campaign '{campaign_id}' through full negotiation lifecycle...")
        res = run_full_autonomous_campaign(campaign_id)
        logger.info(f"[AUTONOMOUS CAMPAIGN] Campaign '{campaign_id}' completed with status: {res.get('deal_status')}")
        return res

    return {
        "campaign_id": campaign_id,
        "commodity": campaign.commodity,
        "benchmark_fob_usd": launched_state.get("benchmark_fob_usd"),
        "freight_cost_usd": launched_state.get("freight_cost_usd"),
        "dynamic_fob_ceiling": launched_state.get("dynamic_fob_ceiling"),
        "dynamic_cif_floor": launched_state.get("dynamic_cif_floor"),
        "anchor_cif_usd": launched_state.get("anchor_cif_usd"),
        "buyer_draft": launched_state.get("buyer_draft"),
        "pipeline_step": launched_state.get("pipeline_step", 1),
        "deal_status": launched_state.get("deal_status", "prospecting"),
        "min_profit_per_mt_hard": campaign.min_profit_per_mt_hard,
        "min_profit_per_mt_soft": campaign.min_profit_per_mt_soft,
        "max_negotiation_rounds": campaign.max_negotiation_rounds,
        "discovered_buyers": launched_state.get("discovered_buyers", []),
        "discovered_suppliers": launched_state.get("discovered_suppliers", []),
        "audit_transcript": launched_state.get("audit_transcript", []),
        "status": "initialized",
    }


def run_full_autonomous_campaign(campaign_id: str) -> dict:
    state = CAMPAIGN_LEDGER.get(campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    campaign: Campaign = state["campaign"]
    commodity = campaign.commodity
    volume = campaign.target_volume_mt
    dest_port = campaign.destination_port
    origin_port = campaign.origin_port_default
    buffer_usd = campaign.buffer_usd_per_mt if campaign.buffer_usd_per_mt is not None else settings.default_buffer_usd_per_mt

    buyers = get_buyers_for_commodity(commodity)
    suppliers = get_suppliers_for_commodity(commodity)
    me_buyers = [b for b in buyers if b.country in ["UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain"]]
    primary_buyer = me_buyers[0] if me_buyers else (buyers[0] if buyers else None)
    target_supplier = suppliers[0] if suppliers else None

    buyer_name = primary_buyer.name if primary_buyer else "Procurement Partner"
    buyer_email = primary_buyer.contact_email if primary_buyer else "procurement@domain.com"
    supp_name = target_supplier.name if target_supplier else "Asian Rice Exporters"
    supp_email = target_supplier.contact_email if target_supplier else "export@supplier.com"

    benchmark_fob = state["benchmark_fob_usd"]
    freight = state["freight_cost_usd"]
    landed_cost_baseline = round(benchmark_fob + freight + buffer_usd, 2)

    if "audit_transcript" not in state or not state["audit_transcript"]:
        state["audit_transcript"] = [{
            "turn": 0,
            "sender": f"Trading Desk ({settings.desk_name})",
            "recipient": f"{buyer_name} <{buyer_email}>",
            "role": "agent",
            "action": "OUTBOUND_SCO",
            "subject": f"Soft Corporate Offer (SCO) — {commodity} CIF {dest_port}",
            "message": state.get("buyer_draft", ""),
        }]

    initial_buyer_cif = round(landed_cost_baseline + campaign.min_profit_per_mt_hard + 30.0, 2)
    buyer_interest_email = (
        f"Subject: Re: Soft Corporate Offer (SCO) — {commodity} CIF {dest_port}\n\n"
        f"To: Trading Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
        f"From: {buyer_name} <{buyer_email}>\n\n"
        f"Dear Trading Desk,\n\n"
        f"We acknowledge receipt of your SCO. We are interested in contracting {volume:,.0f} MT of {commodity} for {dest_port}. "
        f"However, your indicative pitch is above our procurement budget. "
        f"We submit a firm counter-bid of USD {initial_buyer_cif:.2f}/MT CIF {dest_port}. Payment via 100% LC at sight.\n\n"
        f"Best regards,\nProcurement Team, {buyer_name}"
    )

    state["latest_email"] = buyer_interest_email
    state["active_role"] = "buyer"
    state = trade_graph.invoke(state)

    state["audit_transcript"].append({
        "turn": 1,
        "sender": f"{buyer_name} <{buyer_email}>",
        "recipient": f"Trading Desk ({settings.desk_name})",
        "role": "buyer",
        "action": "INBOUND_BID",
        "subject": f"Re: SCO — {commodity} CIF {dest_port}",
        "message": buyer_interest_email,
    })

    rfq_msg = state.get("supplier_draft", "")
    state["audit_transcript"].append({
        "turn": 1,
        "sender": f"Procurement Desk ({settings.desk_name})",
        "recipient": f"{supp_name} <{supp_email}>",
        "role": "agent",
        "action": "OUTBOUND_RFQ",
        "subject": f"Urgent RFQ — {commodity} FOB {origin_port}",
        "message": rfq_msg,
    })

    supplier_fob = benchmark_fob
    supplier_quote_email = (
        f"Subject: Quotation — {commodity} FOB {origin_port}\n\n"
        f"To: Procurement Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
        f"From: {supp_name} <{supp_email}>\n\n"
        f"Dear Procurement Desk,\n\n"
        f"In response to your RFQ, we quote {volume:,.0f} MT of export grade {commodity} at "
        f"USD {supplier_fob:.2f}/MT FOB {origin_port}. Payment terms: 100% LC at sight. Ready for prompt loading.\n\n"
        f"Best regards,\nExport Sales, {supp_name}"
    )

    state["latest_email"] = supplier_quote_email
    state["active_role"] = "supplier"
    state = trade_graph.invoke(state)

    curr_round = state.get("negotiation_round", 2)
    state["audit_transcript"].append({
        "turn": curr_round,
        "sender": f"{supp_name} <{supp_email}>",
        "recipient": f"Procurement Desk ({settings.desk_name})",
        "role": "supplier",
        "action": "INBOUND_QUOTE",
        "subject": f"Quotation — {commodity} FOB {origin_port}",
        "message": supplier_quote_email,
    })

    while state.get("deal_status") not in ["closed", "rejected"]:
        action = state.get("action")
        if action == "ACCEPT_AND_CLOSE":
            break
        elif action == "REJECT_HARD":
            break
        elif action == "COUNTER_TO_MAXIMIZE":
            counter_draft = state.get("buyer_draft") if state.get("active_role") == "buyer" else state.get("supplier_draft")
            state["audit_transcript"].append({
                "turn": state.get("negotiation_round", curr_round),
                "sender": f"Trading Desk ({settings.desk_name})",
                "recipient": f"{buyer_name} <{buyer_email}>",
                "role": "agent",
                "action": "OUTBOUND_COUNTER",
                "subject": f"Negotiation Round {state.get('negotiation_round')}: Tactical Adjustment Request",
                "message": counter_draft or f"Tactical counter to maximize margin: {state.get('evaluation_reason')}",
            })

            target_buyer_cif = round(landed_cost_baseline + campaign.min_profit_per_mt_soft, 2)
            concession_email = (
                f"Subject: Re: Price Adjustment Request — Concession Agreement\n\n"
                f"To: Trading Desk <trading@{settings.desk_name.lower().replace(' ', '')}.com>\n"
                f"From: {buyer_name} <{buyer_email}>\n\n"
                f"Dear Trading Desk,\n\n"
                f"Following your counter-proposal and updated corridor freight analysis, we agree to revise our CIF bid "
                f"to USD {target_buyer_cif:.2f}/MT CIF {dest_port} for {volume:,.0f} MT.\n\n"
                f"Please confirm allocation lock and issue the final SCO acceptance.\n\n"
                f"Best regards,\nProcurement Team, {buyer_name}"
            )

            state["latest_email"] = concession_email
            state["active_role"] = "buyer"
            state = trade_graph.invoke(state)

            curr_round = state.get("negotiation_round", curr_round + 1)
            state["audit_transcript"].append({
                "turn": curr_round,
                "sender": f"{buyer_name} <{buyer_email}>",
                "recipient": f"Trading Desk ({settings.desk_name})",
                "role": "buyer",
                "action": "INBOUND_CONCESSION",
                "subject": f"Re: Price Adjustment Request — Concession Agreement",
                "message": concession_email,
            })
        else:
            break

    if state.get("action") == "ACCEPT_AND_CLOSE" or state.get("deal_status") == "closed":
        state["audit_transcript"].append({
            "turn": state.get("negotiation_round", 3),
            "sender": f"Procurement Desk ({settings.desk_name})",
            "recipient": f"{supp_name} <{supp_email}>",
            "role": "agent",
            "action": "LOCK_SUPPLIER_ALLOCATION",
            "subject": f"Deal Confirmation & Volume Lock — {commodity}",
            "message": state.get("supplier_draft", ""),
        })
        state["audit_transcript"].append({
            "turn": state.get("negotiation_round", 3),
            "sender": f"Trading Desk ({settings.desk_name})",
            "recipient": f"{buyer_name} <{buyer_email}>",
            "role": "agent",
            "action": "ACCEPT_AND_CLOSE",
            "subject": f"Soft Corporate Offer (SCO) Acceptance — {commodity}",
            "message": state.get("buyer_draft", ""),
        })

    CAMPAIGN_LEDGER[campaign_id] = state

    buyer_t = state.get("buyer_terms")
    supp_t = state.get("supplier_terms")

    return {
        "campaign_id": campaign_id,
        "commodity": campaign.commodity,
        "deal_status": state.get("deal_status", "closed"),
        "negotiation_rounds_completed": state.get("negotiation_round", 0),
        "final_net_spread_usd": state.get("net_spread_usd", 0.0),
        "final_net_margin_pct": state.get("net_margin_pct", 0.0),
        "action": state.get("action", "ACCEPT_AND_CLOSE"),
        "is_deal_viable": state.get("is_deal_viable", True),
        "pipeline_step": state.get("pipeline_step", 4),
        "evaluation_reason": state.get("evaluation_reason", ""),
        "audit_transcript": state.get("audit_transcript", []),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": state.get("buyer_draft"),
        "supplier_draft": state.get("supplier_draft"),
        "benchmark_fob_usd": state.get("benchmark_fob_usd"),
        "freight_cost_usd": state.get("freight_cost_usd"),
        "dynamic_fob_ceiling": state.get("dynamic_fob_ceiling"),
        "dynamic_cif_floor": state.get("dynamic_cif_floor"),
        "anchor_cif_usd": state.get("anchor_cif_usd"),
        "discovered_buyers": [b.model_dump() for b in buyers],
        "discovered_suppliers": [s.model_dump() for s in suppliers],
        "status": state.get("deal_status", "closed"),
    }


@app.post("/api/negotiate")
def negotiate_turn(req: SimulateTurnRequest):
    logger.info(f"[API NEGOTIATE] Inbound turn for campaign '{req.campaign_id}' from role='{req.sender_role}'")
    state = CAMPAIGN_LEDGER.get(req.campaign_id)
    if not state:
        logger.warning(f"[API NEGOTIATE] Campaign '{req.campaign_id}' not found in ledger.")
        raise HTTPException(status_code=404, detail=f"Campaign {req.campaign_id} not found.")

    turn_state = dict(state)
    turn_state["latest_email"] = req.raw_email
    turn_state["active_role"] = req.sender_role

    updated_state = trade_graph.invoke(turn_state)

    CAMPAIGN_LEDGER[req.campaign_id] = updated_state
    logger.info(
        f"[API NEGOTIATE] Turn completed for '{req.campaign_id}' | Round {updated_state.get('negotiation_round')} | "
        f"Action: {updated_state.get('action')} | Status: {updated_state.get('deal_status')} | "
        f"Spread: ${updated_state.get('net_spread_usd', 0.0):.2f}/MT | Margin: {updated_state.get('net_margin_pct', 0.0):.2f}%"
    )

    buyer_t = updated_state.get("buyer_terms")
    supp_t = updated_state.get("supplier_terms")

    return {
        "campaign_id": req.campaign_id,
        "negotiation_round": updated_state.get("negotiation_round", 1),
        "deal_status": updated_state.get("deal_status"),
        "action": updated_state.get("action"),
        "pipeline_step": updated_state.get("pipeline_step", 1),
        "is_deal_viable": updated_state.get("is_deal_viable"),
        "net_spread_usd": updated_state.get("net_spread_usd", 0.0),
        "net_margin_pct": updated_state.get("net_margin_pct"),
        "evaluation_reason": updated_state.get("evaluation_reason"),
        "target_fob_ceiling": updated_state.get("target_fob_ceiling", 0.0),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": updated_state.get("buyer_draft"),
        "supplier_draft": updated_state.get("supplier_draft"),
        "audit_transcript": updated_state.get("audit_transcript", []),
    }


@app.get("/api/campaigns/{campaign_id}")
def get_campaign_status(campaign_id: str):
    state = CAMPAIGN_LEDGER.get(campaign_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    buyer_t = state.get("buyer_terms")
    supp_t = state.get("supplier_terms")

    return {
        "campaign_id": campaign_id,
        "commodity": state["campaign"].commodity,
        "target_margin_pct": state["campaign"].target_margin_pct,
        "min_profit_per_mt_hard": state["campaign"].min_profit_per_mt_hard,
        "min_profit_per_mt_soft": state["campaign"].min_profit_per_mt_soft,
        "max_negotiation_rounds": state["campaign"].max_negotiation_rounds,
        "negotiation_round": state.get("negotiation_round", 0),
        "negotiation_rounds_completed": state.get("negotiation_round", 0),
        "deal_status": state.get("deal_status"),
        "action": state.get("action"),
        "pipeline_step": state.get("pipeline_step", 1),
        "is_deal_viable": state.get("is_deal_viable"),
        "net_spread_usd": state.get("net_spread_usd", 0.0),
        "final_net_spread_usd": state.get("net_spread_usd", 0.0),
        "net_margin_pct": state.get("net_margin_pct"),
        "final_net_margin_pct": state.get("net_margin_pct"),
        "evaluation_reason": state.get("evaluation_reason"),
        "dynamic_fob_ceiling": state.get("dynamic_fob_ceiling"),
        "dynamic_cif_floor": state.get("dynamic_cif_floor"),
        "target_fob_ceiling": state.get("target_fob_ceiling", 0.0),
        "anchor_cif_usd": state.get("anchor_cif_usd", 0.0),
        "benchmark_fob_usd": state.get("benchmark_fob_usd"),
        "freight_cost_usd": state.get("freight_cost_usd"),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": state.get("buyer_draft"),
        "supplier_draft": state.get("supplier_draft"),
        "audit_transcript": state.get("audit_transcript", []),
    }


@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Commodity Arbitrage API is running. Visit /docs for Swagger documentation."}
