import logging
from typing import Optional
from fastapi import HTTPException
from sqlalchemy.orm import Session
from langgraph.types import Command

from app.config import settings
from app.database import SessionLocal
from app.db_models import CampaignModel, TradeAuditModel
from app.directory import get_buyers_for_commodity, get_suppliers_for_commodity
from app.models import Campaign
from app.workflow import trade_graph
from app.logger import setup_logger

logger = setup_logger("arbitrage_desk")

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

        snapshot = trade_graph.get_state(config)
        if snapshot and snapshot.next and "approval_gate" in snapshot.next:
            state = trade_graph.invoke(
                Command(resume={"approved": True, "reviewer_notes": "Autonomous campaign auto-approval"}),
                config=config,
            )

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

                snapshot = trade_graph.get_state(config)
                if snapshot and snapshot.next and "approval_gate" in snapshot.next:
                    state = trade_graph.invoke(
                        Command(resume={"approved": True, "reviewer_notes": "Autonomous campaign auto-approval"}),
                        config=config,
                    )

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
