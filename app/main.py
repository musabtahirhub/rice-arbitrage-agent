import asyncio
import base64
from contextlib import asynccontextmanager
import email.utils
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Optional
import uuid
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db, init_db
from app.db_models import CampaignModel, TradeAuditModel
from app.directory import get_all_counterparties, get_buyers_for_commodity, get_suppliers_for_commodity
from app.email_service import (
    check_latest_reply,
    initialize_unseen_snapshot,
    is_automated_or_bounce_message,
    normalize_thread_subject,
    parse_email_draft,
    send_email,
)
from app.gmail_client import (
    fetch_latest_messages_by_history,
    send_gmail_reply,
    setup_gmail_watch,
)
from app.market import get_benchmark_rate, estimate_freight
from app.math_engine import calculate_dynamic_bounds
from app.models import Campaign, CreateCampaignRequest, DealState, ParsedEmail, SimulateTurnRequest
from app.workflow import generate_proactive_sco_draft, setup_checkpointer, split_subject_and_body, trade_graph

from app.logger import setup_logger

logger = setup_logger("arbitrage_desk")

LAST_SEEN_HISTORY_ID: Optional[str] = None
GMAIL_EVENT_LOCK = asyncio.Lock()


def _match_email_to_campaign(subject: str, body: str, sender: str, db: Optional[Session] = None) -> Optional[str]:
    sub_low = subject.lower()
    body_low = body.lower()
    sender_low = sender.lower()

    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True

    try:
        active_campaigns = (
            db.query(CampaignModel)
            .filter(~CampaignModel.deal_status.in_(["closed", "rejected"]))
            .order_by(CampaignModel.created_at.desc())
            .all()
        )
        if not active_campaigns:
            all_campaigns = db.query(CampaignModel).order_by(CampaignModel.created_at.desc()).all()
            if not all_campaigns:
                return None
            for camp in all_campaigns:
                if camp.id.lower() in sub_low or camp.id.lower() in body_low:
                    return camp.id
            return all_campaigns[0].id

        # 1. Direct match by ID in subject or body
        for camp in active_campaigns:
            if camp.id.lower() in sub_low or camp.id.lower() in body_low:
                return camp.id

        # 2. Match counterparty email in historical audit entries
        for camp in active_campaigns:
            audits = db.query(TradeAuditModel).filter(TradeAuditModel.campaign_id == camp.id).all()
            for audit in audits:
                if sender_low and any(
                    part in audit.raw_message.lower()
                    for part in sender_low.replace("<", " ").replace(">", " ").split()
                    if "@" in part
                ):
                    return camp.id

        # 3. Match commodity name
        for camp in active_campaigns:
            comm = camp.commodity.lower()
            if comm in sub_low or comm in body_low or any(w in sub_low for w in comm.split()):
                return camp.id

        # 4. Fallback to latest active campaign
        return active_campaigns[0].id
    finally:
        if should_close:
            db.close()


