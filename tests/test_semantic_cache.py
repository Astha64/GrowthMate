"""
Semantic cache tests (LLD §8) — exact hit, semantic hit, stale miss,
low-similarity miss, corrupt-entry miss. DB-resident (SearchCache table).
"""

import json
from datetime import datetime, timedelta, timezone

from app import semantic_cache as sc
from app.config import SEMANTIC_CACHE_SIMILARITY_THRESHOLD, SEMANTIC_CACHE_TTL_SECONDS
from app.embeddings import embed_text
from app.models import SearchCache

CANDIDATES = [
    {"name": "Nike Pegasus 40", "price": 2100, "currency": "INR", "source": "https://a.test/p1", "why": "within budget"},
]


def _seed(db_session_factory, text, results, *, expire_in=timedelta(seconds=3600), corrupt=False):
    s = db_session_factory()
    s.add(
        SearchCache(
            query_text=text,
            query_normalized=text,
            embedding="not-json" if corrupt else json.dumps(embed_text(text)),
            requirements_json=json.dumps({}),
            results_json=json.dumps(results),
            source="serpapi",
            quality_score=0.5,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + expire_in,
        )
    )
    s.commit()
    s.close()


def test_exact_hit(db_session_factory):
    req = {"category": "footwear", "keywords": ["running"], "budget": 2500}
    _seed(db_session_factory, sc.requirements_to_query(req), CANDIDATES)
    hit = sc.lookup(req)
    assert hit and hit["hit"] and hit["fresh"] is True
    assert len(hit["results"]) == 1
    assert hit["similarity"] > SEMANTIC_CACHE_SIMILARITY_THRESHOLD


def test_semantic_neq_but_similar_hit(db_session_factory):
    # Similar wording still hits (same keywords/category).
    req1 = {"category": "footwear", "keywords": ["running"], "budget": 2500}
    _seed(db_session_factory, "running shoes road running", CANDIDATES)
    req2 = {"category": "footwear", "keywords": ["running", "road"]}
    hit = sc.lookup(req2)
    assert hit and hit["hit"]


def test_stale_miss(db_session_factory):
    req = {"category": "electronics", "keywords": ["earbuds"]}
    _seed(db_session_factory, sc.requirements_to_query(req), CANDIDATES,
          expire_in=timedelta(seconds=-5))
    assert sc.lookup(req) is None


def test_low_similarity_miss(db_session_factory):
    _seed(db_session_factory, "wallet leather bifold genuine", CANDIDATES)
    req = {"category": "footwear", "keywords": ["running", "marathon"], "budget": 3000}
    assert sc.lookup(req) is None


def test_corrupt_entry_is_miss(db_session_factory):
    req = {"category": "footwear", "keywords": ["running"]}
    _seed(db_session_factory, sc.requirements_to_query(req), CANDIDATES, corrupt=True)
    assert sc.lookup(req) is None


def test_put_then_lookup_roundtrip(db_session_factory):
    req = {"category": "home", "keywords": ["lamp"], "budget": 1500}
    cache_id = sc.put(req, CANDIDATES)
    assert cache_id is not None
    hit = sc.lookup({"category": "home", "keywords": ["lamp"]})
    assert hit and hit["source"] == "serpapi"
    assert hit["results"][0]["name"] == "Nike Pegasus 40"


def test_ttl_config_positive():
    assert SEMANTIC_CACHE_TTL_SECONDS > 0


# ---------------------------------------------------------------------------
# Rev 3, Phase 3 — normalize_requirements / cached_lookup / store / prune.
# ---------------------------------------------------------------------------

def test_normalize_requirements_coerces_and_trims():
    assert sc.normalize_requirements(None) is None
    assert sc.normalize_requirements({}) is None
    assert sc.normalize_requirements({"category": "  shoes "}) == {
        "category": "shoes", "product_type": None, "brand": None,
    }
    norm = sc.normalize_requirements({
        "category": "electronics", "keywords": [" earbuds ", "wireless"],
        "budget": " 1500.0 ", "extra_noise": "ignored",
    })
    assert norm["budget"] == 1500.0
    assert norm["keywords"] == [" earbuds ", "wireless"]
    assert "extra_noise" not in norm
    assert sc.normalize_requirements({"budget_max": 500})["budget"] == 500.0


