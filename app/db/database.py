from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.db_models import Base
from app.logger import get_logger

logger = get_logger("arbitrage_desk.database")


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


def _connect_args(url: str) -> dict:
    """libpq's connect_timeout for Postgres; other drivers (e.g. SQLite in tests) don't accept it."""
    if url.startswith(("postgresql", "postgres://")):
        return {"connect_timeout": settings.database_connect_timeout_seconds}
    return {}


engine = create_engine(
    get_sqlalchemy_url(settings.database_url),
    pool_pre_ping=True,
    connect_args=_connect_args(settings.database_url),
)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


pool = ConnectionPool(
    conninfo=get_pool_conninfo(settings.database_url),
    kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
            "connect_timeout": settings.database_connect_timeout_seconds},
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
