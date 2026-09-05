"""
Agent-readable feed tests (Rev 3, Phase 5).

Pins the two well-known manifests: the deterministic catalog feed (served from
catalog_index.rows(), no invented fields) and the order ledger whose every read
re-verifies the HMAC mandate per row (tampered totals surface as
`mandate_verified: false`, never a 5xx). Both are read-only and fail-open.
"""

import json

from app import agent_feed
from app import catalog_index
from app import commerce
from app import mandate
from app.main import app

P_A = dict(sku="APP-001", name="Cotton Crew T-Shirt", price=499.0, currency="INR",
           stock=50, category="apparel", description="premium cotton crew neck tee",
           merchant_priority=0.8, semantic_text="cotton crew t-shirt apparel")
P_B = dict(sku="SHOE-001", name="Racer Run Shoe", price=2499.0, currency="INR",
           stock=12, category="shoes", description="lightweight running sneaker",
           merchant_priority=0.6, semantic_text="racing running sneaker shoes")


def _build_index_to(db_session_factory, tmp_path):
    from app.models import Product
    s = db_session_factory()
    s.add(Product(**P_A))
    s.add(Product(**P_B))
    s.commit()
    s.close()
    index_path = str(tmp_path / "index.json")
    assert catalog_index.build(products=[P_A, P_B], index_path=index_path)
    import app.catalog_index as ci
    import pytest
    monkey = pytest.MonkeyPatch()
    monkey.setattr(ci, "INDEX_PATH", index_path)
    return monkey


class TestCatalogFeed:
    def test_entries_carry_exactly_catalog_fields(self, db_session_factory, tmp_path):
        monkey = _build_index_to(db_session_factory, tmp_path)
        try:
            feed = agent_feed.catalog_feed()
            assert feed["schema"] == "agentic-catalog/v1"
            assert feed["count"] == 2
            assert feed["empty"] is False
            assert feed["currency"] == "INR"
            names = {e["name"] for e in feed["endpoints"]}
            assert {"chat", "quote", "checkout"} <= names
            for entry in feed["products"]:
                # The feed must not invent fields that do not exist on the rows.
                assert set(entry) == {
                    "sku", "name", "category", "currency", "price",
                    "stock", "merchant_priority", "description", "semantic_text",
                }
                assert entry["sku"]
                assert isinstance(entry["price"], float)
        finally:
            monkey.undo()

    def test_order_is_deterministic(self, db_session_factory, tmp_path):
        monkey = _build_index_to(db_session_factory, tmp_path)
        try:
            a = json.dumps(agent_feed.catalog_feed(), sort_keys=True)
            b = json.dumps(agent_feed.catalog_feed(), sort_keys=True)
            assert a == b
        finally:
            monkey.undo()

    def test_empty_catalog_is_fail_open(self, db_session_factory, tmp_path):
        index_path = str(tmp_path / "empty.json")
        catalog_index.build(products=[], index_path=index_path)
        import app.catalog_index as ci
        import pytest
        monkey = pytest.MonkeyPatch()
        monkey.setattr(ci, "INDEX_PATH", index_path)
        try:
            feed = agent_feed.catalog_feed()
            assert feed["empty"] is True
            assert feed["products"] == []
        finally:
            monkey.undo()


class TestOrdersFeed:
    def _signed_order(self, db_session_factory, session_id="o-sess", quantity=2):
        from app.models import CartItem
        s = db_session_factory()
        s.add(CartItem(session_id=session_id, item_type="merchant", ref_id="APP-001",
                       name=P_A["name"], price=P_A["price"], quantity=quantity))
        s.commit()
        s.close()
        subtotal = float(P_A["price"]) * quantity
        sig = mandate.sign_mandate(session_id=session_id, actor="human",
                                   cart_hash="c0ffee", amount=subtotal, quote_id="q-feed")
        order = commerce.create_payment_order(
            session_id, "human", "c0ffee", quote_id="q-feed", mandate_signature=sig)
        assert "error" not in order
        return order

    def test_signed_order_reads_verified_true(self, db_session_factory):
        self._signed_order(db_session_factory)
        feed = agent_feed.orders_feed(session_id="o-sess")
        assert feed["count"] == 1
        row = feed["orders"][0]
        assert row["mandate_verified"] is True
        assert row["quote_id"] == "q-feed"
        assert row["actor"] == "human"

    def test_tampered_total_reads_verified_false(self, db_session_factory):
        from app.models import Order
        self._signed_order(db_session_factory)
        s = db_session_factory()
        row = s.query(Order).filter(Order.session_id == "o-sess").first()
        row.total += 100.0  # evidence of tampering after the ALLOW snapshot
        s.commit()
        s.close()
        feed = agent_feed.orders_feed(session_id="o-sess")
        assert feed["orders"][0]["mandate_verified"] is False

    def test_unsigned_row_reads_verified_false(self, db_session_factory):
        from app.models import CartItem
        s = db_session_factory()
        s.add(CartItem(session_id="o-unsigned", item_type="merchant", ref_id="APP-001",
                       name=P_A["name"], price=P_A["price"], quantity=1))
        s.commit()
        s.close()
        commerce.create_payment_order("o-unsigned", "human", "deadbeef")
        feed = agent_feed.orders_feed(session_id="o-unsigned")
        assert feed["orders"][0]["mandate_verified"] is False

    def test_empty_feed(self, db_session_factory):
        feed = agent_feed.orders_feed(session_id="nope")
        assert feed["empty"] is True
        assert feed["orders"] == []


class TestEndpoints:
    def test_catalog_endpoint_serves_feed(self, db_session_factory, tmp_path):
        from fastapi.testclient import TestClient
        monkey = _build_index_to(db_session_factory, tmp_path)
        try:
            client = TestClient(app)
            r = client.get("/.well-known/agentic-catalog.json")
            assert r.status_code == 200
            body = r.json()
            assert body["schema"] == "agentic-catalog/v1"
            assert body["count"] >= 2
        finally:
            monkey.undo()

    def test_orders_endpoint_serves_feed(self, db_session_factory):
        from fastapi.testclient import TestClient
        self_signed = TestOrdersFeed()
        self_signed._signed_order(db_session_factory, session_id="o-ep")
        client = TestClient(app)
        r = client.get("/.well-known/agentic-orders.json", params={"session_id": "o-ep"})
        assert r.status_code == 200
        body = r.json()
        assert body["schema"] == "agentic-orders/v1"
        assert body["orders"][0]["mandate_verified"] is True