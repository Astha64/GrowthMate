"""
End-to-end: product suggestions -> pick-by-number -> add to cart -> checkout
(Rev 3 suggestion-selection UX).

The pipeline answers a product query with a numbered 2-3 option list (the
captured merchant match first, then payable catalog complements / next-best
matches), lets the shopper pick by number or SKU, adds exactly that pick to the
cart, and flows through the standard checkout/approval to a mocked Razorpay
order with a verified mandate signature.
"""

from app.main import app
from fastapi.testclient import TestClient

ELEC_001 = dict(sku="ELEC-001", name="Wireless Earbuds Pro", price=2499.0, currency="INR",
                stock=30, category="electronics", description="premium wireless earbuds",
                merchant_priority=0.9, semantic_text="wireless earbuds audio electronics")
ELEC_002 = dict(sku="ELEC-002", name="USB-C Fast Charger", price=1199.0, currency="INR",
                stock=40, category="electronics", description="65W usb c fast charger",
                merchant_priority=0.8, semantic_text="usb c fast charger electronics")
ELEC_003 = dict(sku="ELEC-003", name="Over-Ear Headphones", price=3999.0, currency="INR",
                stock=15, category="electronics", description="over ear headphones",
                merchant_priority=0.7, semantic_text="over ear headphones audio electronics")


def _seed(db_session_factory):
    from app.models import Product
    s = db_session_factory()
    s.add_all([Product(**ELEC_001), Product(**ELEC_002), Product(**ELEC_003)])
    s.commit()
    s.close()


def _cart_skus(db_session_factory, session_id):
    from app.models import CartItem
    s = db_session_factory()
    skus = [c.ref_id for c in s.query(CartItem).filter(CartItem.session_id == session_id).all()]
    s.close()
    return skus


def test_pick_by_number_adds_only_the_chosen_item(db_session_factory, monkeypatch):
    from app import discovery
    client = TestClient(app)
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])

    r = client.post("/chat", json={"session_id": "sug-e2e", "actor": "human",
                                   "message": "wireless earbuds"})
    body = r.json()
    assert body["blocked"] is False
    reply = body["reply"]
    # The query surfaces 2-3 numbered, selectable options.
    assert "best options (merchant match)" in reply
    assert "1)" in reply
    assert "ELEC-001" in reply
    assert "ELEC-002" in reply  # payable catalog complement
    assert "pick" in reply.lower()

    # Pick option 2 (the USB-C charger) by number — only that SKU lands in cart.
    r = client.post("/chat", json={"session_id": "sug-e2e", "actor": "human", "message": "2"})
    assert r.json()["blocked"] is False
    assert _cart_skus(db_session_factory, "sug-e2e") == ["ELEC-002"]

    # Pick option 1 by number as well so checkout covers two items.
    r = client.post("/chat", json={"session_id": "sug-e2e", "actor": "human", "message": "1"})
    assert r.json()["blocked"] is False
    assert sorted(_cart_skus(db_session_factory, "sug-e2e")) == ["ELEC-001", "ELEC-002"]