async def email_polling_worker():
    poll_interval = 15
    logger.info(f"[EMAIL WORKER] Initialized live email polling worker (interval: {poll_interval}s)")

    await asyncio.to_thread(initialize_unseen_snapshot)

    while True:
        try:
            allowed_senders: set[str] = set()
            if settings.my_test_email:
                allowed_senders.add(settings.my_test_email.strip().lower())

            with SessionLocal() as db:
                active_campaigns = (
                    db.query(CampaignModel)
                    .filter(~CampaignModel.deal_status.in_(["closed", "rejected"]))
                    .all()
                )
                for camp_obj in active_campaigns:
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

                with SessionLocal() as db:
                    matched_cid = _match_email_to_campaign(sub, raw_body, sender, db=db)
                    db_camp = db.query(CampaignModel).filter(CampaignModel.id == matched_cid).first() if matched_cid else None
                    if db_camp:
                        if db_camp.deal_status in ["closed", "rejected"]:
                            logger.info(f"[EMAIL WORKER] Campaign {matched_cid} is already {db_camp.deal_status}. Skipping.")
                        else:
                            logger.info(f"[EMAIL WORKER] Processing inbound email for campaign: {matched_cid}")
                            config = {"configurable": {"thread_id": db_camp.id}}
                            snapshot = trade_graph.get_state(config)
                            state = dict(snapshot.values) if snapshot and snapshot.values else {}
                            if "campaign" not in state:
                                state["campaign"] = Campaign(
                                    campaign_id=db_camp.id,
                                    commodity=db_camp.commodity,
                                    target_volume_mt=db_camp.target_volume_mt,
                                    destination_port=db_camp.destination_port,
                                    origin_port_default=db_camp.origin_port_default,
                                )

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
                                supp_audit = TradeAuditModel(
                                    campaign_id=db_camp.id,
                                    thread_id=db_camp.id,
                                    role="supplier",
                                    counterparty_price=benchmark_fob,
                                    net_spread=None,
                                    raw_message=supplier_quote_email,
                                    direction="INBOUND",
                                )
                                db.add(supp_audit)

                            updated_state = await asyncio.to_thread(trade_graph.invoke, state, config)

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

                            # Persist inbound email into TradeAuditModel
                            buyer_t = updated_state.get("buyer_terms")
                            inbound_price = buyer_t.price_usd_per_mt if buyer_t else None
                            inbound_audit = TradeAuditModel(
                                campaign_id=db_camp.id,
                                thread_id=db_camp.id,
                                role="buyer",
                                counterparty_price=inbound_price,
                                net_spread=updated_state.get("net_spread_usd"),
                                raw_message=raw_body,
                                direction="INBOUND",
                            )
                            db.add(inbound_audit)

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
                                supp_out_audit = TradeAuditModel(
                                    campaign_id=db_camp.id,
                                    thread_id=db_camp.id,
                                    role="agent",
                                    counterparty_price=state.get("supplier_terms").price_usd_per_mt if state.get("supplier_terms") else None,
                                    net_spread=updated_state.get("net_spread_usd"),
                                    raw_message=supp_msg,
                                    direction="OUTBOUND",
                                )
                                db.add(supp_out_audit)

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

                                updated_state["audit_transcript"].append({
                                    "turn": curr_round,
                                    "sender": f"Trading Desk ({settings.desk_name})",
                                    "recipient": f"{buyer_name} <{buyer_email}>",
                                    "role": "agent",
                                    "action": action or "COUNTER_BUYER",
                                    "subject": actual_thread_sub,
                                    "message": buyer_msg,
                                })
                                buyer_out_audit = TradeAuditModel(
                                    campaign_id=db_camp.id,
                                    thread_id=db_camp.id,
                                    role="agent",
                                    counterparty_price=updated_state.get("anchor_cif_usd"),
                                    net_spread=updated_state.get("net_spread_usd"),
                                    raw_message=buyer_msg,
                                    direction="OUTBOUND",
                                )
                                db.add(buyer_out_audit)

                            db_camp.deal_status = deal_status or db_camp.deal_status
                            db.commit()
                            logger.info(f"[EMAIL WORKER] Campaign {matched_cid} updated to status: {deal_status} (action: {action})")
                    else:
                        logger.warning(f"[EMAIL WORKER] Unread email detected, but no matching active campaign found in database. Subject: '{sub}'")

        except asyncio.CancelledError:
            logger.info("[EMAIL WORKER] Email polling worker cancelled.")
            break
        except Exception as e:
            logger.error(f"[EMAIL WORKER ERROR] Unexpected error in polling cycle: {e}", exc_info=True)

        await asyncio.sleep(poll_interval)


