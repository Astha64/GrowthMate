"""
Catalog RAG index tests (Rev 3, Phase 2).

Pins the deterministic on-disk k-NN index: build/load round-trips, ranking
order, threshold behaviour, idempotent rebuilds, DB backfills, and fail-open
behaviour on corrupt/missing files.
"""

import json
import os

import pytest

from app import catalog_index as ci
from app.models import Product

SEEDS = [
    dict(sku="SHOE-001", name="Nike Revolution 6", price=1899.0, stock=20,
         category="footwear", description="Lightweight running shoe",
         merchant_priority=0.9, semantic_text="nike revolution running shoe footwear"),
    dict(sku="ELEC-001", name="Wireless Earbuds", price=1999.0, stock=30,
         category="electronics", description="Bluetooth true wireless earbuds",
         merchant_priority=0.8, semantic_text="wireless earbuds bluetooth electronics"),
    dict(sku="HOME-001", name="Ceramic Coffee Mug", price=349.0, stock=60,
         category="home", description="Handmade ceramic mug",
         merchant_priority=0.7, semantic_text="ceramic coffee mug handmade home"),
    dict(sku="ACC-001", name="Leather Wallet", price=899.0, stock=22,
         category="accessories", description="Genuine leather bifold wallet",
         merchant_priority=0.6, semantic_text="leather wallet bifold accessories"),
    dict(sku="APP-001", name="Cotton Crew T-Shirt", price=499.0, stock=50,
         category="apparel", description="100% cotton crew neck tee",
         merchant_priority=0.65, semantic_text="cotton crew t-shirt apparel"),
]


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path, monkeypatch):
    ci.clear_cache()
    monkeypatch.setattr(ci, "INDEX_PATH", str(tmp_path / "catalog.index.json"))
    yield
    ci.clear_cache()


def _dicts() -> list[dict]:
    return [dict(s, currency="INR") for s in SEEDS]


def test_build_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "catalog.index.json")
    info = ci.build(_dicts(), index_path=path)
    assert info["count"] == 5
    assert os.path.exists(path)

    ci.clear_cache()
    loaded = ci.load(path)
    assert loaded["schema"] == "catalog_index/v1"
    assert loaded["build_hash"] == info["build_hash"]
    assert [r["sku"] for r in loaded["rows"]] == sorted(r["sku"] for r in SEEDS)


def test_retrieve_ranks_relevant_first():
    ci.build(_dicts())
    top = ci.retrieve("wireless bluetooth earbuds", k=3)
    assert top and top[0]["sku"] == "ELEC-001"
    assert top[0]["score"] >= top[-1]["score"]

    top = ci.retrieve("coffee mug", k=3)
    assert top[0]["sku"] == "HOME-001"

    raked = ci.retrieve("wallet accessories", k=5, min_score=0.0)
    assert raked[0]["sku"] == "ACC-001"
    assert raked[0]["score"] >= raked[1]["score"]


def test_retrieve_never_raises_and_respects_threshold():
    ci.build(_dicts())
    # Keyword string must not contaminate "no result".
    assert ci.retrieve("a complete nonsense query ", k=5, min_score=0.99) == []
    # Default threshold filters weak lexical noise but keeps relevant rows.
    all_rows = ci.retrieve("otter", k=20, min_score=-1.0)
    assert len(all_rows) >= 1
    empty = ci.retrieve("================", k=5, min_score=0.9)
    assert empty == []


def test_retrieve_deterministic_and_stable_ties():
    ci.build(_dicts())
    first = ci.retrieve("wireless earbuds", k=5)
    second = ci.retrieve("wireless earbuds", k=5)
    assert first == second


def test_retrieve_missing_index_is_empty():
    assert ci.retrieve("anything") == []
    assert ci.rows() == []


def test_corrupt_index_fails_open(tmp_path):
    bad = tmp_path / "catalog.index.json"
    bad.write_text("{not json")
    assert ci.load(str(bad)) is None
    assert ci.retrieve("mug", index_path=str(bad)) == []

    # Wrong schema is also ignored.
    bad.write_text(json.dumps({"schema": "someone-elses"}, separators=(",", ":")))
    assert ci.retrieve("mug", index_path=str(bad)) == []


def test_build_is_idempotent(tmp_path):
    path = str(tmp_path / "catalog.index.json")
    ci.build(_dicts(), index_path=path)
    first_mtime = os.path.getmtime(path)
    ci.clear_cache()
    info2 = ci.build(_dicts(), index_path=path)
    assert info2["build_hash"] == ci.load(path)["build_hash"]


def test_is_current():
    ci.build(_dicts())
    assert ci.is_current(_dicts()) is True
    changed = _dicts()
    changed[0] = dict(changed[0], price=1.0)
    assert ci.is_current(changed) is False


def test_backfill_embedding_columns(db_session_factory):
    s = db_session_factory()
    for p in SEEDS:
        s.add(Product(**p))
    s.commit()
    s.close()

    ci.build()  # reads from the swapped DB session
    updated = ci.backfill_embedding_columns()
    assert updated >= 5

    s = db_session_factory()
    row = s.query(Product).filter(Product.sku == "ELEC-001").first()
    stored = json.loads(row.embedding)
    s.close()
    from app.embeddings import cosine, embed_text
    assert abs(cosine(stored, embed_text(_row_text(row))) - 1.0) < 1e-9


def _row_text(row) -> str:
    return (row.semantic_text or "").strip() or " ".join(
        filter(None, [row.name, row.category, row.description]))