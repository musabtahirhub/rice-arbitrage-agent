from typing import Optional
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.db_models import CampaignModel, TradeAuditModel

def match_email_to_campaign(subject: str, body: str, sender: str, db: Optional[Session] = None) -> Optional[str]:
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
