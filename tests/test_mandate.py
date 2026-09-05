"""
HMAC mandate tests (Rev 3, Phase 4).

Pins the deterministic mandate system: signing/verification primitives,
tamper-evidence on every covered field, the ALLOW-only contract on both money
paths (/chat pipeline and /agent/checkout), BLOCK => NULL signature on the
audit row and no Order, and the startup fail-fast without MANDATE_SECRET.
"""

import json
import os

from app import mandate
from app.main import app

APP_001 = dict(sku="APP-001", name="Cotton Crew T-Shirt", price=499.0, currency="INR",
               stock=50, category="apparel", description="premium cotton crew neck t-shirt",
               merchant_priority=0.8, semantic_text="cotton crew t-shirt apparel")


# ---------------------------------------------------------------------------
# Pure primitives.
# ---------------------------------------------------------------------------

def test_sign_and_verify_roundtrip(monkeypatch):
    monkeypatch.setenv("MANDATE_SECRET", "s3cr3t")
    sig = mandate.sign_mandate(session_id="s1", actor="human", cart_hash="abc",
                               amount="4990.00", quote_id="q1")
    assert mandate.verify_mandate(sig, session_id="s1", actor="human",
                                  cart_hash="abc", amount="4990.00", quote_id="q1")
    assert mandate.mandate_configured() is True


def test_signature_is_deterministic(monkeypatch):
    monkeypatch.setenv("MANDATE_SECRET", "s3cr3t")
    a = mandate.sign_mandate(session_id="s1", actor="human", cart_hash="abc",
                             amount=4990, quote_id="q1")
    b = mandate.sign_mandate(session_id="s1", actor="human", cart_hash="abc",
                             amount="4990.00", quote_id="q1")
    assert a == b  # Decimal '4990.00' / float 4990.0 / str '4990' fingerprint alike


def test_tampering_invalidates_every_field(monkeypatch):
    monkeypatch.setenv("MANDATE_SECRET", "s3cr3t")
    kw = dict(session_id="s1", actor="human", cart_hash="abc", amount="4990.00", quote_id="q1")
    sig = mandate.sign_mandate(**kw)
    for field, bad in [
        ("session_id", "s2"), ("actor", "buyer_agent"), ("cart_hash", "xyz"),
        ("amount", "4991.00"), ("quote_id", "q2"),
    ]:
        tampered = dict(kw, **{field: bad})
        assert not mandate.verify_mandate(sig, **tampered), f"tampered {field} verified"


def test_wrong_secret_or_empty_signature_fails(monkeypatch):
    monkeypatch.setenv("MANDATE_SECRET", "s3cr3t")
    sig = mandate.sign_mandate(session_id="s1", actor="human", cart_hash="abc",
                               amount=4990.0, quote_id="q1")
    monkeypatch.setenv("MANDATE_SECRET", "different-secret")
    assert not mandate.verify_mandate(sig, session_id="s1", actor="human",
                                      cart_hash="abc", amount=4990.0, quote_id="q1")
    assert not mandate.verify_mandate(None, session_id="s1", actor="human",
                                      cart_hash="abc", amount=4990.0, quote_id="q1")


def test_verify_order_mandate(monkeypatch):
    monkeypatch.setenv("MANDATE_SECRET", "s3cr3t")
    order = {
        "session_id": "s1", "actor": "human", "cart_hash": "abc", "total": "4990.00",
    }
    order["mandate_signature"] = mandate.sign_mandate(
        session_id=order["session_id"], actor=order["actor"],
        cart_hash=order["cart_hash"], amount=order["total"],
    )
    assert mandate.verify_order_mandate(order) is True
    order["total"] = "99999.00"
    assert mandate.verify_order_mandate(order) is False


# ---------------------------------------------------------------------------
# /chat pipeline: ALLOW signs on the Order + audit rows; BLOCK leaves both NULL.
# ---------------------------------------------------------------------------

def test_chat_allows_and_signs_order_and_audit(db_session_factory, monkeypatch):
    from fastapi.testclient import TestClient
    from app import discovery
    from app.models import AuditLog, Order, Product

    client = TestClient(app)
    s = db_session_factory()
    s.add(Product(**APP_001))
    s.commit()
    s.close()
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr(
        "app.commerce.rzp_create_payment_link",
        lambda o: {"short_url": "https://rzp.test/mandate", "id": "plink_m", "order_id": "ord_m"},
    )

    client.post("/chat", json={"session_id": "m-chat", "actor": "human", "message": "add APP-001"})
    client.post("/chat", json={"session_id": "m-chat", "actor": "human", "message": "checkout"})
    r = client.post("/chat", json={"session_id": "m-chat", "actor": "human",
                                   "message": "Yes proceed"})
    body = r.json()
    assert body["blocked"] is False

    s = db_session_factory()
    order = s.query(Order).filter(Order.session_id == "m-chat").first()
    assert order is not None
    assert order.mandate_signature
    assert order.quote_id
    assert mandate.verify_order_mandate({
        "session_id": order.session_id, "actor": order.actor,
        "cart_hash": order.cart_hash, "quote_id": order.quote_id,
        "total": order.total,
        "mandate_signature": order.mandate_signature,
    }) is True

    chat_row = (s.query(AuditLog)
                .filter(AuditLog.session_id == "m-chat", AuditLog.event_type == "chat_turn")
                .order_by(AuditLog.id.desc()).first())
    assert chat_row.mandate_signature == order.mandate_signature
    s.close()