def test_lookup_respects_stricter_min_similarity(db_session_factory):
    req = {"category": "footwear", "keywords": ["running"], "budget": 2500}
    _seed(db_session_factory, sc.requirements_to_query(req), CANDIDATES)
    assert sc.lookup(req, min_similarity=0.0) is not None
    # Impossibly strict threshold => miss, even for an exact row.
    assert sc.lookup(req, min_similarity=1.0001) is None


def test_cached_lookup_shapes_output(db_session_factory):
    req = {"category": "home", "keywords": ["mug"]}
    cache_id = sc.put(req, CANDIDATES)
    assert cache_id is not None
    hit = sc.cached_lookup(req)
    assert hit is not None
    assert hit["cache_hit"] is True
    assert hit["source"] == "serpapi"
    assert hit["count"] == 1
    assert len(hit["recommendations"]) == 1
    assert hit["recommendations"][0]["name"] == "Nike Pegasus 40"


def test_cached_lookup_rejects_nonsense_and_caps_items(db_session_factory):
    assert sc.cached_lookup(None) is None
    assert sc.cached_lookup({"empty": True}) is None
    req = {"keywords": ["running"]}
    sc.put(req, CANDIDATES)
    hit = sc.cached_lookup(req, max_items=0)
    assert hit is not None and hit["count"] == 0 and hit["recommendations"] == []


def test_store_refuses_empty(db_session_factory):
    req = {"keywords": ["running"]}
    # Legacy put stores even empty sets (callers decide); Phase-3 store refuses.
    assert sc.put(req, []) is not None
    assert sc.store(req, []) is None
    assert sc.store(req, [{"name": "X"}]) is not None
    assert sc.store(req, "not-a-list") is None
    s = db_session_factory()
    assert s.query(SearchCache).count() == 2  # legacy put + one valid store
    s.close()


def test_store_and_cached_lookup_roundtrip(db_session_factory):
    req = {"category": "accessories", "keywords": ["wallet"]}
    sc.store(req, CANDIDATES)
    hit = sc.cached_lookup({"category": "accessories", "keywords": ["wallet"]})
    assert hit and hit["cache_hit"] and hit["count"] == 1


def test_expired_row_not_served_by_cached_lookup(db_session_factory):
    req = {"category": "footwear", "keywords": ["running"]}
    cache_id = sc.put(req, CANDIDATES)
    s = db_session_factory()
    row = s.query(SearchCache).get(cache_id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    s.commit()
    s.close()
    assert sc.cached_lookup(req) is None


def test_prune_expired_bounds_table(db_session_factory):
    req = {"keywords": ["running"]}
    now = datetime.now(timezone.utc)
    s = db_session_factory()
    for i in range(3):
        s.add(SearchCache(
            query_text=f"q{i}", query_normalized=f"q{i}",
            embedding=json.dumps([0.0] * 4), requirements_json="{}",
            results_json=json.dumps(CANDIDATES), source="serpapi",
            quality_score=0.5, created_at=now,
            expires_at=now + timedelta(seconds=-60 if i == 0 else 3600),
        ))
    s.commit()
    s.close()
    assert sc.prune_expired(keep_rows=2) >= 1
    s = db_session_factory()
    alive = s.query(SearchCache).count()
    s.close()
    assert alive <= 2


def test_discovery_cache_shortcircuit_uses_wrapper(db_session_factory, monkeypatch):
    """discovery.discover must serve the Phase-3 discovery-result shape from a
    cached similar query without touching the external sources at all."""
    from app import discovery
    from datetime import datetime, timezone

    monkeypatch.setattr(discovery, "search_external_sources",
                        lambda req: (_ for _ in ()).throw(
                            AssertionError("external search must not run on a cache hit")))
    req = {"category": "footwear", "keywords": ["wireless", "mouse", "running"]}
    sc.put(req, CANDIDATES)
    result = discovery.discover(req, session_id="s-cache-wrap")
    assert result["cache_hit"] is True
    assert result["count"] == 1
    assert result["recommendations"][0]["name"] == "Nike Pegasus 40"