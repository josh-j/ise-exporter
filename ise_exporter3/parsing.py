"""Small shared normalisers for ISE response values."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone


logger = logging.getLogger(__name__)

_DATE_FORMATS = (
    "%a %b %d %H:%M:%S UTC %Y",
    "%a %b %d %H:%M:%S %Y",
    "%a %b %d %H:%M:%S %Z %Y",
)


def parse_ise_date(value):
    """Parse the ISO and ctime-ish shapes ISE uses, always tz-aware."""
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    logger.debug("could not parse ISE date: %s", text)
    return None


def finite(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default