def test_chat_block_leaves_null_signature_and_no_order(db_session_factory, monkeypatch):
    """The engineered-failure block (buyer_agent > per-transaction limit) must
    audit with a NULL mandate signature and create no Order row."""
    from fastapi.testclient import TestClient
    from app import discovery
    from app.models import AuditLog, Order, Product

    client = TestClient(app)
    s = db_session_factory()
    s.add(Product(**APP_001))
    s.commit()
    s.close()
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr("app.commerce.rzp_create_payment_link",
                        lambda o: (_ for _ in ()).throw(AssertionError("block must not pay")))

    client.post("/chat", json={"session_id": "m-block", "actor": "buyer_agent",
                               "message": "add APP-001"})
    # Now 7x APP-001 = ₹3493 > the buyer_agent ₹3000 per-transaction cap.
    from app.commerce import add_to_cart
    for i in range(6):
        add_to_cart("m-block", "buyer_agent", "APP-001")
    client.post("/chat", json={"session_id": "m-block", "actor": "buyer_agent",
                               "message": "checkout"})
    r = client.post("/chat", json={"session_id": "m-block", "actor": "buyer_agent",
                                   "message": "Yes proceed"})
    body = r.json()
    assert body["blocked"] is True

    s = db_session_factory()
    assert s.query(Order).filter(Order.session_id == "m-block").count() == 0
    row = (s.query(AuditLog)
           .filter(AuditLog.session_id == "m-block", AuditLog.event_type == "chat_turn")
           .order_by(AuditLog.id.desc()).first())
    assert row.mandate_signature is None
    s.close()


# ---------------------------------------------------------------------------
# /agent/checkout: ALLOW mints on the guardrail path; BLOCK stays unsigned.
# ---------------------------------------------------------------------------

def test_agent_checkout_allows_and_signs(db_session_factory, monkeypatch):
    from fastapi.testclient import TestClient
    from app import discovery
    from app.models import AuditLog, Order, Product

    client = TestClient(app)
    s = db_session_factory()
    s.add(Product(**APP_001))
    s.commit()
    s.close()
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr(
        "app.commerce.rzp_create_payment_link",
        lambda o: {"short_url": "https://rzp.test/agent", "id": "plink_a", "order_id": "ord_a"},
    )

    quote = client.post("/agent/quote", json={
        "session_id": "m-agent", "actor": "buyer_agent",
        "cart": [{"sku": "APP-001", "quantity": 1}],
    }).json()
    resp = client.post("/agent/checkout", json={
        "session_id": "m-agent", "actor": "buyer_agent",
        "quote_id": quote["quote_id"], "nonce": quote["nonce"],
    })
    body = resp.json()
    assert body["blocked"] is False

    s = db_session_factory()
    order = s.query(Order).filter(Order.session_id == "m-agent").first()
    assert order.mandate_signature
    assert order.quote_id
    assert mandate.verify_order_mandate({
        "session_id": order.session_id, "actor": order.actor,
        "cart_hash": order.cart_hash, "quote_id": order.quote_id,
        "total": order.total,
        "mandate_signature": order.mandate_signature,
    }) is True
    payment_row = (s.query(AuditLog)
                   .filter(AuditLog.session_id == "m-agent",
                           AuditLog.event_type == "payment").first())
    assert payment_row.mandate_signature == order.mandate_signature
    assert "mandate_signature" in json.loads(payment_row.parameters_json or "{}")
    s.close()


def test_agent_checkout_blocks_without_signature(db_session_factory, monkeypatch):
    from fastapi.testclient import TestClient
    from app import discovery
    from app.models import AuditLog, Order, Product

    client = TestClient(app)
    s = db_session_factory()
    s.add(Product(**APP_001))
    s.commit()
    s.close()
    monkeypatch.setattr(discovery, "search_external_sources", lambda req: [])
    monkeypatch.setattr("app.commerce.rzp_create_payment_link",
                        lambda o: (_ for _ in ()).throw(AssertionError("block must not pay")))

    quote = client.post("/agent/quote", json={
        "session_id": "m-agent2", "actor": "buyer_agent",
        "cart": [{"sku": "APP-001", "quantity": 7}],  # ₹3493 > ₹3000 per-txn
    }).json()
    resp = client.post("/agent/checkout", json={
        "session_id": "m-agent2", "actor": "buyer_agent",
        "quote_id": quote["quote_id"], "nonce": quote["nonce"],
    })
    assert resp.json()["blocked"] is True

    s = db_session_factory()
    assert s.query(Order).filter(Order.session_id == "m-agent2").count() == 0
    row = (s.query(AuditLog)
           .filter(AuditLog.session_id == "m-agent2",
                   AuditLog.event_type == "guardrail_decision").first())
    assert row.mandate_signature is None
    s.close()


# ---------------------------------------------------------------------------
# Startup fail-fast.
# ---------------------------------------------------------------------------

def test_startup_raises_without_mandate_secret(monkeypatch):
    monkeypatch.delenv("MANDATE_SECRET", raising=False)
    import pytest

    from app.main import _startup as startup_handler
    with pytest.raises(RuntimeError, match="MANDATE_SECRET"):
        startup_handler()
    assert mandate.mandate_configured() is False