"""
Cascade router behavioural tests (Rev 3, Phase 1).

Covers the tiered fast paths end-to-end through /chat: tier-0 cart/tool
short-circuits, tier-1 first-turn discover grading, tier-2 cached
no-merchant-match short-circuit, tier-3 parity with CASCADE_ENABLED=false, and
the cascade audit trail (event_type=cascade_route + tier_used).
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import cascade_router
from app import discovery
from app.main import app
from app.models import AuditLog, Product

client = TestClient(app)

# These fast-path tests only apply while the cascade is enabled; under
# CASCADE_ENABLED=false every turn takes the Tier-3 pipeline by design.
requires_cascade = pytest.mark.skipif(
    not cascade_router.CASCADE_ENABLED,
    reason="cascade fast paths require CASCADE_ENABLED=true",
)

APP_001 = dict(sku="APP-001", name="Cotton Crew T-Shirt", price=499.0, currency="INR",
               stock=50, category="apparel", description="premium cotton crew neck t-shirt",
               merchant_priority=0.8, semantic_text="cotton crew t-shirt apparel")
ACC_001 = dict(sku="ACC-001", name="Leather Wallet", price=899.0, currency="INR",
               stock=22, category="accessories", description="bifold leather wallet",
               merchant_priority=0.7, semantic_text="leather wallet bifold accessories")
SHOE_001 = dict(sku="SHOE-001", name="Nike Revolution 6", price=1899.0, currency="INR",
                stock=25, category="footwear", description="running shoe for road",
                merchant_priority=0.9, semantic_text="nike revolution running shoe footwear")
SHOE_002 = dict(sku="SHOE-002", name="Adidas Duramo SL", price=2499.0, currency="INR",
                stock=18, category="footwear", description="running shoes",
                merchant_priority=0.8, semantic_text="adidas duramo running shoe footwear")


def _seed(db_factory, *products):
    s = db_factory()
    s.add_all([Product(**p) for p in (products or (APP_001, ACC_001))])
    s.commit()
    s.close()


def _cascade_audits(db_factory, session_id):
    s = db_factory()
    rows = (
        s.query(AuditLog)
        .filter(AuditLog.session_id == session_id, AuditLog.event_type == "cascade_route")
        .all()
    )
    out = [
        {
            "decision": r.decision,
            "tier": r.tier_used,
            "params": json.loads(r.parameters_json or "{}"),
        }
        for r in rows
    ]
    s.close()
    return out


def _chat_turn_tiers(db_factory, session_id):
    s = db_factory()
    rows = (
        s.query(AuditLog)
        .filter(AuditLog.session_id == session_id, AuditLog.event_type == "chat_turn")
        .all()
    )
    out = [(r.tier_used, r.outcome, r.decision) for r in rows]
    s.close()
    return out


# ---------------------------------------------------------------------------
# Tier 0 — fast paths through /chat.
# ---------------------------------------------------------------------------

@requires_cascade
def test_greeting_fastpaths_without_pipeline(db_session_factory, monkeypatch):
    _seed(db_session_factory)

    def boom(req):
        raise AssertionError("discovery must not run on a greeting")

    monkeypatch.setattr(discovery, "search_external_sources", boom)
    r = client.post("/chat", json={"session_id": "c-greet", "actor": "human", "message": "hi"})
    body = r.json()
    assert body["blocked"] is False
    assert body["reply"].startswith("Hi!")
    assert body["tool_calls_made"] == []
    # Greeting is a Tier-0 FAST cascade decision, and the per-turn row records
    # the tier used.
    audits = _cascade_audits(db_session_factory, "c-greet")
    assert audits and audits[-1]["decision"] == "FAST"
    assert audits[-1]["tier"] == 0
    assert "greeting" in audits[-1]["params"]["reason_codes"]
    assert _chat_turn_tiers(db_session_factory, "c-greet")[-1][0] == 0


@requires_cascade
def test_catalog_browse_fastpaths(db_session_factory, monkeypatch):
    _seed(db_session_factory)

    def boom(req):
        raise AssertionError("discovery must not run for a catalog browse")

    monkeypatch.setattr(discovery, "search_external_sources", boom)
    r = client.post("/chat", json={"session_id": "c-browse", "actor": "human",
                                   "message": "what do you sell"})
    body = r.json()
    assert "APP-001" in body["reply"]
    assert "catalog" in body["reply"].lower()
    audits = _cascade_audits(db_session_factory, "c-browse")
    assert audits[-1]["params"]["intent"] == "catalog"


@requires_cascade
def test_add_sku_fastpath_preserves_cross_sell(db_session_factory, monkeypatch):
    """The Tier-0 add path must produce the exact same reply + cross-sell the
    full pipeline would (tool_node is shared, so behaviour must not drift)."""
    _seed(db_session_factory, APP_001)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    r = client.post("/chat", json={"session_id": "c-add", "actor": "human",
                                   "message": "add APP-001"})
    body = r.json()
    assert body["blocked"] is False
    assert "APP-001" in body["reply"]
    assert "add_APP-001" in body["tool_calls_made"]
    audits = _cascade_audits(db_session_factory, "c-add")
    assert audits[-1]["decision"] == "FAST"
    assert audits[-1]["params"]["action"] == "('add', 'APP-001')"
    assert _chat_turn_tiers(db_session_factory, "c-add")[-1][0] == 0


@requires_cascade
def test_checkout_fastpath_creates_preview_only(db_session_factory, monkeypatch):
    """'checkout' fast-paths to a PREVIEW (never execution), the same as the
    pipeline's checkout action."""
    _seed(db_session_factory, APP_001)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr("app.commerce.rzp_create_payment_link",
                        lambda o: {"error": "must never be called on a preview"})
    client.post("/chat", json={"session_id": "c-co", "actor": "human", "message": "add APP-001"})
    r = client.post("/chat", json={"session_id": "c-co", "actor": "human", "message": "checkout"})
    body = r.json()
    assert body["blocked"] is False
    assert "Checkout preview" in body["reply"]
    # No Order was created by the fast-path preview.
    s = db_session_factory()
    from app.models import Order
    n = s.query(Order).count()
    s.close()
    assert n == 0
    audits = _cascade_audits(db_session_factory, "c-co")
    assert audits[-1]["decision"] == "FAST"
    assert audits[-1]["params"]["action"] == "checkout"


