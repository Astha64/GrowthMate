"""
HMAC mandate signing (Rev 3, Phase 4).

A *mandate* is a deterministic HMAC-SHA256 signature that binds a money move to
the exact cart snapshot that the guardrail explicitly approved. Only the ALLOW
path mints a signature:

  - `approval_node` / `guardrail_node` never sign on their own;
  - the ALLOW decision is finalized in one place (`guardrail_node` for /chat,
    `agent_checkout` for /agent/*), which calls `sign_mandate(...)` and hands
    the signature down to the Razorpay/order layer;
  - every BLOCK leaves `mandate_signature` NULL on the audit row and the Order.

The signature covers session + actor + cart_hash + exact amount + quote id, so
tampering with any of them invalidates verification (`verify_mandate`). It is
NOT encryption and does not replace guardrails — it is a tamper-evident audit
binding produced deterministically from server-side secret `MANDATE_SECRET`.

Startup fail-fast: `app.main`'s lifespan refuses to boot without
`MANDATE_SECRET` (the guardrail ALLOW path depends on it). No secrets live in
source; the value is read from the environment only.
"""

import hashlib
import hmac
import os

_FIELD_SEP = "|"


def mandate_secret() -> str:
    return os.environ.get("MANDATE_SECRET", "") or ""


def mandate_configured() -> bool:
    return bool(mandate_secret())


def _fmt_amount(amount) -> str:
    """Canonical two-decimal rendering so Decimal '4990.00', '4990' and float
    4990.0 all fingerprint identically."""
    try:
        return f"{float(amount):.2f}"
    except (TypeError, ValueError):
        return ""


def _canonical(*, session_id: str, actor: str, cart_hash: str,
               amount, quote_id: str | None) -> str:
    return _FIELD_SEP.join(
        [
            str(session_id or ""),
            str(actor or ""),
            str(cart_hash or ""),
            _fmt_amount(amount),
            str(quote_id or ""),
        ]
    )


def sign_mandate(*, session_id: str, actor: str, cart_hash: str,
                 amount, quote_id: str | None = None) -> str:
    """Mint the ALLOW-path signature. Deterministic; same inputs => same hex."""
    payload = _canonical(session_id=session_id, actor=actor,
                         cart_hash=cart_hash, amount=amount, quote_id=quote_id)
    return hmac.new(
        mandate_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_mandate(signature: str | None, *, session_id: str, actor: str,
                   cart_hash: str, amount, quote_id: str | None = None) -> bool:
    """Constant-time verification of a mandate signature. The only authority
    for 'was this cart snapshot actually approved?' on top of the DB rows."""
    if not signature or not mandate_configured():
        return False
    expected = sign_mandate(session_id=session_id, actor=actor,
                            cart_hash=cart_hash, amount=amount, quote_id=quote_id)
    return hmac.compare_digest(str(signature).lower(), expected.lower())


def verify_order_mandate(order: dict) -> bool:
    """Verify an order payload/serialized row carries a valid mandate over its
    own critical fields (Phase 4 binding; the feed re-verifies every row)."""
    return verify_mandate(
        order.get("mandate_signature"),
        session_id=order.get("session_id"),
        actor=order.get("actor"),
        cart_hash=order.get("cart_hash"),
        amount=order.get("total"),
        quote_id=order.get("quote_id"),
    )