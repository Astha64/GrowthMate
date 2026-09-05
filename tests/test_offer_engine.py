"""
Growth + Next-Best-Offer engine tests (LLD §12). Deterministic policy caps and
Beta-smoothed acceptance. Uses the in-memory DB fixture for OfferEvent/CartEvent.
"""

from app import offer_engine, growth
from app.config import DEFAULT_ACCEPT_PROBABILITY, MAX_ADDON_RATIO, MIN_OFFER_CONFIDENCE


def _base_product():
    return {"sku": "ACC-001", "name": "Leather Wallet", "price": 899.0, "currency": "INR", "stock": 22}


def _catalog():
    return [
        _base_product(),
        # cheap add-on within 20% of 899 -> eligible
        {"sku": "APP-001", "name": "Cotton Crew T-Shirt", "price": 160.0, "currency": "INR", "stock": 50},
        # over the add-on ratio (0.20 * 899 = 179.8 max) -> ineligible
        {"sku": "SHOE-001", "name": "Running Shoe", "price": 1899.0, "currency": "INR", "stock": 20},
        # out of stock -> ineligible
        {"sku": "BAG-001", "name": "Backpack", "price": 150.0, "currency": "INR", "stock": 0},
    ]


def test_addon_ratio_cap_filters_expensive():
    base, catalog = _base_product(), _catalog()
    candidates = offer_engine.candidate_offers(base, "sess-offers", catalog)
    skus = {c["sku"] for c in candidates}
    assert "APP-001" in skus          # within ratio
    assert "SHOE-001" not in skus      # over ratio
    assert "BAG-001" not in skus       # no stock
    assert "ACC-001" not in skus       # same SKU excluded


def test_backpack_excluded_by_stock():
    base = _base_product()
    catalog = [
        {"sku": "BAG-001", "name": "Backpack", "price": 150.0, "currency": "INR", "stock": 0},
    ]
    assert offer_engine.candidate_offers(base, "sess-x", catalog) == []


def test_no_candidates_returns_none():
    base = _base_product()
    assert offer_engine.best_offer(base, "sess-zz", [base]) is None


def test_default_accept_probability_used_when_no_history(db_session_factory):
    # Tie to the isolated DB so there is genuinely no OfferEvent history.
    stats = growth.offer_stats("ACC-001", "APP-001")
    assert stats["shown"] == 0
    assert stats["p_accept"] == DEFAULT_ACCEPT_PROBABILITY


def test_record_event_updates_stats(db_session_factory):
    growth.record_offer_event("sess-g", "ACC-001", "APP-001", shown=True, accepted=True)
    stats = growth.offer_stats("ACC-001", "APP-001")
    assert stats["shown"] == 1
    assert stats["accepted"] == 1
    # Beta smoothing: (1 + alpha) / (1 + alpha + beta), alpha=beta=1 -> 2/3
    assert abs(stats["p_accept"] - (2.0 / 3.0)) < 1e-4


def test_low_confidence_offer_rejected(db_session_factory):
    base = _base_product()
    catalog = [{"sku": "APP-001", "name": "Tee", "price": 150.0, "currency": "INR", "stock": 10}]

    # Force a very low empirical accept (1 shown, 0 accepted) -> p_accept below
    # the MIN_OFFER_CONFIDENCE gate, so best_offer returns None.
    growth.record_offer_event("sess-low", "ACC-001", "APP-001", shown=True, accepted=False)
    best = offer_engine.best_offer(base, "sess-low", catalog)
    assert best is None


def test_select_offers_caps_at_one_per_turn():
    base, catalog = _base_product(), _catalog()
    out = offer_engine.select_offers(base, "sess-cap", catalog)
    assert out["max_offers_per_turn"] == 1
    assert len(out["offers"]) <= 1
    assert out["count"] == len(out["offers"])


def test_offer_policy_exposes_caps():
    policy = offer_engine.offer_policy()  # growth.offer_policy re-exported
    assert policy["max_offers_per_turn"] >= 1
    assert policy["max_addon_ratio"] == MAX_ADDON_RATIO
    assert policy["min_offer_confidence"] == MIN_OFFER_CONFIDENCE