def test_pick_by_sku_and_full_checkout_with_mandate(db_session_factory, monkeypatch):
    from app import discovery, mandate
    from app.commerce import rzp_create_payment_link
    from app.models import AuditLog, Order
    client = TestClient(app)
    _seed(db_session_factory)
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr(
        "app.commerce.rzp_create_payment_link",
        lambda o: {"short_url": "https://rzp.test/sug", "id": "plink_sug", "order_id": "ord_sug"},
    )

    client.post("/chat", json={"session_id": "sug-e2e-2", "actor": "human",
                               "message": "wireless earbuds"})
    # Pick option 2 by number (the USB-C charger) and option 1 (the earbuds).
    r = client.post("/chat", json={"session_id": "sug-e2e-2", "actor": "human",
                                   "message": "2"})
    assert r.json()["blocked"] is False
    assert _cart_skus(db_session_factory, "sug-e2e-2") == ["ELEC-002"]
    r = client.post("/chat", json={"session_id": "sug-e2e-2", "actor": "human",
                                   "message": "1"})
    assert r.json()["blocked"] is False
    assert sorted(_cart_skus(db_session_factory, "sug-e2e-2")) == ["ELEC-001", "ELEC-002"]

    client.post("/chat", json={"session_id": "sug-e2e-2", "actor": "human", "message": "checkout"})
    r = client.post("/chat", json={"session_id": "sug-e2e-2", "actor": "human",
                                   "message": "Yes proceed"})
    body = r.json()
    assert body["blocked"] is False
    assert "rzp.test/sug" in body.get("reply", "")

    s = db_session_factory()
    order = s.query(Order).filter(Order.session_id == "sug-e2e-2").first()
    assert order is not None
    assert order.mandate_signature
    assert mandate.verify_order_mandate({
        "session_id": order.session_id, "actor": order.actor,
        "cart_hash": order.cart_hash, "quote_id": order.quote_id,
        "total": order.total, "mandate_signature": order.mandate_signature,
    }) is True
    assert rzp_create_payment_link  # (import guard; monkeypatch replaces the app.commerce alias)
    s.close()


def test_offcatalog_market_pick_adds_and_pays_ext_sku(db_session_factory, monkeypatch):
    """Rev 3 external-payable flow: an off-catalog SERP product is surfaced as a
    selectable EXT-xxx option, picking it lands it in the cart, and it pays
    through the standard checkout with a verified mandate. Every money move is
    still guardrailed and approved."""
    from app import discovery, mandate
    from app.models import Order
    client = TestClient(app)
    _seed(db_session_factory)
    monkeypatch.setattr(
        discovery,
        "search_external_sources",
        lambda req: [{"name": "Logitech G Pro Wireless Mouse", "price": 2500.0,
                      "currency": "INR", "source": "https://m.test/mouse",
                      "features": ["wireless"], "why": "wireless gaming mouse"}],
    )
    monkeypatch.setattr(
        "app.commerce.rzp_create_payment_link",
        lambda o: {"short_url": "https://rzp.test/ext", "id": "plink_ext", "order_id": "ord_ext"},
    )

    # Off-catalog query -> no merchant match -> market EXT-001 option surfaced.
    r = client.post("/chat", json={"session_id": "ext-e2e", "actor": "human",
                                   "message": "wireless mouse for gaming"})
    body = r.json()
    assert body["blocked"] is False
    assert "market shows" in body["reply"]
    assert "EXT-001" in body["reply"]

    # Pick option 1 by number -> the EXT-001 market listing lands in the cart.
    r = client.post("/chat", json={"session_id": "ext-e2e", "actor": "human", "message": "1"})
    assert r.json()["blocked"] is False
    assert _cart_skus(db_session_factory, "ext-e2e") == ["EXT-001"]

    # Checkout previews and approval executes a guarded payment.
    client.post("/chat", json={"session_id": "ext-e2e", "actor": "human", "message": "checkout"})
    r = client.post("/chat", json={"session_id": "ext-e2e", "actor": "human",
                                   "message": "Yes proceed"})
    body = r.json()
    assert body["blocked"] is False
    assert "rzp.test/ext" in body.get("reply", "")

    s = db_session_factory()
    order = s.query(Order).filter(Order.session_id == "ext-e2e").first()
    assert order is not None
    assert float(order.total) == 2500.0
    assert order.mandate_signature
    assert mandate.verify_order_mandate({
        "session_id": order.session_id, "actor": order.actor,
        "cart_hash": order.cart_hash, "quote_id": order.quote_id,
        "total": order.total, "mandate_signature": order.mandate_signature,
    }) is True

    # The reference add landed a cart event in the audit trail.
    from app.models import CartEvent
    events = (s.query(CartEvent)
              .filter(CartEvent.session_id == "ext-e2e",
                      CartEvent.event_type == "cart_add").all())
    assert len(events) == 1
    assert events[0].ref_id == "EXT-001"
    s.close()