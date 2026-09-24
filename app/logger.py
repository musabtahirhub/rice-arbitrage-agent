import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOGS_DIR / "arbitrage_desk.log"

_CONFIGURED = False


def setup_logger(name: str = "arbitrage_desk", level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED

    parent_logger = logging.getLogger("arbitrage_desk")
    parent_logger.setLevel(level)

    if not _CONFIGURED or not parent_logger.handlers:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        formatter = logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)-7s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        parent_logger.handlers.clear()

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        parent_logger.addHandler(console_handler)

        try:
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            parent_logger.addHandler(file_handler)
        except Exception as e:
            parent_logger.warning(f"Could not initialize file handler at {LOG_FILE}: {e}")

        parent_logger.propagate = False
        _CONFIGURED = True

    target_logger = logging.getLogger(name)
    target_logger.setLevel(level)
    if name != "arbitrage_desk":
        target_logger.propagate = True

    return target_logger


def get_logger(name: str = "arbitrage_desk") -> logging.Logger:
    return setup_logger(name)

