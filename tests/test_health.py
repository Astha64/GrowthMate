"""
Health and catalog endpoint tests. Uses FastAPI TestClient (httpx).
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "growthmate-backend"


def test_catalog_shape(db_session_factory):
    from app.models import Product

    s = db_session_factory()
    s.add(Product(sku="SHOE-001", name="Nike Revolution 6", price=1899.0, currency="INR", stock=20, category="footwear"))
    s.commit()
    s.close()

    resp = client.get("/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency"] == "INR"
    assert isinstance(body["products"], list)
    first = body["products"][0]
    # Agent-readable: explicit float price, no display formatting (§3).
    assert isinstance(first["price"], (int, float))
    assert set(first.keys()) == {"sku", "name", "description", "price", "stock", "category"}
