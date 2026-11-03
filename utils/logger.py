"""Logger configuration.

By default logs go to stderr in human-readable form and to a rotated daily
file at ``logs/evoagent_YYYY-MM-DD.log``. ``LOG_DIR`` selects the file directory;
relative paths resolve from the working directory, and unset or blank values
default to ``logs``. Set ``LOG_JSON=1`` to switch the
stderr sink to single-line JSON — useful when running behind Loki, ELK, or
CloudWatch-style log aggregators.
"""

import os
import sys
from pathlib import Path

from loguru import logger

_JSON_LOGS = os.getenv("LOG_JSON", "").strip().lower() in ("1", "true", "yes")
_STDERR_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_FILE_LEVEL = os.getenv("LOG_FILE_LEVEL", "DEBUG").upper()
_LOG_DIR = Path(os.getenv("LOG_DIR", "").strip() or "logs")

logger.remove()

if _JSON_LOGS:
    # loguru's `serialize=True` emits one JSON object per line with `time`,
    # `level`, `message`, `name`, `function`, `line`, and any `extra` fields.
    logger.add(sys.stderr, level=_STDERR_LEVEL, serialize=True)
else:
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level=_STDERR_LEVEL,
    )

logger.add(
    _LOG_DIR / "evoagent_{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention="7 days",
    level=_FILE_LEVEL,
)
