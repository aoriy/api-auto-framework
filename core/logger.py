"""统一日志：可读的调用流水线，方便定位是哪一步失败。"""
from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)-10s | %(message)s"


def get_logger(name: str = "framework", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    return logger