async def process_incoming_gmail_event(history_id: str):
    """
    Background worker to process incoming Gmail Pub/Sub events:
    a) Retrieve newly added messages via fetch_latest_messages_by_history using LAST_SEEN_HISTORY_ID.
       Falls back to direct inbox message listing if history is empty, expired, or startHistoryId matches.
    b) Query active campaigns from PostgreSQL (CampaignModel).
    c) Inject incoming message and headers (last_buyer_message_id, buyer_references, thread_subject) into DealState.
    d) Invoke LangGraph: trade_graph.invoke(input_state, config={"configurable": {"thread_id": campaign.id}}).
    e) If an outbound response is drafted, send it via send_gmail_reply(...) and persist an audit row into TradeAuditModel.
    """
    async with GMAIL_EVENT_LOCK:
        global LAST_SEEN_HISTORY_ID
        start_history_id = LAST_SEEN_HISTORY_ID
        new_history_id = str(history_id) if history_id else ""
        logger.info(
            f"[GMAIL EVENT] Processing incoming Gmail event: new_history_id='{new_history_id}', "
            f"LAST_SEEN_HISTORY_ID='{start_history_id}'"
        )

        query_id = start_history_id if (start_history_id and start_history_id != new_history_id) else (new_history_id if not start_history_id else None)
        try:
            messages = await asyncio.to_thread(fetch_latest_messages_by_history, query_id or "")
        except Exception as e:
            logger.error(f"[GMAIL EVENT ERROR] Failed fetching messages for history_id {history_id}: {e}", exc_info=True)
            return

        if new_history_id:
            LAST_SEEN_HISTORY_ID = new_history_id

        if not messages:
            logger.info(f"[GMAIL EVENT] No new messages returned for history_id='{history_id}'")
            return

    desk_emails = {
        e.strip().lower()
        for e in [settings.email_user, settings.desk_email, "musabtahir2@gmail.com"]
        if e
    }

    for msg_data in messages:
        sender = msg_data.get("From") or msg_data.get("from") or msg_data.get("sender") or ""
        sender_lower = sender.strip().lower()

        # Step a: Ignore messages sent by the desk's own email
        if any(desk_e in sender_lower for desk_e in desk_emails):
            logger.info(f"[GMAIL EVENT] Ignoring message from desk's own email: {sender}")
            continue

        sub = msg_data.get("Subject") or msg_data.get("subject") or ""
        if is_automated_or_bounce_message(sender, sub):
            logger.info(f"[GMAIL EVENT] Ignoring automated/bounce message from {sender} | Subject: '{sub}'")
            continue

        raw_body = msg_data.get("body") or ""
        msg_id = msg_data.get("Message-ID") or msg_data.get("message_id") or ""
        in_reply_to = msg_data.get("In-Reply-To") or msg_data.get("in_reply_to") or ""
        references = msg_data.get("References") or msg_data.get("references") or ""
        thread_id = msg_data.get("threadId") or msg_data.get("thread_id") or ""

        logger.info(f"[GMAIL EVENT] Inbound email detected from '{sender}' | Subject: '{sub}' | Message-ID: '{msg_id}'")

        # Step b: Query active campaigns from PostgreSQL (CampaignModel)
        with SessionLocal() as db:
            active_campaigns = (
                db.query(CampaignModel)
                .filter(~CampaignModel.deal_status.in_(["closed", "rejected"]))
                .order_by(CampaignModel.created_at.desc())
                .all()
            )
            if not active_campaigns:
                logger.warning(f"[GMAIL EVENT] No active campaigns found for inbound email. Subject: '{sub}'")
                continue

            matched_cid = _match_email_to_campaign(sub, raw_body, sender, db=db)
            db_camp = db.query(CampaignModel).filter(CampaignModel.id == matched_cid).first() if matched_cid else None
            if not db_camp:
                logger.warning(f"[GMAIL EVENT] Inbound email detected, but no matching active campaign found in database. Subject: '{sub}'")
                continue

            if db_camp.deal_status in ["closed", "rejected"]:
                logger.info(f"[GMAIL EVENT] Campaign {matched_cid} is already {db_camp.deal_status}. Skipping.")
                continue

            logger.info(f"[GMAIL EVENT] Processing inbound email for campaign: {matched_cid}")

            # Step c: Inject incoming message and headers (last_buyer_message_id, buyer_references, thread_subject) into DealState
            config = {"configurable": {"thread_id": db_camp.id}}
            snapshot = trade_graph.get_state(config)
            state = dict(snapshot.values) if snapshot and snapshot.values else {}
            if "campaign" not in state:
                state["campaign"] = Campaign(
                    campaign_id=db_camp.id,
                    commodity=db_camp.commodity,
                    target_volume_mt=db_camp.target_volume_mt,
                    destination_port=db_camp.destination_port,
                    origin_port_default=db_camp.origin_port_default,
                )

            state["latest_email"] = raw_body
            state["active_role"] = "buyer"

            if msg_id:
                state["last_buyer_message_id"] = msg_id
                existing_refs = state.get("buyer_references") or references or ""
                if msg_id not in existing_refs:
                    state["buyer_references"] = f"{existing_refs} {msg_id}".strip()
            elif references and not state.get("buyer_references"):
                state["buyer_references"] = references

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
                supp_audit = TradeAuditModel(
                    campaign_id=db_camp.id,
                    thread_id=db_camp.id,
                    role="supplier",
                    counterparty_price=benchmark_fob,
                    net_spread=None,
                    raw_message=supplier_quote_email,
                    direction="INBOUND",
                )
                db.add(supp_audit)

            # Step d: Invoke LangGraph
            updated_state = await asyncio.to_thread(
                trade_graph.invoke,
                state,
                config={"configurable": {"thread_id": db_camp.id}},
            )

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

            # Inbound audit record
            buyer_t = updated_state.get("buyer_terms")
            inbound_price = buyer_t.price_usd_per_mt if buyer_t else None
            inbound_audit = TradeAuditModel(
                campaign_id=db_camp.id,
                thread_id=db_camp.id,
                role="buyer",
                counterparty_price=inbound_price,
                net_spread=updated_state.get("net_spread_usd"),
                raw_message=raw_body,
                direction="INBOUND",
            )
            db.add(inbound_audit)

            action = updated_state.get("action")
            deal_status = updated_state.get("deal_status")
            buyer_msg = updated_state.get("buyer_draft")
            supp_msg = updated_state.get("supplier_draft")

            if supp_msg and (action in ["ACCEPT_AND_CLOSE", "LOCK_SUPPLIER_ALLOCATION"] or "Volume Lock" in supp_msg or not state.get("supplier_terms")):
                supp_thread_sub = f"Urgent RFQ — {camp.commodity} FOB {camp.origin_port_default}"
                await asyncio.to_thread(
                    send_gmail_reply,
                    supp_email,
                    supp_msg,
                    in_reply_to=updated_state.get("last_supplier_message_id"),
                    references=updated_state.get("supplier_references"),
                    thread_subject=supp_thread_sub,
                    thread_id=thread_id or None,
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
                supp_out_audit = TradeAuditModel(
                    campaign_id=db_camp.id,
                    thread_id=db_camp.id,
                    role="agent",
                    counterparty_price=state.get("supplier_terms").price_usd_per_mt if state.get("supplier_terms") else None,
                    net_spread=updated_state.get("net_spread_usd"),
                    raw_message=supp_msg,
                    direction="OUTBOUND",
                )
                db.add(supp_out_audit)

            # Step e: If an outbound response is drafted, send it via send_gmail_reply(...) and persist an audit row into TradeAuditModel
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

                await asyncio.to_thread(
                    send_gmail_reply,
                    buyer_email,
                    buyer_msg,
                    in_reply_to=updated_state.get("last_buyer_message_id"),
                    references=updated_state.get("buyer_references"),
                    thread_subject=thread_sub,
                    thread_id=thread_id or None,
                )

                actual_thread_sub = normalize_thread_subject(buyer_sub, thread_sub)
                logger.info(
                    f"[GMAIL EVENT] Dispatched buyer response to '{buyer_email}' via Gmail REST API | "
                    f"Action: {action} | Subject: '{actual_thread_sub}'"
                )

                updated_state["audit_transcript"].append({
                    "turn": curr_round,
                    "sender": f"Trading Desk ({settings.desk_name})",
                    "recipient": f"{buyer_name} <{buyer_email}>",
                    "role": "agent",
                    "action": action or "COUNTER_BUYER",
                    "subject": actual_thread_sub,
                    "message": buyer_msg,
                })
                buyer_out_audit = TradeAuditModel(
                    campaign_id=db_camp.id,
                    thread_id=db_camp.id,
                    role="agent",
                    counterparty_price=updated_state.get("anchor_cif_usd"),
                    net_spread=updated_state.get("net_spread_usd"),
                    raw_message=buyer_msg,
                    direction="OUTBOUND",
                )
                db.add(buyer_out_audit)

            db_camp.deal_status = deal_status or db_camp.deal_status
            db.commit()
            logger.info(f"[GMAIL EVENT] Campaign {matched_cid} updated to status: {deal_status} (action: {action})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. DB & Checkpointer init
    init_db()
    setup_checkpointer()
    worker_task = None

    # 2. Setup Push Webhook Watch or Polling Worker
    if settings.use_push_webhooks:
        topic = settings.google_pubsub_topic
        if topic:
            logger.info(f"[LIFESPAN] Registering Gmail watch for topic: {topic}")
            try:
                watch_result = await asyncio.to_thread(setup_gmail_watch, topic)
                if watch_result:
                    global LAST_SEEN_HISTORY_ID
                    if "historyId" in watch_result:
                        LAST_SEEN_HISTORY_ID = str(watch_result["historyId"])
                    logger.info(
                        f"[LIFESPAN] Gmail watch active! Expiration: {watch_result.get('expiration')}, "
                        f"initial historyId: {LAST_SEEN_HISTORY_ID}"
                    )
                else:
                    logger.error("[LIFESPAN ERROR] Failed to register Gmail watch.")
            except Exception as e:
                logger.error(f"[LIFESPAN] Failed to set up Gmail watch on boot: {e}")
        else:
            logger.info("[LIFESPAN] Push webhooks enabled (USE_PUSH_WEBHOOKS=True). Polling worker disabled.")
    else:
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


@app.post("/api/webhooks/gmail")
async def gmail_webhook_endpoint(request: Request, background_tasks: BackgroundTasks):
    """
    Real-time Google Cloud Pub/Sub push notification endpoint for Gmail watch.
    Reads JSON payload: {"message": {"data": "<base64_encoded>"}}, acknowledges Pub/Sub with 200 immediately,
    and offloads processing to process_incoming_gmail_event(history_id).
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"[GMAIL WEBHOOK] Invalid JSON received: {e}")
        return {"status": "error", "message": "Invalid JSON"}

    message = body.get("message") if isinstance(body, dict) else {}
    if not isinstance(message, dict):
        message = {}

    data_b64 = message.get("data", "")
    history_id = ""

    if data_b64:
        try:
            missing_padding = len(data_b64) % 4
            if missing_padding:
                data_b64 += "=" * (4 - missing_padding)
            decoded_bytes = base64.b64decode(data_b64)
            decoded_str = decoded_bytes.decode("utf-8")
            try:
                data_json = json.loads(decoded_str)
                history_id = str(data_json.get("historyId", ""))
            except Exception:
                history_id = decoded_str.strip()
        except Exception as e:
            logger.error(f"[GMAIL WEBHOOK] Error decoding Pub/Sub data: {e}")

    logger.info(f"[GMAIL WEBHOOK] Received Pub/Sub webhook notification. historyId: '{history_id}'")

    if history_id:
        background_tasks.add_task(process_incoming_gmail_event, history_id)

    return {"status": "ok", "historyId": history_id}


@app.get("/api/directory")
def list_directory():
    return get_all_counterparties()


@app.post("/api/campaigns")
def create_campaign(req: CreateCampaignRequest, db: Session = Depends(get_db)):
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

    # Persist the new campaign row into CampaignModel via a SQLAlchemy session
    db_campaign = CampaignModel(
        id=campaign_id,
        commodity=req.commodity,
        target_volume_mt=req.target_volume_mt,
        destination_port=req.destination_port,
        origin_port_default=settings.default_origin_port,
        deal_status="initiating",
        anchor_cif_usd=None,
    )
    db.add(db_campaign)
    db.commit()
    db.refresh(db_campaign)

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

    config = {"configurable": {"thread_id": campaign.campaign_id}}
    launched_state = trade_graph.invoke(initial_state, config=config)

    # Update deal status and anchor_cif_usd
    db_campaign.deal_status = launched_state.get("deal_status", "prospecting")
    db_campaign.anchor_cif_usd = launched_state.get("anchor_cif_usd")

    # Persist initial outbound SCO draft to TradeAuditModel
    if launched_state.get("buyer_draft"):
        sco_audit = TradeAuditModel(
            campaign_id=campaign_id,
            thread_id=campaign_id,
            role="agent",
            counterparty_price=launched_state.get("anchor_cif_usd"),
            net_spread=0.0,
            raw_message=launched_state.get("buyer_draft"),
            direction="OUTBOUND",
        )
        db.add(sco_audit)
    db.commit()

    logger.info(
        f"[CAMPAIGN CREATED] ID='{campaign_id}' | Benchmark FOB: ${launched_state.get('benchmark_fob_usd', 0.0):.2f}/MT | "
        f"Freight: ${launched_state.get('freight_cost_usd', 0.0):.2f}/MT | Dynamic FOB Ceiling: ${launched_state.get('dynamic_fob_ceiling', 0.0):.2f}/MT | "
        f"Anchor CIF: ${launched_state.get('anchor_cif_usd', 0.0):.2f}/MT"
    )

    if req.auto_run:
        logger.info(f"[AUTONOMOUS CAMPAIGN] Auto-running campaign '{campaign_id}' through full negotiation lifecycle...")
        res = run_full_autonomous_campaign(campaign_id, db=db)
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


def run_full_autonomous_campaign(campaign_id: str, db: Optional[Session] = None) -> dict:
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True

    try:
        db_camp = db.query(CampaignModel).filter(CampaignModel.id == campaign_id).first()
        if not db_camp:
            raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

        config = {"configurable": {"thread_id": campaign_id}}
        snapshot = trade_graph.get_state(config)
        state = dict(snapshot.values) if snapshot and snapshot.values else {}
        if not state:
            raise HTTPException(status_code=404, detail=f"State for campaign {campaign_id} not found.")

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
        state = trade_graph.invoke(state, config=config)

        state["audit_transcript"].append({
            "turn": 1,
            "sender": f"{buyer_name} <{buyer_email}>",
            "recipient": f"Trading Desk ({settings.desk_name})",
            "role": "buyer",
            "action": "INBOUND_BID",
            "subject": f"Re: SCO — {commodity} CIF {dest_port}",
            "message": buyer_interest_email,
        })
        db.add(TradeAuditModel(
            campaign_id=campaign_id,
            thread_id=campaign_id,
            role="buyer",
            counterparty_price=initial_buyer_cif,
            net_spread=state.get("net_spread_usd"),
            raw_message=buyer_interest_email,
            direction="INBOUND",
        ))

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
        db.add(TradeAuditModel(
            campaign_id=campaign_id,
            thread_id=campaign_id,
            role="agent",
            counterparty_price=state.get("target_fob_ceiling"),
            net_spread=state.get("net_spread_usd"),
            raw_message=rfq_msg,
            direction="OUTBOUND",
        ))

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
        state = trade_graph.invoke(state, config=config)

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
        db.add(TradeAuditModel(
            campaign_id=campaign_id,
            thread_id=campaign_id,
            role="supplier",
            counterparty_price=supplier_fob,
            net_spread=state.get("net_spread_usd"),
            raw_message=supplier_quote_email,
            direction="INBOUND",
        ))

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
                db.add(TradeAuditModel(
                    campaign_id=campaign_id,
                    thread_id=campaign_id,
                    role="agent",
                    counterparty_price=state.get("last_counter_cif_usd"),
                    net_spread=state.get("net_spread_usd"),
                    raw_message=counter_draft or "",
                    direction="OUTBOUND",
                ))

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
                state = trade_graph.invoke(state, config=config)

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
                db.add(TradeAuditModel(
                    campaign_id=campaign_id,
                    thread_id=campaign_id,
                    role="buyer",
                    counterparty_price=target_buyer_cif,
                    net_spread=state.get("net_spread_usd"),
                    raw_message=concession_email,
                    direction="INBOUND",
                ))
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
            db.add(TradeAuditModel(
                campaign_id=campaign_id,
                thread_id=campaign_id,
                role="agent",
                counterparty_price=state.get("supplier_terms").price_usd_per_mt if state.get("supplier_terms") else None,
                net_spread=state.get("net_spread_usd"),
                raw_message=state.get("supplier_draft", ""),
                direction="OUTBOUND",
            ))

            state["audit_transcript"].append({
                "turn": state.get("negotiation_round", 3),
                "sender": f"Trading Desk ({settings.desk_name})",
                "recipient": f"{buyer_name} <{buyer_email}>",
                "role": "agent",
                "action": "ACCEPT_AND_CLOSE",
                "subject": f"Soft Corporate Offer (SCO) Acceptance — {commodity}",
                "message": state.get("buyer_draft", ""),
            })
            db.add(TradeAuditModel(
                campaign_id=campaign_id,
                thread_id=campaign_id,
                role="agent",
                counterparty_price=state.get("buyer_terms").price_usd_per_mt if state.get("buyer_terms") else None,
                net_spread=state.get("net_spread_usd"),
                raw_message=state.get("buyer_draft", ""),
                direction="OUTBOUND",
            ))

        db_camp.deal_status = state.get("deal_status", "closed")
        db.commit()

        try:
            trade_graph.update_state(
                config,
                {
                    "audit_transcript": state.get("audit_transcript", []),
                    "deal_status": state.get("deal_status", "closed"),
                    "action": state.get("action", "ACCEPT_AND_CLOSE"),
                    "buyer_draft": state.get("buyer_draft"),
                    "supplier_draft": state.get("supplier_draft"),
                },
            )
        except Exception as exc:
            logger.warning(f"Could not update graph state: {exc}")

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
    finally:
        if should_close:
            db.close()


@app.post("/api/negotiate")
def negotiate_turn(req: SimulateTurnRequest, db: Session = Depends(get_db)):
    logger.info(f"[API NEGOTIATE] Inbound turn for campaign '{req.campaign_id}' from role='{req.sender_role}'")
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == req.campaign_id).first()
    if not db_camp:
        logger.warning(f"[API NEGOTIATE] Campaign '{req.campaign_id}' not found in database.")
        raise HTTPException(status_code=404, detail=f"Campaign {req.campaign_id} not found.")

    config = {"configurable": {"thread_id": req.campaign_id}}
    snapshot = trade_graph.get_state(config)
    state = dict(snapshot.values) if snapshot and snapshot.values else {}
    if not state:
        logger.warning(f"[API NEGOTIATE] State for campaign '{req.campaign_id}' not found.")
        raise HTTPException(status_code=404, detail=f"State for campaign {req.campaign_id} not found.")

    turn_state = dict(state)
    turn_state["latest_email"] = req.raw_email
    turn_state["active_role"] = req.sender_role

    updated_state = trade_graph.invoke(turn_state, config=config)

    buyer_t = updated_state.get("buyer_terms")
    supp_t = updated_state.get("supplier_terms")
    price = (
        buyer_t.price_usd_per_mt
        if (req.sender_role == "buyer" and buyer_t)
        else (supp_t.price_usd_per_mt if supp_t else None)
    )

    inbound_audit = TradeAuditModel(
        campaign_id=req.campaign_id,
        thread_id=req.campaign_id,
        role=req.sender_role,
        counterparty_price=price,
        net_spread=updated_state.get("net_spread_usd"),
        raw_message=req.raw_email,
        direction="INBOUND",
    )
    db.add(inbound_audit)

    outbound_msg = (
        updated_state.get("buyer_draft")
        if req.sender_role == "supplier" or updated_state.get("action") in ["ACCEPT_AND_CLOSE", "REJECT_HARD", "COUNTER_TO_MAXIMIZE"]
        else updated_state.get("supplier_draft")
    )
    if outbound_msg:
        outbound_audit = TradeAuditModel(
            campaign_id=req.campaign_id,
            thread_id=req.campaign_id,
            role="agent",
            counterparty_price=updated_state.get("anchor_cif_usd"),
            net_spread=updated_state.get("net_spread_usd"),
            raw_message=outbound_msg,
            direction="OUTBOUND",
        )
        db.add(outbound_audit)

    db_camp.deal_status = updated_state.get("deal_status", db_camp.deal_status)
    db.commit()

    logger.info(
        f"[API NEGOTIATE] Turn completed for '{req.campaign_id}' | Round {updated_state.get('negotiation_round')} | "
        f"Action: {updated_state.get('action')} | Status: {updated_state.get('deal_status')} | "
        f"Spread: ${updated_state.get('net_spread_usd', 0.0):.2f}/MT | Margin: {updated_state.get('net_margin_pct', 0.0):.2f}%"
    )

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
def get_campaign_status(campaign_id: str, db: Session = Depends(get_db)):
    db_camp = db.query(CampaignModel).filter(CampaignModel.id == campaign_id).first()
    if not db_camp:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found.")

    config = {"configurable": {"thread_id": campaign_id}}
    snapshot = trade_graph.get_state(config)
    state = dict(snapshot.values) if snapshot and snapshot.values else {}

    audits = (
        db.query(TradeAuditModel)
        .filter(TradeAuditModel.campaign_id == campaign_id)
        .order_by(TradeAuditModel.id.asc())
        .all()
    )

    audit_transcript = state.get("audit_transcript") or []
    if not audit_transcript and audits:
        for a in audits:
            audit_transcript.append({
                "turn": 0,
                "sender": a.role,
                "recipient": "trading_desk",
                "role": a.role,
                "action": a.direction,
                "message": a.raw_message,
            })

    campaign_obj = state.get("campaign")
    commodity = campaign_obj.commodity if campaign_obj else db_camp.commodity
    target_margin_pct = campaign_obj.target_margin_pct if campaign_obj else settings.default_target_margin_pct
    min_profit_per_mt_hard = campaign_obj.min_profit_per_mt_hard if campaign_obj else 50.0
    min_profit_per_mt_soft = campaign_obj.min_profit_per_mt_soft if campaign_obj else 120.0
    max_negotiation_rounds = campaign_obj.max_negotiation_rounds if campaign_obj else 3

    buyer_t = state.get("buyer_terms")
    supp_t = state.get("supplier_terms")

    return {
        "campaign_id": campaign_id,
        "commodity": commodity,
        "target_margin_pct": target_margin_pct,
        "min_profit_per_mt_hard": min_profit_per_mt_hard,
        "min_profit_per_mt_soft": min_profit_per_mt_soft,
        "max_negotiation_rounds": max_negotiation_rounds,
        "negotiation_round": state.get("negotiation_round", 0),
        "negotiation_rounds_completed": state.get("negotiation_round", 0),
        "deal_status": state.get("deal_status", db_camp.deal_status),
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
        "anchor_cif_usd": state.get("anchor_cif_usd", db_camp.anchor_cif_usd or 0.0),
        "benchmark_fob_usd": state.get("benchmark_fob_usd"),
        "freight_cost_usd": state.get("freight_cost_usd"),
        "buyer_terms": buyer_t.model_dump() if buyer_t else None,
        "supplier_terms": supp_t.model_dump() if supp_t else None,
        "buyer_draft": state.get("buyer_draft"),
        "supplier_draft": state.get("supplier_draft"),
        "audit_transcript": audit_transcript,
    }


@app.get("/")
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Commodity Arbitrage API is running. Visit /docs for Swagger documentation."}
