from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base, relationship

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
    direction = Column(String, nullable=False)  # "INBOUND" or "OUTBOUND"
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    campaign = relationship("CampaignModel", back_populates="trade_audits")
