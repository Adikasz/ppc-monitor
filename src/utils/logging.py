"""
Egységes naplózás az egész alkalmazásban.

Használat:
    from src.utils.logging import get_logger
    log = get_logger(__name__)
    log.info("Valami történt")
"""
from __future__ import annotations

import logging
import sys


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    # Ha már be van állítva, ne duplikáljuk a handlereket
    if logger.handlers:
        return logger

    # A szintet a config-ból olvassuk, de elkerüljük a körkörös importot:
    # közvetlenül a környezetből nézzük.
    import os
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-7s  %(name)s  —  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Ne propagáljon a gyökér loggerhez (elkerüli a dupla logokat)
    logger.propagate = False

    return logger
