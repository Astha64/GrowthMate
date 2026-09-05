"""
Compact durable session state (Revision 3) — LLD §4 / HLD §3.5.

Instead of reconstructing agent context from raw conversation history, /chat
persists a small JSON state object per (session_id, actor): structured
requirements, selected product, shown recommendations, offers, quote id, and
cart version. The LLM never sees raw history unless a turn genuinely needs it.

State is process-shared through SQLite; the in-memory chat only carries the
latest user message.
"""

import json

from app import db as db_module
from app.models import SessionState

_DEFAULT = {
    "structured_requirements": None,
    "requirements_complete": False,
    "selected_product": None,
    "merchant_match": None,
    "recommendations": None,
    "offer_candidates": None,
    "checkout_preview": None,
    "quote_id": None,
    "approval_pending": False,
    "last_outcome": None,
}


def load_state(session_id: str, actor: str) -> dict:
    db = db_module.SessionLocal()
    try:
        row = (
            db.query(SessionState)
            .filter(
                SessionState.session_id == session_id,
                SessionState.actor == actor,
            )
            .first()
        )
        if row is None:
            db.add(SessionState(session_id=session_id, actor=actor, cart_version=0, state_json="{}"))
            db.commit()
        else:
            data = json.loads(row.state_json or "{}")
            if data:
                return data
    except Exception:  # noqa: BLE001 — corrupt state becomes a fresh session
        db.rollback()
    return dict(_DEFAULT)


def save_state(session_id: str, actor: str, state: dict) -> None:
    db = db_module.SessionLocal()
    try:
        row = (
            db.query(SessionState)
            .filter(
                SessionState.session_id == session_id,
                SessionState.actor == actor,
            )
            .first()
        )
        payload = json.dumps(state, default=str)
        if row is None:
            db.add(
                SessionState(
                    session_id=session_id,
                    actor=actor,
                    cart_version=state.get("cart_version", 0),
                    state_json=payload,
                )
            )
        else:
            row.state_json = payload
            row.cart_version = state.get("cart_version", row.cart_version or 0)
        db.commit()
    finally:
        db.close()