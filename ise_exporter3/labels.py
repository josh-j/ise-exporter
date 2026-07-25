"""Byte-bounded Prometheus label values.

An ISE deployment supplies plenty of free-form text -- policy names, failure
messages, endpoint attributes -- and any of it reaching a label unbounded turns a
metric into row storage. Values are capped in UTF-8 bytes rather than characters,
and an over-long value keeps a hash suffix so two values sharing a long prefix
cannot silently collapse into one series.
"""
from __future__ import annotations

import hashlib


MAX_LABEL_BYTES = 256


def label(value, default="unknown", max_bytes=MAX_LABEL_BYTES):
    """Return a readable, deterministic, byte-bounded label value."""
    text = str(value or "").strip() or default
    limit = max(32, min(MAX_LABEL_BYTES, int(max_bytes)))
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    prefix = encoded[:limit - len(digest) - 1].decode("utf-8", "ignore")
    return f"{prefix}~{digest}"


def bounded_detail(value, max_bytes=240):
    """Collapse an operator explanation to one bounded single line."""
    text = " ".join(str(value or "").split())
    return label(text, default="no detail available", max_bytes=max_bytes)
