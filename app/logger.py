"""
logger.py
- One logger for the whole program.
- Writes to logs/studybridge_YYYY-MM-DD.log AND to the console.
- Every line has a timestamp, the level (INFO/ERROR), and where it came from.
- When something crashes, the FULL error trace is written to the log.

If anything breaks, the user only needs to send the newest file in the logs/ folder.
"""
import logging
import os
from datetime import date
from .config import LOGS_DIR

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_logger = None


def get_logger():
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("studybridge")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # If this named logger already has handlers (e.g. server reload), reuse it.
    if logger.handlers:
        _logger = logger
        return logger

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # File: one file per day
    log_path = os.path.join(LOGS_DIR, f"studybridge_{date.today().isoformat()}.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    logger.info("=" * 70)
    logger.info("StudyBridge logger started. Log file: %s", log_path)
    _logger = logger
    return logger