def test_approval_turn_falls_through_and_pays(db_session_factory, monkeypatch):
    """The money path must remain fully reachable through the cascade: after a
    fast-path preview, the approval message is payment-adjacent and falls
    through to approval -> guardrail -> pay."""
    _seed(db_session_factory, APP_001)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr(
        "app.commerce.rzp_create_payment_link",
        lambda o: {"short_url": "https://rzp.test/cascade", "id": "plink_c", "order_id": "ord_c"},
    )
    client.post("/chat", json={"session_id": "c-pay", "actor": "human", "message": "add APP-001"})
    client.post("/chat", json={"session_id": "c-pay", "actor": "human", "message": "checkout"})
    r = client.post("/chat", json={"session_id": "c-pay", "actor": "human", "message": "Yes proceed"})
    body = r.json()
    assert body["blocked"] is False
    assert "rzp.test" in body["reply"]
    s = db_session_factory()
    from app.models import Order
    assert s.query(Order).count() == 1
    s.close()
    audits = _cascade_audits(db_session_factory, "c-pay")
    # add+checkout were FAST; the approval was a FALLTHROUGH into the pipeline.
    assert audits[-1]["decision"] == "FALLTHROUGH"
    assert "payment_adjacent_guard" in audits[-1]["params"]["reason_codes"]
    assert _chat_turn_tiers(db_session_factory, "c-pay")[-1][0] == 0


# ---------------------------------------------------------------------------
# Tier 1 — first-turn discover grading.
# ---------------------------------------------------------------------------

@requires_cascade
def test_first_turn_discover_runs_full_pipeline(db_session_factory, monkeypatch):
    """Tier 1 resolves intent but must still fall through so merchant capture
    and ranking produce the SAME ranked shortlist as Rev-2."""
    _seed(db_session_factory, SHOE_001, SHOE_002)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    r = client.post("/chat", json={"session_id": "c-tier1", "actor": "human",
                                   "message": "running shoes"})
    body = r.json()
    assert body["blocked"] is False
    assert "best options" in body["reply"].lower()
    audits = _cascade_audits(db_session_factory, "c-tier1")
    assert audits[-1]["decision"] == "FALLTHROUGH"
    assert audits[-1]["tier"] == 1
    assert "tier1_resolved" in audits[-1]["params"]["reason_codes"]
    assert audits[-1]["params"]["intent"] == "discover"
    assert _chat_turn_tiers(db_session_factory, "c-tier1")[-1][0] == 1


def test_selection_pick_is_not_fastpathed(db_session_factory, monkeypatch):
    """A ranked-shortlist pick ('2') must reach the agent's deterministic
    selection logic — never a cached or direct reply."""
    _seed(db_session_factory, SHOE_001, SHOE_002)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    client.post("/chat", json={"session_id": "c-pick", "actor": "human", "message": "running shoes"})
    r = client.post("/chat", json={"session_id": "c-pick", "actor": "human", "message": "2"})
    body = r.json()
    assert body["blocked"] is False
    assert "Added" in body["reply"]
    s = db_session_factory()
    from app.models import CartItem
    rows = s.query(CartItem).filter(CartItem.session_id == "c-pick").all()
    s.close()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tier 2 — cached no-merchant-match short-circuit.
# ---------------------------------------------------------------------------

