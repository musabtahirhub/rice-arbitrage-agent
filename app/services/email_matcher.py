from typing import Optional
from sqlalchemy.orm import Session
import app.database
from app.db_models import CampaignModel, TradeAuditModel


def _extract_clean_email_tokens(sender_raw: str) -> list[str]:
    cleaned = sender_raw.lower().replace("<", " ").replace(">", " ")
    return [token for token in cleaned.split() if "@" in token]


def _find_campaign_by_id_reference(campaigns: list[CampaignModel], text_corpus: str) -> Optional[str]:
    for campaign in campaigns:
        if campaign.id.lower() in text_corpus:
            return campaign.id
    return None


def _find_campaign_by_audit_sender(campaigns: list[CampaignModel], sender_tokens: list[str], db: Session) -> Optional[str]:
    if not sender_tokens:
        return None
    for campaign in campaigns:
        audits = db.query(TradeAuditModel).filter(TradeAuditModel.campaign_id == campaign.id).all()
        for audit in audits:
            audit_message_lower = audit.raw_message.lower()
            if any(token in audit_message_lower for token in sender_tokens):
                return campaign.id
    return None


def _find_campaign_by_commodity_keywords(campaigns: list[CampaignModel], subject_text: str, body_text: str) -> Optional[str]:
    for campaign in campaigns:
        commodity_lower = campaign.commodity.lower()
        if commodity_lower in subject_text or commodity_lower in body_text:
            return campaign.id
        if any(word in subject_text for word in commodity_lower.split()):
            return campaign.id
    return None


def _fetch_candidate_campaigns(db: Session) -> list[CampaignModel]:
    active_campaigns = (
        db.query(CampaignModel)
        .filter(~CampaignModel.deal_status.in_(["closed", "rejected"]))
        .order_by(CampaignModel.created_at.desc())
        .all()
    )
    if active_campaigns:
        return active_campaigns
    return db.query(CampaignModel).order_by(CampaignModel.created_at.desc()).all()


def match_email_to_campaign(subject: str, body: str, sender: str, db: Optional[Session] = None) -> Optional[str]:
    subject_lower = subject.lower()
    body_lower = body.lower()
    combined_corpus = f"{subject_lower} {body_lower}"

    should_close_session = False
    if db is None:
        db = app.database.SessionLocal()
        should_close_session = True

    try:
        campaigns = _fetch_candidate_campaigns(db)
        if not campaigns:
            return None

        matched_id = _find_campaign_by_id_reference(campaigns, combined_corpus)
        if matched_id:
            return matched_id

        sender_tokens = _extract_clean_email_tokens(sender)
        matched_id = _find_campaign_by_audit_sender(campaigns, sender_tokens, db)
        if matched_id:
            return matched_id

        return _find_campaign_by_commodity_keywords(campaigns, subject_lower, body_lower)
    finally:
        if should_close_session:
            db.close()
