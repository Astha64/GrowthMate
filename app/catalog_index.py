"""
Deterministic catalog RAG index (Rev 3, Phase 2).

A flat, on-disk, exact-k-NN index over merchant catalog SKUs so the
agent-readable catalog feed (Phase 5) and the optional growth agent's product
search (Phase 6) can retrieve catalogue rows without re-running embedding on
the hot path. Vectors come from `app.embeddings` (deterministic hashing-trick),
so results are reproducible and fully offline; the catalog is 10-60 SKUs, so
brute-force dot products are trivially fast and dependency-free.

Invariants
----------
- Deterministic: same products + same query => same ranking. Ties break by
  (-score, sku). Rebuilds are content-addressed (build_hash over the rows).
- Fail-open: `load()`/`retrieve()` never raise on I/O or parsing problems —
  a missing/corrupt index degrades to an empty result set.
- An "embedding" here is just the deterministic unit vector from
  `embeddings.embed_text(names void names + category + description)` — price /
  stock / priority stay metadata columns alongside the vector (never inside it).
- Price/stock freshness is not an index concern: rows carry the latest DB
  metadata on every `build()`; the vector describes only the product text.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

from app.config import CATALOG_INDEX_PATH, CATALOG_INDEX_MIN_SCORE, EMBEDDING_HASH_DIM
from app.embeddings import cosine, embed_text

INDEX_SCHEMA = "catalog_index/v1"
INDEX_PATH = CATALOG_INDEX_PATH

_INDEX = None  # lazy: {"rows": [...], "matrix": [[...]], "build_hash": str}


def _product_text(product: dict) -> str:
    """Canonical embedding text: semantic_text when present, else the assembled
    name/category/description — mirrors merchant-capture retrieval."""
    semantic = (product.get("semantic_text") or "").strip()
    if semantic:
        return semantic
    return " ".join(
        str(product.get(k) or "")
        for k in ("name", "category", "description")
    ).strip()


def _build_hash(rows: list[dict]) -> str:
    """Content address so rebuilds are idempotent (warm-up writes once)."""
    payload = sorted(
        (
            r["sku"],
            str(r.get("name") or ""),
            str(r.get("category") or ""),
            str(r.get("description") or ""),
            str(r.get("price") or ""),
            str(r.get("stock") or ""),
            str(r.get("currency") or ""),
            str(r.get("merchant_priority") or ""),
            str(r.get("semantic_text") or ""),
        )
        for r in rows
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def build(products: Optional[list[dict]] = None, index_path: Optional[str] = None) -> dict:
    """Build (or refresh) the on-disk index from `products` (dicts shaped like
    orchestration._merchant_products()) and return {path, count, build_hash}.

    When `products` is None the index is built from the DB `Product` table. The
    write is atomic (tmp file + rename). Only touches disk when the content
    hash changed, so repeated warm-ups are cheap.
    """
    from app.db import SessionLocal
    from app.models import Product

    if products is None:
        session = SessionLocal()
        try:
            products = [
                {
                    "sku": r.sku, "name": r.name, "price": r.price,
                    "stock": r.stock, "category": r.category,
                    "description": r.description or "",
                    "currency": r.currency,
                    "merchant_priority": r.merchant_priority,
                    "semantic_text": r.semantic_text or "",
                }
                for r in session.query(Product).all()
            ]
        finally:
            session.close()

    products = sorted(products, key=lambda r: str(r.get("sku") or ""))
    build_hash = _build_hash(products)
    index_path = index_path or INDEX_PATH

    loaded = _read(index_path)
    if loaded and loaded.get("build_hash") == build_hash:
        _set_cache(loaded)
        return {"path": index_path, "count": len(loaded["rows"]), "build_hash": build_hash}

    rows, matrix = [], []
    for product in products:
        vec = embed_text(_product_text(product), dim=EMBEDDING_HASH_DIM)
        rows.append(
            {
                "sku": product.get("sku"),
                "name": product.get("name") or "",
                "category": product.get("category") or "",
                "description": product.get("description") or "",
                "price": float(product.get("price") or 0.0),
                "stock": int(product.get("stock") or 0),
                "currency": product.get("currency") or "INR",
                "merchant_priority": float(product.get("merchant_priority") or 0.0),
            }
        )
        matrix.append(vec)

    payload = {
        "schema": INDEX_SCHEMA,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "dim": EMBEDDING_HASH_DIM,
        "build_hash": build_hash,
        "rows": rows,
        "matrix": matrix,
    }
    _atomic_write(index_path, payload)
    _set_cache(payload)
    return {"path": index_path, "count": len(rows), "build_hash": build_hash}


def load(index_path: Optional[str] = None) -> Optional[dict]:
    """Load the index from disk into the module cache. Returns None on any
    failure (missing/corrupt file) — callers must treat that as empty."""
    if _INDEX is not None:
        return _INDEX
    loaded = _read(index_path or INDEX_PATH)
    if loaded is not None:
        _set_cache(loaded)
    return loaded


def retrieve(query_text: str, k: int = 5, min_score: Optional[float] = None,
             index_path: Optional[str] = None) -> list[dict]:
    """Exact k-NN over the catalog index: top-k rows by cosine, ties broken by
    (-score, sku). Never raises; never calls the LLM."""
    index = load(index_path)
    if index is None:
        return []
    threshold = CATALOG_INDEX_MIN_SCORE if min_score is None else min_score
    q = embed_text(query_text, dim=index.get("dim", EMBEDDING_HASH_DIM))
    scored = []
    for row, vec in zip(index.get("rows", []), index.get("matrix", [])):
        score = cosine(q, vec)
        if score >= threshold:
            scored.append((score, row["sku"], {**row, "score": round(score, 4)}))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [item for _, _, item in scored[:k]]


def rows() -> list[dict]:
    """The indexed rows (metadata + embedding vector) — the data source for the
    agent-readable catalog feed. Never raises: [] on a missing index."""
    index = load()
    if index is None:
        return []
    out = []
    for row, vec in zip(index.get("rows", []), index.get("matrix", [])):
        out.append({**row, "embedding": vec, "dim": index.get("dim")})
    return out


def is_current(products: Optional[list[dict]] = None) -> bool:
    """Whether the on-disk index matches the given (or DB) products — cheap
    idempotency check for the post-seed warm-up."""
    from app.db import SessionLocal
    from app.models import Product

    if products is None:
        session = SessionLocal()
        try:
            products = [
                _serialize(r) for r in session.query(Product).all()
            ]
        finally:
            session.close()
    loaded = _read(INDEX_PATH)
    return bool(loaded and loaded.get("build_hash") == _build_hash(products))


def backfill_embedding_columns() -> int:
    """Persist the deterministic vectors into `products.embedding` so callers
    that read the DB directly (buyer feed, tests) see the same index vectors.
    Best effort — never raises; returns the number of rows updated."""
    from app.db import SessionLocal
    from app.models import Product

    index = load()
    if index is None:
        return 0
    vectors = {row["sku"]: vec for row, vec in zip(index["rows"], index["matrix"])}
    session = SessionLocal()
    updated = 0
    try:
        for row in session.query(Product).all():
            vec = vectors.get(row.sku)
            if vec is None:
                continue
            if row.embedding != json.dumps(vec):
                row.embedding = json.dumps(vec)
                updated += 1
        session.commit()
    except Exception:  # noqa: BLE001 — non-authoritative enrichment
        session.rollback()
        updated = 0
    finally:
        session.close()
    return updated


def clear_cache() -> None:
    """Reset the module index cache (tests / incremental rebuilds)."""
    global _INDEX
    _INDEX = None


def _serialize(row) -> dict:
    return {
        "sku": row.sku, "name": row.name, "price": row.price,
        "stock": row.stock, "category": row.category,
        "description": row.description or "",
        "currency": row.currency,
        "merchant_priority": row.merchant_priority,
        "semantic_text": row.semantic_text or "",
    }


def _set_cache(payload: dict) -> None:
    global _INDEX
    _INDEX = payload


def _read(index_path: str) -> Optional[dict]:
    try:
        with open(index_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict) or payload.get("schema") != INDEX_SCHEMA:
            return None
        rows, matrix = payload.get("rows", []), payload.get("matrix", [])
        if not rows or len(rows) != len(matrix):
            return None
        return payload
    except (OSError, ValueError, TypeError):
        return None


def _atomic_write(index_path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(index_path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".index.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, index_path)
        return tmp
    except Exception:  # noqa: BLE001 — fail-open index
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise