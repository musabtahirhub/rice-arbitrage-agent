import sys
import time
import uuid

from app.config import settings
from app.email_service import (
    check_latest_reply,
    get_unseen_message_ids,
    parse_email_draft,
    send_email,
)
from app.market import estimate_freight, get_benchmark_rate
from app.math_engine import calculate_dynamic_bounds
from app.models import Campaign, DealState, ParsedEmail
from app.workflow import generate_proactive_sco_draft, trade_graph


def run_live_email_negotiation(recipient_email: str | None = None):
    print("=" * 72)
    print("  COMMODITY ARBITRAGE DESK — LIVE GMAIL NEGOTIATION RUNNER")
    print("=" * 72)

    target_email = recipient_email or settings.my_test_email
    if not target_email:
        if len(sys.argv) > 1:
            target_email = sys.argv[1].strip()
        else:
            try:
                target_email = input("\nEnter your target test email address (e.g. buyer@gmail.com): ").strip()
            except (EOFError, KeyboardInterrupt):
                target_email = "test.buyer@domain.com"

    if not target_email:
        target_email = "test.buyer@domain.com"

    print(f"\n[CONFIG] Target Buyer Email: {target_email}")
    print(f"[CONFIG] Desk Identity:     {settings.desk_name}")
    print(f"[CONFIG] Gemini AI Model:   {settings.gemini_model} (Active: {bool(settings.gemini_api_key)})")

    live_transport = bool(settings.email_user and settings.email_pass)
    if live_transport:
        print(f"[CONFIG] Live Transport:    ENABLED (SMTP: {settings.smtp_server}, IMAP: {settings.imap_server})")
    else:
        print("[CONFIG] Live Transport:    DISABLED (EMAIL_USER / EMAIL_PASS not configured in .env)")
        print("          Please set EMAIL_USER and EMAIL_PASS to test with real Gmail.")
        print("          Falling back to interactive console input for buyer responses.\n")

    cid = f"CAMP-{uuid.uuid4().hex[:6].upper()}"
    campaign = Campaign(
        campaign_id=cid,
        commodity=settings.default_commodity,
        target_volume_mt=settings.default_target_volume_mt,
        target_margin_pct=settings.default_target_margin_pct,
        max_variance_from_benchmark_pct=settings.default_max_variance_pct,
        destination_port=settings.default_destination_port,
        min_profit_per_mt_hard=50.0,
        min_profit_per_mt_soft=120.0,
        max_negotiation_rounds=3,
    )

    baseline_unseen_ids = get_unseen_message_ids() if live_transport else set()

    print("\n[AI DRAFTING] Launching proactive outreach node natively via trade_graph...")
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
        "target_buyer_email": target_email,
        "target_buyer_name": "Institutional Procurement Partner",
    }
    config = {"configurable": {"thread_id": cid}}
    state = trade_graph.invoke(initial_state, config=config)

    if state.get("supplier_terms") is None:
        state["supplier_terms"] = ParsedEmail(
            sender_role="supplier",
            commodity=campaign.commodity,
            quantity_mt=campaign.target_volume_mt,
            price_usd_per_mt=state.get("benchmark_fob_usd", 900.0),
            incoterm="FOB",
            port=campaign.origin_port_default,
            payment_terms=settings.default_payment_terms,
        )

    sco_draft = state.get("buyer_draft", "")
    sco_subject, sco_body = parse_email_draft(sco_draft)
    print("\n" + "-" * 72)
    print(f"OUTBOUND EMAIL DRAFT (Turn 0):\nSUBJECT: {sco_subject}\n\n{sco_body}")
    print("-" * 72)
    print("\n[OUTBOUND] Dispatched Cold SCO. Entering IMAP listening loop...")

    expected_snippet = campaign.commodity.split()[0].lower()
    poll_round = 0

    try:
        while True:
            incoming_text = None

            if live_transport:
                poll_round += 1
                print(f"[POLLING] Checking IMAP every 10s for unread reply (Check #{poll_round}) [Ctrl+C to stop]...")
                incoming_text = check_latest_reply(
                    expected_subject_snippet=expected_snippet,
                    ignore_message_ids=baseline_unseen_ids,
                    allowed_senders={target_email.lower()} if target_email else None,
                )
                if not incoming_text:
                    time.sleep(10)
                    continue
            else:
                print("\n[MANUAL CONSOLE INPUT] Type buyer's counter-offer (or 'quit' to exit):")
                try:
                    user_input = input("Buyer Reply: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n[STOPPED] Exiting runner.")
                    break
                if user_input.lower() in ["quit", "exit", "q"]:
                    break
                incoming_text = user_input

            print(f"\n[INBOUND] Received reply: '{incoming_text}'")

            in_msg_id = getattr(incoming_text, "message_id", "")
            in_refs = getattr(incoming_text, "references", "")
            if in_msg_id:
                state["last_buyer_message_id"] = in_msg_id
                cur_refs = state.get("buyer_references") or in_refs or ""
                if in_msg_id not in cur_refs:
                    state["buyer_references"] = f"{cur_refs} {in_msg_id}".strip()

            state["latest_email"] = incoming_text
            state["active_role"] = "buyer"

            print("[LANGGRAPH] Evaluating math engine and invoking Gemini drafting node...")
            state = trade_graph.invoke(state, config=config)

            round_num = state.get("negotiation_round", 1)
            action = state.get("action")
            deal_status = state.get("deal_status")
            net_spread = state.get("net_spread_usd", 0.0)
            net_margin = state.get("net_margin_pct", 0.0)
            reason = state.get("evaluation_reason", "")

            print(f"\n[STATUS] Round: {round_num} | Action: {action} | Deal Status: {deal_status}")
            print(f"[METRICS] Net Spread: ${net_spread:.2f}/MT | Net Margin: {net_margin:.2f}%")
            print(f"[REASON]  {reason}")

            outbound_msg = state.get("buyer_draft", "")
            out_sub, out_body = parse_email_draft(outbound_msg)

            print("\n" + "-" * 72)
            print(f"OUTBOUND AGENT RESPONSE ({action}):\nSUBJECT: {out_sub}\n\n{out_body}")
            print("-" * 72)

            response_msg_id = email.utils.make_msgid(domain=desk_domain)
            send_email(
                target_email,
                outbound_msg,
                in_reply_to=state.get("last_buyer_message_id"),
                references=state.get("buyer_references"),
                thread_subject=state.get("thread_subject"),
                custom_message_id=response_msg_id,
            )
            state["last_buyer_message_id"] = response_msg_id
            cur_refs = state.get("buyer_references") or ""
            state["buyer_references"] = f"{cur_refs} {response_msg_id}".strip()

            print(f"[OUTBOUND] Dispatched response ({action}) to {target_email} in thread: '{state.get('thread_subject')}'.")

            if action in ["ACCEPT_AND_CLOSE", "REJECT_HARD"] or deal_status in ["closed", "rejected"]:
                print("\n" + "=" * 72)
                print(f"  NEGOTIATION COMPLETE: Deal {deal_status.upper()}! Final Net Spread: ${net_spread:.2f}/MT")
                print("=" * 72)
                break

            print("\n[WAITING] Listening for next buyer reply...")

    except KeyboardInterrupt:
        print("\n\n[STOPPED] Polling cancelled by user (Ctrl+C). Exiting.")


if __name__ == "__main__":
    cli_email = sys.argv[1].strip() if len(sys.argv) > 1 else None
    run_live_email_negotiation(cli_email)
