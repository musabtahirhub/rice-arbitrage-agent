import asyncio
import email.utils
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
import app.database
from app.db_models import CampaignModel, TradeAuditModel
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity
from app.email_service import (
    check_latest_reply,
    initialize_unseen_snapshot,
    is_automated_or_bounce_message,
    normalize_thread_subject,
    send_email,
)
from app.gmail_client import (
    fetch_latest_messages_by_history,
    send_gmail_reply,
)
from app.market import get_benchmark_rate
from app.models import Campaign, ParsedEmail
import app.workflow
from app.workflow import split_subject_and_body
from app.services.email_matcher import match_email_to_campaign

logger = logging.getLogger("arbitrage_desk")

LAST_SEEN_HISTORY_ID: Optional[str] = None
GMAIL_EVENT_LOCK = asyncio.Lock()


async def email_polling_worker():
    poll_interval = 15
    logger.info(f"[EMAIL WORKER] Initialized live email polling worker (interval: {poll_interval}s)")

    await asyncio.to_thread(initialize_unseen_snapshot)

    while True:
        try:
            allowed_senders: set[str] = set()
            if settings.my_test_email:
                allowed_senders.add(settings.my_test_email.strip().lower())

            with app.database.SessionLocal() as db:
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

                with app.database.SessionLocal() as db:
                    matched_cid = match_email_to_campaign(sub, raw_body, sender, db=db)
                    db_camp = db.query(CampaignModel).filter(CampaignModel.id == matched_cid).first() if matched_cid else None
                    if db_camp:
                        if db_camp.deal_status in ["closed", "rejected"]:
                            logger.info(f"[EMAIL WORKER] Campaign {matched_cid} is already {db_camp.deal_status}. Skipping.")
                        else:
                            logger.info(f"[EMAIL WORKER] Processing inbound email for campaign: {matched_cid}")
                            config = {"configurable": {"thread_id": db_camp.id}}
                            snapshot = app.workflow.trade_graph.get_state(config)
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
                            state["buyer_draft"] = ""
                            state["supplier_draft"] = ""

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

                            g_config = {"configurable": {"thread_id": db_camp.id}}
                            updated_state = await asyncio.to_thread(
                                app.workflow.trade_graph.invoke,
                                state,
                                config=g_config,
                            )

                            snapshot = app.workflow.trade_graph.get_state(g_config)
                            if snapshot and snapshot.next and "approval_gate" in snapshot.next:
                                app.workflow.trade_graph.update_state(g_config, {"deal_status": "pending_approval"})
                                updated_state["deal_status"] = "pending_approval"

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

                            if deal_status == "pending_approval" or (snapshot and snapshot.next and "approval_gate" in snapshot.next):
                                logger.info(
                                    f"[EMAIL WORKER] Campaign {matched_cid} is awaiting human approval. "
                                    f"Halting outbound dispatch until human review."
                                )
                            else:
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
                                        f"[OUTBOUND EMAIL] Dispatched buyer reply to '{buyer_email}' | "
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
                                        "id": response_msg_id,
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

        with app.database.SessionLocal() as db:
            active_campaigns = (
                db.query(CampaignModel)
                .filter(~CampaignModel.deal_status.in_(["closed", "rejected"]))
                .order_by(CampaignModel.created_at.desc())
                .all()
            )
            if not active_campaigns:
                logger.warning(f"[GMAIL EVENT] No active campaigns found for inbound email. Subject: '{sub}'")
                continue

            matched_cid = match_email_to_campaign(sub, raw_body, sender, db=db)
            db_camp = db.query(CampaignModel).filter(CampaignModel.id == matched_cid).first() if matched_cid else None
            if not db_camp:
                logger.warning(f"[GMAIL EVENT] Inbound email detected, but no matching active campaign found in database. Subject: '{sub}'")
                continue

            if db_camp.deal_status in ["closed", "rejected"]:
                logger.info(f"[GMAIL EVENT] Campaign {matched_cid} is already {db_camp.deal_status}. Skipping.")
                continue

            logger.info(f"[GMAIL EVENT] Processing inbound email for campaign: {matched_cid}")

            config = {"configurable": {"thread_id": db_camp.id}}
            snapshot = app.workflow.trade_graph.get_state(config)
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
            state["buyer_draft"] = ""
            state["supplier_draft"] = ""

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

            if thread_id:
                state["thread_id"] = thread_id

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

            parsed_sender_email = email.utils.parseaddr(sender)[1]
            if parsed_sender_email and not any(desk_e in parsed_sender_email.lower() for desk_e in desk_emails):
                state["target_buyer_email"] = parsed_sender_email
            else:
                state["target_buyer_email"] = buyer_email

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

            g_config = {"configurable": {"thread_id": db_camp.id}}
            updated_state = await asyncio.to_thread(
                app.workflow.trade_graph.invoke,
                state,
                config=g_config,
            )

            snapshot = app.workflow.trade_graph.get_state(g_config)
            if snapshot and snapshot.next and "approval_gate" in snapshot.next:
                app.workflow.trade_graph.update_state(g_config, {"deal_status": "pending_approval"})
                updated_state["deal_status"] = "pending_approval"

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

            if deal_status == "pending_approval" or (snapshot and snapshot.next and "approval_gate" in snapshot.next):
                logger.info(
                    f"[GMAIL EVENT] Campaign {matched_cid} is awaiting human approval. "
                    f"Halting outbound buyer dispatch until approval is confirmed."
                )
            else:
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
                        "id": msg_id,
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
