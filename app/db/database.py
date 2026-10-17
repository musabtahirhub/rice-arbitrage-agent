"""Database engine and sessions, the psycopg pool for the Postgres checkpointer, and the ORM models."""
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from app.config import settings
from app.logger import get_logger

logger = get_logger("arbitrage_desk.database")

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


def get_sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


def get_pool_conninfo(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    return url


engine = create_engine(get_sqlalchemy_url(settings.database_url), pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

pool = ConnectionPool(
    conninfo=get_pool_conninfo(settings.database_url),
    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    open=False,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("[DATABASE] Database tables verified/initialized successfully.")
    except Exception as exc:
        logger.warning(f"[DATABASE] init_db skipped or failed: {exc}")
