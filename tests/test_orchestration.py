"""
End-to-end orchestration + agent-commerce API tests (LLD §21-§25).

Uses the static in-memory DB fixture for the shared test DB and monkeypatches
the Razorpay wrapper + external discovery so the suite needs no keys/network.
Monkeypatching `app.commerce.rzp_create_payment_link` (aliased at import time
as `commerce.rzp_create_payment_link`) covers the human chat's pay_node and
the /agent/checkout executor. `app.discovery.search_external_sources` covers
the offline external-discovery fallback path.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app import discovery
from app.main import app
from app.models import AuditLog, Order, Product

client = TestClient(app)


APP_001 = dict(sku="APP-001", name="Cotton Crew T-Shirt", price=499.0, currency="INR",
               stock=50, category="apparel", description="premium cotton crew neck t-shirt",
               merchant_priority=0.8, semantic_text="cotton crew t-shirt apparel")
ACC_001 = dict(sku="ACC-001", name="Leather Wallet", price=899.0, currency="INR",
               stock=22, category="accessories", description="bifold leather wallet",
               merchant_priority=0.7, semantic_text="leather wallet bifold accessories")
HOME_001 = dict(sku="HOME-001", name="Ceramic Coffee Mug", price=199.0, currency="INR",
                stock=40, category="home", description="ceramic coffee mug",
                merchant_priority=0.6, semantic_text="ceramic coffee mug home")
SHOE_001 = dict(sku="SHOE-001", name="Nike Revolution 6", price=1899.0, currency="INR",
                stock=25, category="footwear", description="running shoe for road",
                merchant_priority=0.9, semantic_text="nike revolution running shoe footwear")
SHOE_002 = dict(sku="SHOE-002", name="Adidas Duramo SL", price=2499.0, currency="INR",
                stock=18, category="footwear", description="running shoes",
                merchant_priority=0.8, semantic_text="adidas duramo running shoe footwear")
ELEC_001 = dict(sku="ELEC-001", name="Wireless Earbuds", price=1999.0, currency="INR",
                stock=30, category="electronics", description="true wireless earbuds",
                merchant_priority=0.8, semantic_text="wireless earbuds bluetooth electronics")
ELEC_002 = dict(sku="ELEC-002", name="USB-C Fast Charger", price=1499.0, currency="INR",
                stock=20, category="electronics", description="65w usb-c gan charger",
                merchant_priority=0.7, semantic_text="usb c fast charger electronics")


def _seed(db_factory):
    s = db_factory()
    s.add_all([Product(**p) for p in (APP_001, ACC_001)])
    s.commit()
    s.close()


def _seed_with_home(db_factory):
    _seed(db_factory)
    s = db_factory()
    s.add(Product(**HOME_001))
    s.commit()
    s.close()


def _seed_shoes(db_factory):
    s = db_factory()
    s.add_all([Product(**p) for p in (SHOE_001, SHOE_002)])
    s.commit()
    s.close()


def _seed_electronics(db_factory):
    s = db_factory()
    s.add_all([Product(**p) for p in (ELEC_001, ELEC_002)])
    s.commit()
    s.close()


def _audit_types(db_factory, session_id):
    s = db_factory()
    rows = s.query(AuditLog).filter(AuditLog.session_id == session_id).all()
    s.close()
    return [(r.event_type, r.decision, r.outcome) for r in rows]


def _order_count(db_factory):
    s = db_factory()
    n = s.query(Order).count()
    s.close()
    return n


# ---------------------------------------------------------------------------
# /chat — add -> checkout -> approve -> pay (Razorpay mocked to succeed).
# ---------------------------------------------------------------------------

def test_chat_merchant_journey_pays_when_approved(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    sid = "chat-journey"
    # 1) add a merchant item.
    r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": "add APP-001"})
    assert r.status_code == 200
    body = r.json()
    assert body["blocked"] is False
    assert "APP-001" in body["reply"]
    assert "add_APP-001" in body["tool_calls_made"]

    # 2) show checkout -> creates a quote + preview.
    r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": "checkout"})
    body = r.json()
    assert body["blocked"] is False
    assert "Checkout preview" in body["reply"]
    assert "APP-001" in body["reply"]

    # 3) approval + payment. Mock Razorpay to succeed.
    monkeypatch.setattr(
        "app.commerce.rzp_create_payment_link",
        lambda order: {"short_url": "https://rzp.test/pay", "id": "plink_1", "order_id": "ord_1"},
    )
    r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": "Yes proceed"})
    body = r.json()
    assert body["blocked"] is False
    assert "rzp.test" in body["reply"]

    # Exactly one Order for this journey.
    assert _order_count(db_session_factory) == 1
    s = db_session_factory()
    order = s.query(Order).first()
    order_status = order.status
    s.close()
    assert order_status == "created"

    # Approval + payment audited.
    events = _audit_types(db_session_factory, sid)
    assert ("chat_turn", "ALLOW", "success") in events


def test_chat_blocks_without_explicit_approval(db_session_factory, monkeypatch):
    """Missing preview or non-affirmative reply must never pay."""
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr("app.commerce.rzp_create_payment_link", lambda o: {"error": "should not build link"})

    sid = "chat-noapprove"
    client.post("/chat", json={"session_id": sid, "actor": "human", "message": "add APP-001"})
    client.post("/chat", json={"session_id": sid, "actor": "human", "message": "checkout"})

    # Approve without a fresh preview is impossible here because approve_checkout
    # requires the preview. Send a non-affirmative reply instead.
    r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": "not sure"})
    body = r.json()
    assert body["blocked"] is False  # routed to summary, not payment
    assert _order_count(db_session_factory) == 0


def test_chat_add_disambiguates_merchant_sku(db_session_factory, monkeypatch):
    """ACC-001 must not be mis-resolved to APP-001 — full SKU tokens map
    exactly to their own merchant product."""
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    sid = "chat-sku"
    r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": "add ACC-001"})
    body = r.json()
    assert body["blocked"] is False
    assert "ACC-001" in body["reply"]
    s = db_session_factory()
    from app.models import CartItem
    rows = s.query(CartItem).filter(CartItem.session_id == sid).all()
    s.close()
    assert [r.ref_id for r in rows] == ["ACC-001"]


def test_chat_explicit_offcatalog_item_is_not_forcefit(db_session_factory, monkeypatch):
    """"Wireless mouse" must NOT be force-fit to a same-category merchant product
    the user did not ask for; it degrades to external discovery, whose market
    listings (EXT-xxx) ARE surfaced as selectable, addable options (Rev 3)."""
    _seed(db_session_factory)
    listings = [{"name": "Logitech G Pro Wireless Gaming Mouse", "price": 2500.0,
                 "currency": "INR", "source": "https://m.test/mouse",
                 "features": ["wireless"], "why": "gaming mouse within budget"}]
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: listings)

    sid = "chat-mouse"
    r = client.post("/chat", json={"session_id": sid, "actor": "human",
                                   "message": "wireless mouse for gaming under 3000"})
    body = r.json()
    assert body["blocked"] is False
    # Merchant capture must NOT present an unrelated earbuds/wallet fit.
    assert "APP-001" not in body["reply"] and "ACC-001" not in body["reply"]
    assert "market shows" in body["reply"]
    # The external listing is surfaced as a selectable EXT-001 option.
    assert "EXT-001" in body["reply"]
    # Nothing lands in the cart until the user actually picks/adds it.
    s = db_session_factory()
    from app.models import CartItem
    rows = s.query(CartItem).filter(CartItem.session_id == sid).all()
    s.close()
    assert [r.ref_id for r in rows] == []


def test_chat_empty_cart_introspection_is_not_confusing_total(db_session_factory, monkeypatch):
    """'show cart' on an empty cart must explain the cart is empty, not answer
    with the bare jarring 'Your current cart total is ₹0.00 (0 item(s)).'."""
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    sid = "chat-empty-cart"
    r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": "show my cart"})
    body = r.json()
    assert body["blocked"] is False
    assert "cart is empty" in body["reply"].lower()
    assert body["reply"].startswith("Your current cart total") is False


def test_chat_product_price_question_is_not_hijacked_to_cart_total(db_session_factory, monkeypatch):
    """"how much is running shoes" is a PRODUCT price question. It must reach
    merchant discovery, NOT the cart-total handler (which wrongly answered the
    empty cart's ₹0.00 total)."""
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    sid = "chat-price-q"
    r = client.post("/chat", json={"session_id": sid, "actor": "human",
                                   "message": "how much is running shoes"})
    body = r.json()
    assert body["blocked"] is False
    assert "cart total" not in body["reply"].lower()
    assert "merchant match" in body["reply"].lower()


def test_chat_natural_language_catalog_add_asks_confirmation(db_session_factory, monkeypatch):
    """"put the mug in my cart" is a catalog-item request with no explicit SKU.
    The catalog-keyword add path must CONFIRM before anything enters the cart."""
    _seed_with_home(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    sid = "chat-nl-add"
    r = client.post("/chat", json={"session_id": sid, "actor": "human",
                                   "message": "put the mug in my cart"})
    body = r.json()
    assert body["blocked"] is False
    assert "Should I go ahead" in body["reply"]
    assert "HOME-001" in body["reply"] or "Ceramic Coffee Mug" in body["reply"]

    s = db_session_factory()
    from app.models import CartItem
    rows = s.query(CartItem).filter(CartItem.session_id == sid).all()
    s.close()
    assert [r.ref_id for r in rows] == []  # nothing added without approval


def test_chat_natural_language_add_confirmation_adds_item(db_session_factory, monkeypatch):
    """After a catalog-keyword confirmation, an affirmative reply adds the item."""
    _seed_with_home(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    sid = "chat-nl-add-ok"
    client.post("/chat", json={"session_id": sid, "actor": "human",
                               "message": "get me the mug"})
    r = client.post("/chat", json={"session_id": sid, "actor": "human",
                                   "message": "yes add it"})
    body = r.json()
    assert body["blocked"] is False
    assert "HOME-001" in body["reply"]

    s = db_session_factory()
    from app.models import CartItem
    rows = s.query(CartItem).filter(CartItem.session_id == sid).all()
    s.close()
    assert [r.ref_id for r in rows] == ["HOME-001"]


def test_chat_llm_intent_rescues_clear_cart(db_session_factory, monkeypatch):
    """Phrasing the deterministic router cannot parse falls back to the Gemini
    intent rescue. A clear_cart intent empties the cart deterministically."""
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr("app.router.extract_intent_llm",
                        lambda message: {"intent": "clear_cart", "product": None,
                                         "category": None, "quantity": None})

    sid = "chat-llm-clear"
    client.post("/chat", json={"session_id": sid, "actor": "human", "message": "add APP-001"})
    r = client.post("/chat", json={"session_id": sid, "actor": "human",
                                   "message": "could you kindly take every item off my list please"})
    body = r.json()
    assert body["blocked"] is False
    assert "Emptied your cart" in body["reply"]


def test_chat_ranked_shortlist_offers_top_options(db_session_factory, monkeypatch):
    """"running shoes" returns a ranked top-N shortlist with fit %; the shopper
    picks by number and only the picked SKU lands in the cart."""
    _seed_shoes(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    sid = "chat-ranked"
    r = client.post("/chat", json={"session_id": sid, "actor": "human",
                                   "message": "running shoes"})
    body = r.json()
    assert body["blocked"] is False
    assert "best options" in body["reply"].lower()
    assert "1)" in body["reply"] and "2)" in body["reply"]
    assert "% fit" in body["reply"]

    second_sku = re.search(r"^\s*2\) .*?\((SHOE-\d+)\)", body["reply"], re.M).group(1)
    r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": "2"})
    body = r.json()
    assert body["blocked"] is False
    assert "Added" in body["reply"]

    s = db_session_factory()
    from app.models import CartItem
    rows = s.query(CartItem).filter(CartItem.session_id == sid).all()
    s.close()
    assert [r.ref_id for r in rows] == [second_sku]


def test_chat_cross_sell_suggests_catalog_complement(db_session_factory, monkeypatch):
    """Adding earbuds surfaces a payable merchant complement (the charger)."""
    _seed_electronics(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    sid = "chat-xsell"
    r = client.post("/chat", json={"session_id": sid, "actor": "human",
                                   "message": "add ELEC-001"})
    body = r.json()
    assert body["blocked"] is False
    assert "ELEC-002" in body["reply"]
    assert "pairs well" in body["reply"]


def test_agent_discover_returns_payable_merchant_candidates(db_session_factory, monkeypatch):
    """Automated buyers get a ranked, payable merchant shortlist — not just
    reference-only external listings — so they can select and purchase."""
    _seed_shoes(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    r = client.post("/agent/discover", json={"session_id": "ag-shoes", "actor": "buyer_agent",
                                             "query": "running shoes"})
    body = r.json()
    assert r.status_code == 200
    best = body["merchant_candidates"] or []
    assert len(best) >= 2
    assert best[0]["payable"] is True
    assert best[0]["sku"].startswith("SHOE-")
    assert best[0]["confidence"] > 0.0


def test_chat_buyer_over_limit_blocks_200(db_session_factory, monkeypatch):
    """Engineered failure: buyer_agent 7x APP-001 = 3493 > 3000 -> HTTP 200 blocked."""
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr("app.commerce.rzp_create_payment_link", lambda o: {"error": "must not be called"})

    sid = "chat-buyer-block"
    client.post("/chat", json={"session_id": sid, "actor": "buyer_agent", "message": "add APP-001"})

    # Drive quantity to 7 via repeated adds.
    for _ in range(6):
        r = client.post("/chat", json={"session_id": sid, "actor": "buyer_agent", "message": "add APP-001"})
        assert r.status_code == 200

    r = client.post("/chat", json={"session_id": sid, "actor": "buyer_agent", "message": "checkout"})
    assert r.status_code == 200
    assert "Checkout preview" in r.json()["reply"]

    r = client.post("/chat", json={"session_id": sid, "actor": "buyer_agent", "message": "Yes proceed"})
    assert r.status_code == 200  # block is control flow, NOT an error status
    body = r.json()
    assert body["blocked"] is True
    assert "per-transaction limit" in body["reply"]
    # No order ever created.
    assert _order_count(db_session_factory) == 0

    events = _audit_types(db_session_factory, sid)
    assert ("chat_turn", "BLOCK", "blocked") in events


def test_chat_stale_quote_after_cart_mutation(db_session_factory, monkeypatch):
    """Checkout -> mutate cart -> approve: quote <-> cart integrity blocks."""
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr("app.commerce.rzp_create_payment_link", lambda o: {"error": "must not be called"})

    sid = "chat-stale"
    client.post("/chat", json={"session_id": sid, "actor": "human", "message": "add APP-001"})
    client.post("/chat", json={"session_id": sid, "actor": "human", "message": "checkout"})
    # Mutate the cart AFTER the preview -> invalidates the quote.
    client.post("/chat", json={"session_id": sid, "actor": "human", "message": "add ACC-001"})

    r = client.post("/chat", json={"session_id": sid, "actor": "human", "message": "Yes proceed"})
    body = r.json()
    # The quote was superseded by the cart mutation -> approval fails closed, so
    # the checkout blocks before any charge. No Order row is created.
    assert body["blocked"] is True
    assert _order_count(db_session_factory) == 0
    events = _audit_types(db_session_factory, sid)
    assert ("chat_turn", "BLOCK", "blocked") in events


# ---------------------------------------------------------------------------
# Agent-commerce APIs.
# ---------------------------------------------------------------------------

def test_agent_discover_external_payable(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    listings = [{"name": "Adidas Ultraboost", "price": 3600.0, "currency": "INR",
                 "source": "https://a.test/ub", "why": "running shoe within stock",
                 "features": ["running"], "score": 0.6}]
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: listings)

    resp = client.post("/agent/discover",
                       json={"session_id": "agent-disc", "actor": "human",
                             "query": "running shoes", "budget": 4000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    candidate = body["candidates"][0]
    # External listings are ADDABLE/payable via their EXT-xxx SKU (Rev 3).
    assert candidate["payable"] is True
    assert candidate["sku"] == "EXT-001"


def test_agent_quote_then_guardrailed_checkout_blocked_200(db_session_factory, monkeypatch):
    """Assuming exhausted buyer-agent blocks are handled by guardrail at checkout.
    Simulate an over-limit buyer quote (e.g. 7x APP-001 = 3493 > 3000)."""
    _seed(db_session_factory)
    cartoonly = [{"sku": "APP-001", "quantity": 7}]
    quote_resp = client.post("/agent/quote",
                             json={"session_id": "agent-q", "actor": "buyer_agent", "cart": cartoonly})
    assert quote_resp.status_code == 200
    q = quote_resp.json()
    assert q["status"] == "active"
    assert float(q["amount"]) == 3493.0

    checkout_resp = client.post("/agent/checkout",
                                json={"session_id": "agent-q", "actor": "buyer_agent",
                                      "quote_id": q["quote_id"], "nonce": q["nonce"]})
    assert checkout_resp.status_code == 200          # block is 200 control flow
    co = checkout_resp.json()
    assert co["blocked"] is True
    assert "per-transaction limit" in co["reason"]
    # No order, no Razorpay.
    assert _order_count(db_session_factory) == 0


def test_agent_checkout_bad_nonce_blocks_200(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    qr = client.post("/agent/quote",
                     json={"session_id": "agent-nonce", "actor": "human",
                           "cart": [{"sku": "APP-001", "quantity": 1}]})
    q = qr.json()
    resp = client.post("/agent/checkout", json={"session_id": "agent-nonce", "actor": "human",
                                                "quote_id": q["quote_id"], "nonce": "wrong-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is True
    assert "approval token mismatch" in body["reason"]
    assert _order_count(db_session_factory) == 0


def test_agent_checkout_pays_when_all_ok(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    monkeypatch.setattr("app.commerce.rzp_create_payment_link",
                        lambda o: {"short_url": "https://rzp.test/a", "id": "plink_2",
                                   "order_id": "ord_2"})
    qr = client.post("/agent/quote",
                     json={"session_id": "agent-pay", "actor": "human",
                           "cart": [{"sku": "APP-001", "quantity": 1}]})
    q = qr.json()
    resp = client.post("/agent/checkout", json={"session_id": "agent-pay", "actor": "human",
                                                "quote_id": q["quote_id"], "nonce": q["nonce"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is False
    assert body["payment_link"] == "https://rzp.test/a"
    assert _order_count(db_session_factory) == 1

    # Quote consumed -> a duplicate checkout is blocked.
    dup = client.post("/agent/checkout", json={"session_id": "agent-pay", "actor": "human",
                                               "quote_id": q["quote_id"], "nonce": q["nonce"]})
    assert dup.status_code == 200
    assert dup.json()["blocked"] is True


def test_agent_quote_reference_skus_require_name_and_price(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    # EXT-xxx needs name/price — a bare EXT-999 isn't addable.
    resp = client.post("/agent/quote", json={"session_id": "agent-bad", "actor": "human",
                                             "cart": [{"sku": "EXT-999", "quantity": 1}]})
    assert resp.status_code == 422
    assert "unknown SKU" in resp.json()["detail"]


def test_agent_quote_external_reference_payable(db_session_factory, monkeypatch):
    """A SERP listing (EXT-xxx with name/price from /agent/discover) goes into
    the cart, gets quoted, and clears checkout exactly like a merchant item."""
    _seed(db_session_factory)
    monkeypatch.setattr("app.commerce.rzp_create_payment_link",
                        lambda o: {"short_url": "https://rzp.test/ext", "id": "plink_3",
                                   "order_id": "ord_3"})
    qr = client.post("/agent/quote",
                     json={"session_id": "agent-ext", "actor": "human",
                           "cart": [{"sku": "EXT-001", "name": "Logitech Mouse",
                                     "price": 2500.0, "quantity": 1}]})
    assert qr.status_code == 200
    q = qr.json()
    assert q["status"] == "active"
    assert float(q["amount"]) == 2500.0

    cr = client.post("/agent/checkout", json={"session_id": "agent-ext", "actor": "human",
                                              "quote_id": q["quote_id"], "nonce": q["nonce"]})
    assert cr.status_code == 200
    co = cr.json()
    assert co["blocked"] is False
    assert co["order_id"] is not None
    assert _order_count(db_session_factory) == 1


def test_agent_order_status_and_metrics(db_session_factory, monkeypatch):
    _seed(db_session_factory)
    monkeypatch.setattr("app.commerce.rzp_create_payment_link",
                        lambda o: {"short_url": "https://rzp.test/m", "id": "plink_3",
                                   "order_id": "ord_3"})
    qr = client.post("/agent/quote",
                     json={"session_id": "agent-ms", "actor": "human",
                           "cart": [{"sku": "ACC-001", "quantity": 1}]})
    q = qr.json()
    client.post("/agent/checkout", json={"session_id": "agent-ms", "actor": "human",
                                         "quote_id": q["quote_id"], "nonce": q["nonce"]})

    s = db_session_factory()
    order = s.query(Order).first()
    oid = order.id
    s.close()

    o = client.get(f"/agent/order/{oid}").json()
    assert o["status"] == "created"
    assert o["total"] == "899.00"

    m = client.get("/metrics/latency").json()
    assert "total_requests" in m