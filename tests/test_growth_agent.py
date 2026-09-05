"""
Growth-recovery agent tests (Rev 3, Phase 6).

Pins the deterministic quantity-fit recovery: proposals only (never a cart
mutation, never a money move, never a discount), bounded by the guardrail
per-transaction limit, floored by MIN_RECOVERY_AMOUNT, and audited under the
`system_growth_agent` actor with event_type `recovery_offer`.
"""

from app import growth_agent
from app import mandate
from app.guardrail import MAX_PER_TRANSACTION

APP_001 = dict(sku="APP-001", name="Cotton Crew T-Shirt", price=499.0, currency="INR",
               stock=50, category="apparel", description="d", merchant_priority=0.8,
               semantic_text="cotton crew t-shirt apparel")
LUX = dict(sku="LUX-001", name="Premium Console", price=3500.0, currency="INR",
           stock=3, category="electronics", description="d", merchant_priority=0.9,
           semantic_text="premium gaming console electronics")


class TestRecoverCart:
    def test_within_limit_is_not_eligible(self, db_session_factory):
        from app.models import CartItem
        s = db_session_factory()
        s.add(CartItem(session_id="ok", item_type="merchant", ref_id="APP-001",
                       name="T", price=499.0, quantity=2))  # ₹998 < ₹3000
        s.commit()
        s.close()
        out = growth_agent.recover_cart("ok", "buyer_agent")
        assert out["eligible"] is False
        assert out["sku"] is None

    def test_over_limit_proposes_quantity_fit(self, db_session_factory):
        from app.models import CartItem
        s = db_session_factory()
        s.add(CartItem(session_id="over", item_type="merchant", ref_id="APP-001",
                       name="T", price=499.0, quantity=7))  # ₹3493 > ₹3000
        s.commit()
        s.close()
        out = growth_agent.recover_cart("over", "buyer_agent")
        assert out["eligible"] is True
        assert out["sku"] == "APP-001"
        assert out["from_quantity"] == 7
        assert out["to_quantity"] == 6  # floor(3000/499)=6, 6×499=2994 ≤ limit
        assert out["old_total"] == "3493.00"
        assert out["new_total"] == "2994.00"
        assert float(out["new_total"]) <= MAX_PER_TRANSACTION["buyer_agent"]

    def test_single_unit_over_limit_is_unrecoverable(self, db_session_factory):
        from app.models import CartItem
        s = db_session_factory()
        s.add(CartItem(session_id="ci", item_type="merchant", ref_id="LUX-001",
                       name="Lux", price=3500.0, quantity=1))  # > ₹3000 single unit
        s.commit()
        s.close()
        out = growth_agent.recover_cart("ci", "buyer_agent")
        assert out["eligible"] is False
        assert "single unit alone" in out["reason"]

    def test_min_recovery_floor_declines(self, db_session_factory, monkeypatch):
        from app.models import CartItem
        s = db_session_factory()
        s.add(CartItem(session_id="fl", item_type="merchant", ref_id="APP-001",
                       name="T", price=499.0, quantity=7))
        s.commit()
        s.close()
        monkeypatch.setattr("app.growth_agent.MIN_RECOVERY_AMOUNT", 99999.0)
        out = growth_agent.recover_cart("fl", "buyer_agent")
        assert out["eligible"] is False
        assert "minimum transaction" in out["reason"]

    def test_unknown_actor_is_not_eligible(self, db_session_factory):
        from app.models import CartItem
        s = db_session_factory()
        s.add(CartItem(session_id="u", item_type="merchant", ref_id="APP-001",
                       name="T", price=499.0, quantity=7))
        s.commit()
        s.close()
        out = growth_agent.recover_cart("u", "system_growth_agent")
        assert out["eligible"] is False


class TestRecoveryEndpoint:
    def _seed(self, db_session_factory, session_id, qty=7):
        from app.models import CartItem, Product
        s = db_session_factory()
        s.add(Product(**APP_001))
        s.add(CartItem(session_id=session_id, item_type="merchant", ref_id="APP-001",
                       name="T", price=499.0, quantity=qty))
        s.commit()
        s.close()

    def test_recovery_audits_and_never_mutates(self, db_session_factory):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.models import AuditLog, CartItem, Order
        client = TestClient(app)
        self._seed(db_session_factory, "rec")

        r = client.post("/agent/recovery", json={"session_id": "rec", "actor": "buyer_agent"})
        body = r.json()
        assert body["eligible"] is True
        assert body["from_quantity"] == 7
        assert body["to_quantity"] == 6

        s = db_session_factory()
        assert s.query(CartItem).filter(CartItem.session_id == "rec").one().quantity == 7
        assert s.query(Order).filter(Order.session_id == "rec").count() == 0
        audit = (s.query(AuditLog)
                 .filter(AuditLog.event_type == "recovery_offer").first())
        assert audit.actor == growth_agent.RECOVERY_ACTOR
        assert audit.decision == "ALLOW"
        assert audit.tool_name == "agent_recovery"
        s.close()

    def test_non_eligible_recovery_audits_block(self, db_session_factory):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.models import AuditLog
        client = TestClient(app)
        self._seed(db_session_factory, "rec2", qty=2)  # ₹998 within ₹3000

        r = client.post("/agent/recovery", json={"session_id": "rec2", "actor": "buyer_agent"})
        assert r.json()["eligible"] is False

        s = db_session_factory()
        audit = (s.query(AuditLog)
                 .filter(AuditLog.event_type == "recovery_offer").first())
        assert audit.actor == growth_agent.RECOVERY_ACTOR
        assert audit.decision == "BLOCK"
        s.close()

    def test_recovery_then_normal_guardrailed_checkout(self, db_session_factory, monkeypatch):
        """A recovery proposal does not bypass the money pipeline: after
        adjusting to the proposed quantity the buyer must still pass the normal
        quote/approval/guardrail flow, which mints an HMAC mandate."""
        from fastapi.testclient import TestClient
        from app import discovery
        from app.main import app
        from app.models import Order
        client = TestClient(app)
        self._seed(db_session_factory, "rec3", qty=7)
        monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
        monkeypatch.setattr("app.commerce.rzp_create_payment_link",
                            lambda o: {"short_url": "https://rzp.test/rec", "id": "pl",
                                       "order_id": "ord_rec"})

        r = client.post("/agent/recovery", json={"session_id": "rec3", "actor": "buyer_agent"})
        assert r.json()["to_quantity"] == 6

        # Buyer applies the recovery (reduce to 6) then rides the real pipeline.
        from app.commerce import set_quantity
        set_quantity("rec3", "buyer_agent", "APP-001", 6)
        quote = client.post("/agent/quote", json={
            "session_id": "rec3", "actor": "buyer_agent",
            "cart": [{"sku": "APP-001", "quantity": 6}],
        }).json()
        resp = client.post("/agent/checkout", json={
            "session_id": "rec3", "actor": "buyer_agent",
            "quote_id": quote["quote_id"], "nonce": quote["nonce"],
        }).json()
        assert resp["blocked"] is False

        s = db_session_factory()
        order = s.query(Order).filter(Order.session_id == "rec3").first()
        assert order.total == 2994.0
        assert order.mandate_signature
        assert mandate.verify_order_mandate({
            "session_id": order.session_id, "actor": order.actor,
            "cart_hash": order.cart_hash, "quote_id": order.quote_id,
            "total": order.total, "mandate_signature": order.mandate_signature,
        }) is True
        s.close()