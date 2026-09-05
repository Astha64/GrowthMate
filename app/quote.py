"""
Quote service (Revision 3) — immutable backend-owned snapshot (LLD §14).

A quote ties the exact payable snapshot to a session/actor via:

  canonical cart representation (deterministic ordering)
    -> SHA-256 cart hash
    -> Quote row {quote_id, cart_version, cart_hash, amount, nonce, expiry}

The payment path (guardrail.validate_quote_against_cart) re-checks that the
current cart still hashes to the quoted hash before Razorpay is ever called.
Cart math is Decimal end-to-end; DB storage uses strings to stay exact.
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from app import db as db_module
from app.config import QUOTE_TTL_SECONDS
from app.models import Quote, SessionState


def canonical_cart_json(cart_items: list[dict]) -> str:
    """Deterministic, sortable representation of the payable cart.

    `cart_items` are plain dicts with at least sku/quantity/price/currency.
    Order is normalized so identical carts always serialize identically.
    """
    entries = [
        {
            "sku": str(i.get("sku") or i.get("ref_id") or ""),
            "quantity": int(i.get("quantity", 1)),
            "unit_price": str(_two_places(Decimal(str(i.get("price", 0))))),
        }
        for i in cart_items
    ]
    entries.sort(key=lambda e: e["sku"])
    return json.dumps(entries, sort_keys=True)


def compute_cart_hash(cart_items: list[dict]) -> str:
    return hashlib.sha256(canonical_cart_json(cart_items).encode("utf-8")).hexdigest()


def cart_total(cart_items: list[dict]) -> Decimal:
    total = Decimal("0.00")
    for i in cart_items:
        qty = Decimal(int(i.get("quantity", 1)))
        price = Decimal(str(i.get("price", 0)))
        total += qty * price
    return _two_places(total)


def _two_places(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def create_quote(session_id: str, actor: str, cart_items: list[dict], cart_version: int) -> dict:
    """Create a new active quote for the current cart snapshot."""
    quote_id = uuid.uuid4().hex[:16]
    nonce = uuid.uuid4().hex[:16]
    amount = cart_total(cart_items)
    db = db_module.SessionLocal()
    try:
        row = Quote(
            quote_id=quote_id,
            session_id=session_id,
            actor=actor,
            cart_version=int(cart_version),
            cart_hash=compute_cart_hash(cart_items),
            amount=str(amount),
            currency="INR",
            status="active",
            nonce=nonce,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=QUOTE_TTL_SECONDS),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return to_quote_dict(row)
    finally:
        db.close()


def current_quote(session_id: str) -> dict | None:
    """Most recent active quote for the session, or None."""
    db = db_module.SessionLocal()
    try:
        row = (
            db.query(Quote)
            .filter(Quote.session_id == session_id)
            .order_by(Quote.id.desc())
            .first()
        )
        return to_quote_dict(row) if row else None
    finally:
        db.close()


def get_quote(quote_id: str, session_id: str | None = None) -> dict | None:
    """Look up a quote by its id (optionally scoped to a session)."""
    db = db_module.SessionLocal()
    try:
        q = db.query(Quote).filter(Quote.quote_id == quote_id)
        if session_id:
            q = q.filter(Quote.session_id == session_id)
        row = q.first()
        return to_quote_dict(row) if row else None
    finally:
        db.close()


def to_quote_dict(row) -> dict:
    return {
        "quote_id": row.quote_id,
        "session_id": row.session_id,
        "actor": row.actor,
        "cart_version": row.cart_version,
        "cart_hash": row.cart_hash,
        "amount": row.amount,
        "currency": row.currency,
        "status": row.status,
        "nonce": row.nonce,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "expires_at": row.expires_at.isoformat() if row.expires_at else "",
    }


def invalidate_quote(session_id: str) -> None:
    """Deprecate all open quotes for a session after a cart mutation."""
    db = db_module.SessionLocal()
    try:
        db.query(Quote).filter(
            Quote.session_id == session_id, Quote.status == "active"
        ).update({"status": "superseded"})
        db.commit()
    finally:
        db.close()


def load_session_version(session_id: str, actor: str) -> int:
    """Return the persisted cart version, seeding a record if absent."""
    db = db_module.SessionLocal()
    try:
        row = db.query(SessionState).filter(SessionState.session_id == session_id).first()
        if row is None:
            db.add(SessionState(session_id=session_id, actor=actor, cart_version=0))
            db.commit()
            return 0
        return row.cart_version or 0
    finally:
        db.close()


def bump_cart_version(session_id: str) -> int:
    """Increment + persist the cart version for a session; returns new value."""
    db = db_module.SessionLocal()
    try:
        row = db.query(SessionState).filter(SessionState.session_id == session_id).first()
        if row is None:
            db.add(SessionState(session_id=session_id, actor="unknown", cart_version=1))
            db.commit()
            return 1
        row.cart_version = (row.cart_version or 0) + 1
        db.commit()
        return row.cart_version
    finally:
        db.close()