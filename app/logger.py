import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from app.config import PROJECT_ROOT, settings

# LOG_FILE in .env/env overrides the default, e.g. so test runs don't write into the server's log.
LOG_FILE = Path(settings.log_file) if settings.log_file else PROJECT_ROOT / "logs" / "arbitrage_desk.log"
LOGS_DIR = LOG_FILE.parent

_CONFIGURED = False


def _build_console_handler(formatter: logging.Formatter, level: int) -> logging.Handler:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    return console_handler


def _build_file_handler(formatter: logging.Formatter, level: int) -> logging.Handler | None:
    try:
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        return file_handler
    except Exception:
        return None


def setup_logger(name: str = "arbitrage_desk", level: Optional[int] = None) -> logging.Logger:
    global _CONFIGURED

    if level is None:
        level = getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO)

    parent_logger = logging.getLogger("arbitrage_desk")
    parent_logger.setLevel(level)

    if not _CONFIGURED or not parent_logger.handlers:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        formatter = logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)-7s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        parent_logger.handlers.clear()
        parent_logger.addHandler(_build_console_handler(formatter, level))

        file_h = _build_file_handler(formatter, level)
        if file_h:
            parent_logger.addHandler(file_h)
        else:
            parent_logger.warning(f"Could not initialize file handler at {LOG_FILE}")

        parent_logger.propagate = False
        _CONFIGURED = True

    target_logger = logging.getLogger(name)
    target_logger.setLevel(level)
    if name != "arbitrage_desk":
        target_logger.propagate = True

    return target_logger


def get_logger(name: str = "arbitrage_desk") -> logging.Logger:
    return setup_logger(name)
