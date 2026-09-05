"""
Semantic external cache (Revision 3) — sits in front of SerpAPI (LLD §8).

A cache hit requires BOTH similarity >= threshold AND freshness (expires_at in
the future). Corrupt rows are treated as a miss. Cached external results are
market intelligence ONLY — never the source of truth for merchant pricing,
inventory, or checkout amounts (ARCHITECTURE §7.2).

Rev 3, Phase 3 adds the wrapper layer used by the cascade and pipeline:
  - `normalize_requirements(req)`: canonical keys + budget coercion; None when
    nothing meaningful survives (nothing to look up / store).
  - `cached_lookup(req, ...)`: discovery-result shaped output
    {cache_hit, source, similarity, count, recommendations} on a hit, else None.
  - `store(req, results)`: persist only non-empty, normalized candidate sets.
  The legacy `lookup`/`put` remain for compatibility and are what the rest of
  the codebase (and tests) already use.
"""

import json
from datetime import datetime, timedelta, timezone

from app import db as db_module
from app.config import (
    SEMANTIC_CACHE_SIMILARITY_THRESHOLD,
    SEMANTIC_CACHE_TTL_SECONDS,
)
from app.embeddings import cosine, embed_text
from app.models import SearchCache

_KNOWN_REQ_KEYS = (
    "category", "product_type", "budget", "budget_max", "keywords",
    "required_features", "brand", "explicit_item",
)


def normalize_requirements(requirements) -> dict | None:
    """Canonicalize a requirements dict for cache-keying.

    Trims strings, coerces budget/budget_max to a float, drops empty lists/maps,
    and keeps only known keys. Returns None when nothing meaningful remains
    (no category/features/keywords/brand/budget) — callers treat that as a miss.
    Deterministic and dependency-free."""
    if not isinstance(requirements, dict):
        return None
    out: dict = {
        "category": str(requirements.get("category") or "").strip() or None,
        "product_type": str(requirements.get("product_type") or "").strip() or None,
        "brand": str(requirements.get("brand") or "").strip() or None,
    }
    out.update({k: list(requirements.get(k))
                for k in ("keywords", "required_features", "explicit_item")
                if isinstance(requirements.get(k), (list, tuple, set))})
    budget = requirements.get("budget")
    if budget is None and requirements.get("budget_max") is not None:
        budget = requirements.get("budget_max")
    if budget is not None:
        try:
            budget = float(str(budget).strip())
        except (TypeError, ValueError):
            budget = None
    if budget is not None:
        out["budget"] = budget

    meaningful = bool(
        out.get("category") or out.get("product_type")
        or out.get("brand") or out.get("budget") is not None
        or out.get("keywords") or out.get("required_features")
        or out.get("explicit_item")
    )
    if not meaningful:
        return None
    return out


def _ensure_utc(dt) -> datetime:
    if dt is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def requirements_to_query(requirements: dict) -> str:
    """Deterministic normalized query text from structured requirements."""
    category = (requirements.get("category") or "").strip()
    keywords = " ".join(requirements.get("keywords") or requirements.get("required_features") or [])
    brand = (requirements.get("brand") or "").strip()
    budget = requirements.get("budget") or requirements.get("budget_max")
    parts = [p for p in (category, brand, keywords) if p]
    text = " ".join(parts).strip().lower() or "general"
    if budget:
        text += f" {budget}"
    return " ".join(text.split())


def requirements_embedding(requirements: dict) -> list[float]:
    return embed_text(requirements_to_query(requirements))


def _item_signature(requirements: dict) -> list[str]:
    """Specific item nouns named in the query (e.g. "mouse", "grinder").
    Used as a deterministic gate on top of the embedding similarity so a cached
    "wireless mouse" set is never served for a "wireless keyboard" query."""
    return sorted(requirements.get("explicit_item") or [])


