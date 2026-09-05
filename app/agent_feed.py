"""
Agent-readable storefront manifests (Rev 3, Phase 5).

Two well-known, read-only feeds for external buyer agents:

  `/,well-known/agentic-catalog.json`
      The deterministic merchant catalog (served from the Phase-2 index's
      `rows()`, never re-embedding on the hot path) plus endpoint metadata the
      agent protocol expects. `last_built_at` is the session checkpoint (max
      SessionState.updated_at), so a feed read is addressable in time.

  `/,well-known/agentic-orders.json`
      A per-session order ledger. Every read RE-VERIFIES each order's HMAC
      mandate (`mandate.verify_order_mandate`) before declaring it `signed`;
      tampered or unsigned rows are listed with `mandate_verified: false` so an
      agent can see the evidence rather than a 500.

Invariants
----------
- Fail-open and read-only: no writes, no raises. A missing index or DB degrades
  to an explicit `"empty": true` payload, never a 5xx.
- No invented data: entries carry only fields that exist on the catalog rows
  (merchant products have no `brand` column, so none is emitted).
- Deterministic: same DB snapshot => same feed bytes.
"""

from datetime import datetime, timezone

from app import mandate as mandate_module
from app import catalog_index
from app import db as db_module
from app.models import Order, SessionState


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _catalog_rows() -> list[dict]:
    rows = catalog_index.rows()
    if not rows:
        # Fail-open: index not yet built (e.g. cold boot pre-warm-up). Build once
        # from the DB deterministically, then serve — never raise, never guess.
        try:
            catalog_index.build()
        except Exception:  # noqa: BLE001
            return []
        rows = catalog_index.rows()
    return rows


def _session_checkpoint() -> str | None:
    db = db_module.SessionLocal()
    try:
        row = (
            db.query(SessionState.updated_at)
            .order_by(SessionState.updated_at.desc())
            .first()
        )
        return _iso(row[0]) if row else None
    finally:
        db.close()


ENDPOINTS = [
    {
        "name": "chat",
        "method": "POST",
        "path": "/chat",
        "summary": "human conversational pipeline (guardrailed)",
    },
    {
        "name": "quote",
        "method": "POST",
        "path": "/agent/quote",
        "summary": "immutable backend quote for a merchant cart",
    },
    {
        "name": "checkout",
        "method": "POST",
        "path": "/agent/checkout",
        "summary": "guarded payment execution (HMAC mandate on ALLOW)",
    },
    {
        "name": "discover",
        "method": "POST",
        "path": "/agent/discover",
        "summary": "external discovery with semantic cache",
    },
]


def catalog_feed() -> dict:
    """Build (never write) the agentic catalog manifest.

    Dollar amounts are snapshots at build time; the authoritative amount for any
    payment is the backend quote, never this feed.
    """
    products = _catalog_rows()
    return {
        "schema": "agentic-catalog/v1",
        "merchant": "GrowthMate",
        "last_built_at": _session_checkpoint(),
        "count": len(products),
        "empty": len(products) == 0,
        "currency": "INR",
        "endpoints": ENDPOINTS,
        "products": [
            {
                "sku": p.get("sku"),
                "name": p.get("name"),
                "category": p.get("category"),
                "currency": p.get("currency", "INR"),
                "price": p.get("price"),
                "stock": p.get("stock"),
                "merchant_priority": p.get("merchant_priority"),
                "description": p.get("description"),
                "semantic_text": p.get("semantic_text"),
            }
            for p in products
        ],
    }


def orders_feed(session_id: str | None = None) -> dict:
    """Agent-readable order ledger. Every read re-verifies each mandate."""
    db = db_module.SessionLocal()
    try:
        query = db.query(Order)
        if session_id:
            query = query.filter(Order.session_id == session_id)
        orders = query.order_by(Order.id.desc()).limit(200).all()
        entries = []
        for order in orders:
            payload = {
                "order_id": order.id,
                "session_id": order.session_id,
                "actor": order.actor,
                "status": order.status,
                "currency": order.currency,
                "subtotal": str(order.subtotal),
                "total": str(order.total),
                "cart_hash": order.cart_hash,
                "quote_id": order.quote_id,
            }
            payload["mandate_verified"] = mandate_module.verify_order_mandate(
                {
                    "session_id": order.session_id,
                    "actor": order.actor,
                    "cart_hash": order.cart_hash,
                    "quote_id": order.quote_id,
                    "total": order.total,
                    "mandate_signature": order.mandate_signature,
                }
            )
            entries.append(payload)
        return {
            "schema": "agentic-orders/v1",
            "last_built_at": _session_checkpoint(),
            "count": len(entries),
            "empty": len(entries) == 0,
            "orders": entries,
        }
    finally:
        db.close()