@requires_cascade
def test_tier2_cached_no_match_shortcircuit(db_session_factory, monkeypatch):
    """A cached, fresh, similar result for a query the merchant can't fulfill is
    answered directly (no capture pipeline, no external call) with the same
    copy the pipeline would produce, audited as a Tier-2 FAST decision."""
    _seed(db_session_factory, APP_001)  # merchant stock: only a t-shirt
    monkeypatch.setattr(discovery, "search_external_sources",
                        lambda req: (_ for _ in ()).throw(
                            AssertionError("external search must not run on a fast path")))
    from app import semantic_cache

    req = {
        "category": None, "product_type": None, "budget": None,
        "keywords": ["wireless", "mouse"], "required_features": ["wireless"],
        "brand": None, "explicit_item": ["mouse"],
    }
    semantic_cache.put(req, [{"name": "Logitech G Pro Gaming Mouse", "price": 2500.0,
                              "currency": "INR", "source": "https://m.test/x",
                              "why": "wireless gaming mouse"}])

    # First turn spends a non-discover message so the follow-up is non-first.
    client.post("/chat", json={"session_id": "c-tier2", "actor": "human",
                               "message": "add APP-001"})
    r = client.post("/chat", json={"session_id": "c-tier2", "actor": "human",
                                   "message": "wireless mouse"})
    body = r.json()
    assert body["blocked"] is False
    assert "market shows" in body["reply"]
    assert "Logitech" in body["reply"]

    audits = _cascade_audits(db_session_factory, "c-tier2")
    last = audits[-1]
    assert last["decision"] == "FAST"
    assert last["tier"] == 2
    assert "tier2_cache_hit" in last["params"]["reason_codes"]
    # The per-turn audit row records the cache hit + tier.
    s = db_session_factory()
    row = (s.query(AuditLog)
           .filter(AuditLog.session_id == "c-tier2", AuditLog.event_type == "chat_turn")
           .order_by(AuditLog.id.desc()).first())
    s.close()
    assert row.cache_hit is True
    assert row.tier_used == 2
    # Nothing was added to the cart yet — the user saw the EXT-xxx market
    # options but hasn't picked one.
    s = db_session_factory()
    from app.models import CartItem
    cart = [c.ref_id for c in s.query(CartItem).filter(CartItem.session_id == "c-tier2").all()]
    s.close()
    assert cart == ["APP-001"]
    # The tier-2 no-match reply surfaced a selectable EXT-001 (Rev 3).
    assert "EXT-001" in body["reply"]


@requires_cascade
def test_cached_match_is_not_shortcircuited_when_merchant_can_pay(db_session_factory, monkeypatch):
    """When merchant capture HAS a real match (the phrase names the product and
    its merchant card keeps it), the cascade must fall through so the pipeline
    shows the payable match — even if the semantic cache has results. (A phrase
    the pipeline itself classifies as no-merchant-match stays fast-pathable —
    same reply either way, so that's just a latency win.)"""
    _seed(db_session_factory, SHOE_001)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    from app import semantic_cache

    req = {
        "category": None, "product_type": None, "budget": None,
        "keywords": ["nike", "revolution"], "required_features": ["nike", "revolution"],
        "brand": None, "explicit_item": [],
    }
    semantic_cache.put(req, [{"name": "Some External Shoe", "price": 999.0,
                              "currency": "INR", "source": "https://m.test/shoe",
                              "why": "generic running shoe"}])
    client.post("/chat", json={"session_id": "c-t2pay", "actor": "human",
                               "message": "add SHOE-001"})
    r = client.post("/chat", json={"session_id": "c-t2pay", "actor": "human",
                                   "message": "Nike Revolution"})
    body = r.json()
    assert body["blocked"] is False
    # The merchant match must win — not the cached external listing.
    assert "market shows" not in body["reply"]
    assert "Nike Revolution" in body["reply"]
    audits = _cascade_audits(db_session_factory, "c-t2pay")
    assert audits[-1]["tier"] == 2
    assert audits[-1]["decision"] == "FALLTHROUGH"
    assert "tier2_capture_found" in audits[-1]["params"]["reason_codes"]


# ---------------------------------------------------------------------------
# Tier 3 parity — CASCADE_ENABLED=false reproduces Rev-2.
# ---------------------------------------------------------------------------

def test_disabled_cascade_replies_match_enabled(db_session_factory, monkeypatch):
    """Toggling the cascade off must not change ANY user-visible reply: the
    fall-through path is literally the same pipeline Rev-2 ran."""
    _seed(db_session_factory, SHOE_001, SHOE_002)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    def turn(sid, msg):
        r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": msg})
        return r.json()

    # Enabled: discovery + add.
    r_on = turn("c-on", "running shoes")
    turn("c-on", "1")
    # Disabled: everything delegates to the pipeline.
    monkeypatch.setattr(cascade_router, "CASCADE_ENABLED", False)
    r_off = turn("c-off", "running shoes")
    turn("c-off", "1")

    assert r_on["reply"] == r_off["reply"]
    assert r_on["blocked"] == r_off["blocked"]

    # Cart listing via the fast path must match the disable-toggled pipeline.
    monkeypatch.setattr(cascade_router, "CASCADE_ENABLED", True)
    r_on2 = turn("c-on2", "show my cart")
    monkeypatch.setattr(cascade_router, "CASCADE_ENABLED", False)
    r_off2 = turn("c-off2", "show my cart")
    assert r_on2["reply"] == r_off2["reply"]