def lookup(requirements: dict, min_similarity: float | None = None) -> dict | None:
    """Fresh + similar cached candidate set, or None. Never raises.

    `min_similarity` overrides the configured threshold (used e.g. by the
    cascade Tier-2 which applies its own stricter gate)."""
    threshold = SEMANTIC_CACHE_SIMILARITY_THRESHOLD if min_similarity is None else min_similarity
    query_norm = requirements_to_query(requirements)
    query_vec = requirements_embedding(requirements)
    q_items = _item_signature(requirements)
    db = db_module.SessionLocal()
    try:
        rows = db.query(SearchCache).order_by(SearchCache.id.desc()).limit(200).all()
    except Exception:  # noqa: BLE001
        return None
    finally:
        db.close()

    best: dict | None = None
    best_sim = 0.0
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            if row.expires_at is None or _ensure_utc(row.expires_at) <= now:
                continue
            stored_vec = json.loads(row.embedding or "[]")
            if not stored_vec:
                continue
            # Deterministic noun gate: both sides name a specific item but they
            # are disjoint -> not a usable hit regardless of embedding sim.
            cached_items = _item_signature(json.loads(row.requirements_json or "{}"))
            if q_items and cached_items and set(q_items).isdisjoint(cached_items):
                continue
            sim = cosine(query_vec, stored_vec)
            if sim < threshold:
                continue
            results = json.loads(row.results_json or "[]")
            if not isinstance(results, list):
                continue
        except Exception:  # noqa: BLE001 — corrupt cache row is a miss
            continue
        if sim > best_sim:
            best_sim = sim
            best = {
                "hit": True,
                "fresh": True,
                "similarity": round(sim, 4),
                "results": results,
                "source": row.source,
                "quality_score": row.quality_score,
            }
    if best:
        best["query"] = query_norm
        return best
    return None


def cached_lookup(requirements: dict, min_similarity: float | None = None,
                  max_items: int | None = None) -> dict | None:
    """Wrapper over `lookup` returning the discovery-result shape consumed by
    the pipeline (`discovery.discover` cache-hit branch) and the cascade Tier-2
    pre-seed ({cache_hit, source, similarity, count, recommendations})."""
    req = normalize_requirements(requirements)
    if req is None:
        return None
    cached = lookup(req, min_similarity=min_similarity)
    if not cached:
        return None
    recs = cached.get("results", [])
    if max_items is not None:
        recs = recs[:max_items]
    return {
        "cache_hit": True,
        "source": cached.get("source", "cache"),
        "similarity": cached.get("similarity"),
        "count": len(recs),
        "raw_count": len(recs),
        "recommendations": recs,
    }


def put(requirements: dict, results: list[dict], source: str = "serpapi") -> int | None:
    """Persist a normalized candidate set for future semantic reuse."""
    req = normalize_requirements(requirements)
    if req is None:
        return None
    query_norm = requirements_to_query(req)
    try:
        payload = json.dumps(results, default=str)
        embed = json.dumps(requirements_embedding(req))
        req_payload = json.dumps(req, default=str)
    except Exception:  # noqa: BLE001
        return None
    db = db_module.SessionLocal()
    try:
        row = SearchCache(
            query_text=json.dumps(req, default=str),
            query_normalized=query_norm,
            embedding=embed,
            requirements_json=req_payload,
            results_json=payload,
            source=source,
            quality_score=0.5,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=SEMANTIC_CACHE_TTL_SECONDS),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def store(requirements: dict, results: list[dict], source: str = "serpapi") -> int | None:
    """Phase-3 store: persist non-empty, normalized results or return None.
    Matching `put` semantics for everything else (same row shape, same TTL)."""
    if not isinstance(results, list) or not results:
        return None
    return put(requirements, results, source=source)


def prune_expired(keep_rows: int = 500) -> int:
    """Delete expired cache rows (plus a bounded number of stale rows) so the
    table can't grow unboundedly. Idempotent; never raises."""
    deleted = 0
    db = db_module.SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired = db.query(SearchCache).filter(
            (SearchCache.expires_at.is_(None))
            | (SearchCache.expires_at <= now)
        )
        expired_rows = expired.count()
        if expired_rows:
            expired.delete(synchronize_session=False)
            deleted += expired_rows
        alive = db.query(SearchCache).order_by(SearchCache.created_at.desc())
        if alive.count() > keep_rows:
            stale = alive.offset(keep_rows).all()
            for row in stale:
                db.delete(row)
            deleted += len(stale)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        deleted = 0
    finally:
        db.close()
    return deleted