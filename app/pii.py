"""
Minimal input-protection layer (ARCHITECTURE §16).

Detects obvious sensitive values (email, phone, card-like numbers, secret/key
patterns) so they can be redacted from logs and refused before being forwarded
to external providers. Deliberately conservative: this is a redaction guard,
not a security boundary.
"""

import hashlib
import re

from app.config import PII_REDACT_TOKEN

# Dart regex patterns
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\d])(?:\+?\d[\s().-]*\d){7,14}(?![\d])")
_CARD_RE = re.compile(r"\b(?:\d[ -]*){13,16}\b")
_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|client[_-]?secret)"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{8,})['\"]?"
)

# Hash keys for stable masked identities (never reversible from logs alone).
_EMAIL_HASH_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact_text(text: str) -> str:
    """Replace detected PII/secret values with a redaction token (logs safe)."""
    if not text:
        return text
    out = _EMAIL_RE.sub(PII_REDACT_TOKEN, text)
    out = _CARD_RE.sub(PII_REDACT_TOKEN, out)
    out = _SECRET_RE.sub(PII_REDACT_TOKEN, out)
    out = _PHONE_RE.sub(PII_REDACT_TOKEN, out)
    return out


def contains_sensitive(text: str) -> bool:
    """True if the text looks like it carries obvious PII or a secret."""
    if not text:
        return False
    return bool(
        _EMAIL_RE.search(text)
        or _PHONE_RE.search(text)
        or _CARD_RE.search(text)
        or _SECRET_RE.search(text)
    )


def mask_email(text: str) -> str:
    """Replace emails with a stable SHA-256 digest so grouping stays possible
    without storing the raw address."""
    return _EMAIL_HASH_RE.sub(
        lambda m: "user-" + hashlib.sha256(m.group(0).encode("utf-8")).hexdigest()[:12],
        text,
    )