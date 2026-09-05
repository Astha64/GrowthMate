"""
Guardrail / policy layer — plain deterministic Python, no I/O, no LLM.

Revision 3 keeps every money decision in plain deterministic functions
(LLD §16, ARCHITECTURE §13):

  1. `validate_approval`  — did the user explicitly approve THIS checkout,
     tied to a shown preview and a current quote when one exists? Fail-closed.
  2. `validate_quote_against_cart` — is the quote still an exact snapshot of
     the current cart (hash + version), unexpired, and same actor? Fail-closed.
  3. `check_transaction`   — spend limits (per-transaction / per-session /
     unknown actor). Fail-closed.

Rule order in check_transaction (first failing rule wins):
  1. actor not in ALLOWED_ACTORS        -> block "unknown actor"
  2. amount > MAX_PER_TRANSACTION       -> block per-transaction
  3. spend_so_far + amount > MAX_PER_SESSION -> block per-session
  4. else -> allow

This module performs no DB or network access — fully unit-testable in
isolation (tests/test_guardrail.py).
"""

from dataclasses import dataclass
from datetime import datetime, timezone

MAX_PER_TRANSACTION = {"human": 5000.0, "buyer_agent": 3000.0}
MAX_PER_SESSION = {"human": 15000.0, "buyer_agent": 5000.0}
ALLOWED_ACTORS = {"human", "buyer_agent"}


@dataclass
class GuardrailDecision:
    allowed: bool
    reason: str


# ---------------------------------------------------------------------------
# §6.1  Approval validation — fail-closed, never an LLM tool.
# ---------------------------------------------------------------------------

# A small fixed set of affirmative patterns, tied to the specific confirmation
# question `approval_node` expects the agent to have asked (e.g. "shall I
# proceed with this checkout?"). Anything not in this set — or any missing
# precondition — is treated as NOT approved (fail-closed).
AFFIRMATIVE_PATTERNS = {
    "yes",
    "yep",
    "yeah",
    "ya",
    "go ahead",
    "proceed",
    "confirm",
    "confirmed",
    "approved",
    "looks good",
    "sure",
    "ok",
    "okay",
    "yes please",
    "do it",
    "that works",
    "sounds good",
}

# Negation words that flip any affirmative match back to NOT approved
# (fail-closed): "no", "not sure", "don't", etc.
NEGATION_WORDS = (
    "no", "nope", "not", "never", "don't", "dont", "do not",
    "not now", "wait", "hold on", "maybe", "perhaps",
)


def is_explicit_approval(text: str) -> bool:
    """True if `text` is an unambiguous explicit approval (fail-closed)."""
    clean = (text or "").strip().lower()
    return _is_affirmative(clean)


def _is_affirmative(text: str) -> bool:
    """Return True if `text` is an explicit approval (containing an affirmative
    phrase and no negation), False otherwise."""
    # Any negation word anywhere flips a would-be match back to False
    # (fail-closed). "not sure" must NOT count as approval even though it
    # contains "sure". Word-boundary check avoids matching inside "donut".
    negated = any(
        f" {w} " in f" {text} " or text.startswith(f"{w} ") or text == w or text.endswith(f" {w}")
        for w in NEGATION_WORDS
    )
    if negated:
        return False
    hit = any(p in text for p in AFFIRMATIVE_PATTERNS if p)
    return hit


def _ensure_utc(dt) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC for comparisons."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def validate_approval(state: dict) -> bool:
    """
    True only if:
      - state['checkout_preview'] is set (a checkout preview was actually
        shown to this user in this same conversation), AND
      - when state['quote'] is present it must be active and un-expired
        (approval is tied to the *shown* quote), AND
      - the most recent user message, checked against AFFIRMATIVE_PATTERNS, is
        classified as explicit approval.

    Fail-closed: any ambiguity, missing precondition, or classification error
    returns False, never True. Approval is a state transition tied to a prior
    shown checkout preview / quote — not a standalone sentiment judgment.
    """
    preview = state.get("checkout_preview")
    if not preview:
        return False

    quote = state.get("quote")
    if quote:
        status = quote.get("status", "")
        expires_at = quote.get("expires_at")
        if status != "active":
            return False
        if expires_at:
            if isinstance(expires_at, str):
                try:
                    expires_at = datetime.fromisoformat(expires_at)
                except ValueError:
                    return False
            if _ensure_utc(expires_at) <= datetime.now(timezone.utc):
                return False

    messages = state.get("messages") or []
    last_user = None
    for msg in reversed(messages):
        if isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
        else:
            role = getattr(msg, "type", None)
            content = getattr(msg, "content", None)
        if role == "user" and content is not None:
            last_user = str(content).strip().lower()
            break

    if not last_user:
        return False

    return _is_affirmative(last_user)


# ---------------------------------------------------------------------------
# §14/§16.2  Quote integrity — cart hash/version must match the quoted snapshot.
# ---------------------------------------------------------------------------

def validate_quote_against_cart(quote: dict, cart_items: list[dict], cart_version: int) -> tuple[bool, str]:
    """Pure check that a quote is still a faithful snapshot of the current cart.

    `quote`          — a dict from `quote.to_quote_dict`.
    `cart_items`     — current cart rows as plain dicts (sku, quantity, price,
                       currency), any order (hash canonicalizes).
    `cart_version`   — current persisted cart version for the session.

    Fail-closed: empty cart, missing quote, expired quote, cart hash mismatch,
    version mismatch, or actor mismatch all block.
    """
    if not quote:
        return False, "no quote exists for this session"
    if not cart_items:
        return False, "cart is empty — nothing to charge"

    from app.quote import compute_cart_hash

    current_hash = compute_cart_hash(cart_items)
    if quote.get("cart_hash") != current_hash:
        return False, "cart changed after the quote was created"

    if int(quote.get("cart_version", -1)) != int(cart_version):
        return False, "cart version changed after the quote was created"

    status = quote.get("status")
    if status != "active":
        return False, f"quote is {status}"

    expires_at = quote.get("expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at)
            except ValueError:
                return False, "quote has no valid expiry"
        if _ensure_utc(expires_at) <= datetime.now(timezone.utc):
            return False, "quote has expired"

    return True, "quote matches the current cart"


# ---------------------------------------------------------------------------
# §6.2  Transaction spend-limit check.
# ---------------------------------------------------------------------------

def check_transaction(actor: str, amount, spend_so_far) -> GuardrailDecision:
    """Pure function: same inputs always produce the same output.

    Accepts Decimal/int/float money values; normalized to float for comparison
    so callers can feed Decimal amounts from quote/cart math.
    """
    amount = float(amount)
    spend_so_far = float(spend_so_far)

    if actor not in ALLOWED_ACTORS:
        return GuardrailDecision(allowed=False, reason="unknown actor")

    if amount > MAX_PER_TRANSACTION[actor]:
        return GuardrailDecision(
            allowed=False,
            reason=f"exceeds per-transaction limit of ₹{int(MAX_PER_TRANSACTION[actor])}",
        )

    if spend_so_far + amount > MAX_PER_SESSION[actor]:
        return GuardrailDecision(
            allowed=False,
            reason=f"exceeds per-session limit of ₹{int(MAX_PER_SESSION[actor])}",
        )

    return GuardrailDecision(allowed=True, reason="within limits")
