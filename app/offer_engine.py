"""
Next Best Offer Engine (Revision 3) — deterministic expected-revenue steering
(LLD §12, HLD §10).

Given a captured base product + session revenue + available merchant SKUs, pick
the single add-on that maximizes expected incremental revenue under policy caps:

  EIR(offer) = p_accept(offer) * (offer_price - cost(offer))
         where  p_accept = empirical success rate (offer_events, smoothed
                            with Beta(alpha, beta) priors) OR default fallback.

Deterministic caps before candidates are even considered (LLD §12.3):
  - Max add-on price <= MAX_ADDON_RATIO * base price (conversion-preserving)
  - Candidate must be in stock, distinct SKU, not already in the cart
  - Only the single best offer per turn (MAX_OFFERS_PER_TURN == 1)
  - MIN_OFFER_CONFIDENCE gate on the empirical accept probability so a product
    is never pushed on 2/2 successes of a sugar-crash impulse buy
"""

from app.config import MAX_ADDON_RATIO, MAX_OFFERS_PER_TURN, MIN_OFFER_CONFIDENCE
from app.growth import offer_policy, offer_stats
from app.models import CartItem


def candidate_offers(base_product: dict, session_id: str, all_products: list[dict]) -> list[dict]:
    """Deterministic candidate add-ons that satisfy the hard caps."""
    base_sku = str(base_product.get("sku") or "").upper()
    base_price = float(base_product.get("price") or 0.0)

    from app import db as db_module

    db = db_module.SessionLocal()
    try:
        rows = db.query(CartItem).filter(CartItem.session_id == session_id).all()
    finally:
        db.close()
    cart_rows = {r.ref_id for r in rows}

    candidates: list[dict] = []
    for p in all_products:
        sku = str(p.get("sku") or "").upper()
        if sku == base_sku or sku in cart_rows:
            continue
        price = float(p.get("price") or 0.0)
        if price <= 0 or price > MAX_ADDON_RATIO * base_price:
            continue
        if int(p.get("stock") or 0) <= 0:
            continue
        candidates.append(
            {
                "sku": sku,
                "name": p.get("name"),
                "price": price,
                "currency": p.get("currency", "INR"),
                "stock": p.get("stock"),
            }
        )
    return candidates


def best_offer(base_product: dict, session_id: str, all_products: list[dict]) -> dict | None:
    """Pick the single best-scoring add-on per the policy, or None."""
    offers = candidate_offers(base_product, session_id, all_products)
    if not offers:
        return None

    base_sku = str(base_product.get("sku") or "").upper()
    scored: list[dict] = []
    for offer in offers:
        stats = offer_stats(base_sku, offer["sku"])
        p_accept = stats["p_accept"]
        if p_accept < MIN_OFFER_CONFIDENCE:
            continue
        price = float(offer["price"])
        cost = float(offer.get("unit_cost") or (price * 0.7))
        eir = p_accept * (price - cost)
        scored.append(
            {
                **offer,
                "p_accept": round(p_accept, 4),
                "expected_incremental_revenue": round(eir, 2),
                "_score": round(eir, 2),
            }
        )

    if not scored:
        return None
    scored.sort(key=lambda s: s["_score"], reverse=True)
    return scored[0]


def select_offers(base_product: dict, session_id: str, all_products: list[dict]) -> dict:
    """Public engine entry: returns the ranked offer list (<= policy cap)."""
    policy = offer_policy()
    offer = best_offer(base_product, session_id, all_products)
    selected = [offer] if offer else []
    return {
        "max_offers_per_turn": policy["max_offers_per_turn"],
        "max_addon_ratio": policy["max_addon_ratio"],
        "count": len(selected),
        "offers": selected,
    }