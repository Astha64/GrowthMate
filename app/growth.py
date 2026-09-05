"""
Growth analytics (Revision 3) — LLD §12.4 feedback loop + HLD §14 demo metrics.

- `offer_stats(base_sku, offer_sku)` returns a smoothed empirical accept
  probability over OfferEvent history (alpha=1, beta=1 priors) and the
  expected incremental revenue for candidate offers.
- `get_growth_insights()` aggregates top products and abandonment from
  orders / cart_events.
"""

from sqlalchemy import func

from app import db as db_module
from app.config import (
    DEFAULT_ACCEPT_PROBABILITY,
    MAX_ADDON_RATIO,
    MAX_OFFERS_PER_TURN,
    MIN_OFFER_CONFIDENCE,
    SMOOTHED_PRIOR_ALPHA,
    SMOOTHED_PRIOR_BETA,
)
from app.models import CartEvent, OfferEvent, OrderItem


def offer_stats(base_sku: str, offer_sku: str) -> dict:
    db = db_module.SessionLocal()
    try:
        shown = (
            db.query(func.count(OfferEvent.id))
            .filter(
                OfferEvent.base_sku == base_sku,
                OfferEvent.offer_sku == offer_sku,
                OfferEvent.shown.is_(True),
            )
            .scalar()
            or 0
        )
        accepted = (
            db.query(func.count(OfferEvent.id))
            .filter(
                OfferEvent.base_sku == base_sku,
                OfferEvent.offer_sku == offer_sku,
                OfferEvent.accepted.is_(True),
            )
            .scalar()
            or 0
        )
        if shown > 0:
            p_accept = (accepted + SMOOTHED_PRIOR_ALPHA) / (shown + SMOOTHED_PRIOR_ALPHA + SMOOTHED_PRIOR_BETA)
        else:
            p_accept = DEFAULT_ACCEPT_PROBABILITY
        return {"shown": int(shown), "accepted": int(accepted), "p_accept": round(p_accept, 4)}
    finally:
        db.close()


def record_offer_event(
    session_id: str,
    base_sku: str,
    offer_sku: str,
    shown: bool = True,
    accepted: bool = False,
    p_accept: float | None = None,
    eir: float | None = None,
) -> None:
    db = db_module.SessionLocal()
    try:
        db.add(
            OfferEvent(
                session_id=session_id,
                base_sku=base_sku,
                offer_sku=offer_sku,
                shown=shown,
                accepted=accepted,
                accept_probability=p_accept,
                expected_incremental_revenue=eir,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()


def offer_policy() -> dict:
    return {
        "max_offers_per_turn": MAX_OFFERS_PER_TURN,
        "max_addon_ratio": MAX_ADDON_RATIO,
        "min_offer_confidence": MIN_OFFER_CONFIDENCE,
    }


def get_growth_insights() -> dict:
    db = db_module.SessionLocal()
    try:
        top = (
            db.query(OrderItem.name, func.sum(OrderItem.quantity))
            .group_by(OrderItem.name)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(5)
            .all()
        )
        top_products = [{"name": n, "units": int(q)} for n, q in top]

        searched = set(
            row[0]
            for row in db.query(CartEvent.ref_id)
            .filter(CartEvent.event_type == "searched")
            .all()
            if row[0]
        )
        purchased = set(
            row[0]
            for row in db.query(CartEvent.ref_id)
            .filter(CartEvent.event_type == "purchased")
            .all()
            if row[0]
        )
        abandoned = list(searched - purchased)
        return {
            "top_products": top_products,
            "abandonment": {"count": len(abandoned), "ref_ids": abandoned},
        }
    finally:
        db.close()