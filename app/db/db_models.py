"""ORM models, plus helpers that record negotiation history (trade_audits rows + audit_transcript)."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session, declarative_base, relationship

from app.config import settings
from app.schemas import AuditTranscriptEntry

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CampaignModel(Base):
    __tablename__ = "campaigns"

    id = Column(String, primary_key=True)
    commodity = Column(String, nullable=False)
    target_volume_mt = Column(Float, nullable=False)
    destination_port = Column(String, nullable=False)
    origin_port_default = Column(String, nullable=False)
    deal_status = Column(String, nullable=False, default="initiating")
    anchor_cif_usd = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    trade_audits = relationship(
        "TradeAuditModel",
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="TradeAuditModel.id",
    )


class TradeAuditModel(Base):
    __tablename__ = "trade_audits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    thread_id = Column(String, index=True, nullable=False)
    role = Column(String, nullable=False)
    counterparty_price = Column(Float, nullable=True)
    net_spread = Column(Float, nullable=True)
    raw_message = Column(Text, nullable=False)
    direction = Column(String, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    campaign = relationship("CampaignModel", back_populates="trade_audits")


class ProcessedInboundEmailModel(Base):
    """One row per inbound email the desk has claimed. The primary key makes the claim atomic across
    processes and ingestion paths (Pub/Sub push and IMAP polling), so each email gets one reply."""

    __tablename__ = "processed_inbound_emails"

    message_key = Column(String, primary_key=True)  # RFC 822 Message-ID, else the Gmail message id
    campaign_id = Column(String, nullable=True)
    source = Column(String, nullable=False)  # "push" | "poll"
    status = Column(String, nullable=False, default="processing")  # "processing" | "done"
    created_at = Column(DateTime, default=_utcnow, nullable=False)


def desk_party(title: str = "Trading Desk") -> str:
    """How the desk appears as sender/recipient in transcripts, e.g. 'Trading Desk (Global Agro ...)'."""
    return f"{title} ({settings.desk_name})"


def add_trade_audit(
    db: Session,
    campaign_id: str,
    role: str,
    raw_message: Optional[str],
    direction: str,
    counterparty_price: Optional[float] = None,
    net_spread: Optional[float] = None,
) -> None:
    db.add(TradeAuditModel(
        campaign_id=campaign_id,
        thread_id=campaign_id,
        role=role,
        counterparty_price=counterparty_price,
        net_spread=net_spread,
        raw_message=raw_message or "",
        direction=direction,
    ))


def record_exchange(
    state: dict,
    db: Session,
    campaign_id: str,
    *,
    turn: int,
    sender: str,
    recipient: str,
    role: str,
    action: str,
    subject: str,
    message: str,
    direction: str,
    counterparty_price: Optional[float] = None,
    net_spread: Optional[float] = None,
    msg_id: Optional[str] = None,
) -> None:
    """Append one email to state['audit_transcript'] and stage the matching trade_audits row."""
    entry = AuditTranscriptEntry(
        turn=turn, sender=sender, recipient=recipient, role=role, action=action, subject=subject,
        message=message, id=msg_id,
    )
    if state.get("audit_transcript") is None:
        state["audit_transcript"] = []
    state["audit_transcript"].append(entry.model_dump(exclude_none=True))
    add_trade_audit(db, campaign_id, role, message, direction, counterparty_price, net_spread)
