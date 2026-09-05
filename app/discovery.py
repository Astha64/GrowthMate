"""
Dynamic product discovery (Revision 3) — cache-aware async pipeline (LLD §8-§10).

Revision-3 changes vs Rev-2:
  - `semantic_cache` sits in front of the external provider (LLD §8): a fresh,
    similar hit short-circuits SerpAPI entirely and sets cache_hit for the audit
    trail.
  - SerpAPI is called via async httpx with a short timeout (config) instead of
    a blocking 15s requests call (LLD §9).
  - SerpAPI results are persisted to ExternalProductListing rows and cached in
    SearchCache. Cached/persisted listings are market intelligence ONLY — the
    merchant catalog (products table) is the only source for prices at checkout.
  - Every provider failure degrades gracefully. Without SERPAPI_KEY the offline
    mock catalog keeps the demo and tests working end-to-end.

`search_external_sources(requirements)` returns raw candidate dicts and is the
deterministic seam tests monkeypatch to simulate live sources.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx

from app import db as db_module
from app.config import SERPAPI_TIMEOUT_SECONDS
from app.models import ExternalProductListing
from app.semantic_cache import cached_lookup as cache_cached_lookup
from app.semantic_cache import put as cache_put

# ---------------------------------------------------------------------------
# Deterministic offline mock "external" sources (kept from Rev-2).
# ---------------------------------------------------------------------------

_MOCK_SOURCES: dict[str, list[dict]] = {
    "marketplace_api": [
        {
            "source_url": "https://market.test/products/1",
            "title": "Nike Air Zoom Pegasus 40",
            "price": 2100,
            "currency": "INR",
            "brand": "Nike",
            "features": ["running", "road", "cushioned"],
            "rating": 4.5,
            "availability": "in_stock",
        },
        {
            "source_url": "https://market.test/products/2",
            "title": "Adidas Ultraboost Light",
            "price": 3600,
            "currency": "INR",
            "brand": "Adidas",
            "features": ["running", "cushioned"],
            "rating": 4.7,
            "availability": "in_stock",
        },
        {
            "source_url": "https://market.test/products/3",
            "title": "Asics Gel-Kayano 30",
            "price": 2450,
            "currency": "INR",
            "brand": "Asics",
            "features": ["running", "stability"],
            "rating": 4.4,
            "availability": "in_stock",
        },
    ],
    "price_comparison_api": [
        {
            "source_url": "https://compare.test/p/1",
            "title": "Nike Air Zoom Pegasus 40",
            "price": 2050,
            "currency": "INR",
            "brand": "Nike",
            "features": ["running", "road"],
            "rating": 4.6,
            "availability": "in_stock",
        },
        {
            "source_url": "https://compare.test/p/2",
            "title": "Puma Velocity Nitro 2",
            "price": 1900,
            "currency": "INR",
            "brand": "Puma",
            "features": ["running", "road"],
            "rating": 4.2,
            "availability": "in_stock",
        },
        {
            "source_url": "https://compare.test/p/3",
            "title": "New Balance Fresh Foam 880",
            "price": 2700,
            "currency": "INR",
            "brand": "New Balance",
            "features": ["running", "daily"],
            "rating": 4.5,
            "availability": "out_of_stock",
        },
    ],
    "deals_api": [
        {
            "source_url": "https://deals.test/d/1",
            "title": "Nike Revolution 6",
            "price": 1799,
            "currency": "INR",
            "brand": "Nike",
            "features": ["running", "budget"],
            "rating": 4.1,
            "availability": "in_stock",
        },
        {
            "source_url": "https://deals.test/d/2",
            "title": "Reebok Floatride Energy 4",
            "price": 2300,
            "currency": "INR",
            "brand": "Reebok",
            "features": ["running", "road"],
            "rating": 4.3,
            "availability": "in_stock",
        },
    ],
}


def _build_search_query(requirements: dict) -> str:
    category = (requirements.get("category") or "").strip()
    keywords = requirements.get("keywords") or []
    terms = [t for t in ([(requirements.get("product_type") or category or "")] + list(keywords)) if t]
    if not terms:
        terms = ["product"]
    return " ".join(terms) + " buy"


async def _serpapi_shopping(query: str, api_key: str) -> list[dict]:
    """Async SerpAPI Google Shopping request with a hard short timeout."""
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google_shopping",
        "q": query,
        "api_key": api_key,
        "hl": "en",
        "gl": "in",
        "num": 10,
    }
    async with httpx.AsyncClient(timeout=SERPAPI_TIMEOUT_SECONDS) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("shopping_results") or []
    candidates: list[dict] = []
    for item in results:
        price = 0.0
        price_str = item.get("price") or ""
        try:
            price = float(str(price_str).replace("₹", "").replace(",", "").strip())
        except ValueError:
            price = 0.0
        candidates.append(
            {
                "source_url": item.get("link") or item.get("product_link") or "",
                "title": item.get("title") or item.get("name") or "",
                "price": price,
                "currency": "INR",
                "brand": item.get("brand") or "",
                "features": [],
                "rating": float(item.get("rating") or 0.0),
                "availability": "in_stock" if item.get("in_stock") else "unknown",
            }
        )
    return candidates


def _mock_relevant(raw_items: list[dict], requirements: dict) -> list[dict]:
    """Keep only mock listings topically related to the search terms, so the
    demo doesn't flood running shoes for e.g. a desk-lamp query. A query with
    no detectable terms falls back to the full mock set (deterministic)."""
    terms = [
        str(t).strip().lower()
        for t in (
            ([requirements.get("category")]
             + [requirements.get("product_type")]
             + list(requirements.get("keywords") or [])
             + list(requirements.get("required_features") or []))
            or []
        )
        if t
    ]
    if not terms:
        return raw_items
    out = []
    for item in raw_items:
        hay = " ".join(
            [str(item.get("title", "")).lower(),
             str(item.get("brand", "")).lower(),
             " ".join(str(f).lower() for f in item.get("features", []))]
        )
        if any(t in hay for t in terms):
            out.append(item)
    return out


def search_external_sources(requirements: dict) -> list[dict]:
    """Raw candidates from the configured live provider, mock fallback.

    Deterministic seam for tests: monkeypatch this to inject candidate sets.
    Live SerpAPI runs via the async provider; a failing/empty source is
    skipped rather than failing discovery (ARCHITECTURE §7). The offline mock
    is filtered for topical relevance so results are honest for off-catalog
    queries.
    """
    query = _build_search_query(requirements)
    api_key = os.getenv("SERPAPI_KEY")

    candidates: list[dict] = []
    if api_key:
        try:
            candidates = list(asyncio.run(_serpapi_shopping(query, api_key)))
        except Exception:  # noqa: BLE001 - failing live source never fails discovery
            candidates = []

    if not candidates:
        for source_name, raw_items in _MOCK_SOURCES.items():
            try:
                candidates.extend(_mock_relevant(raw_items, requirements))
            except Exception:  # noqa: BLE001
                continue
    return candidates


# ---------------------------------------------------------------------------
# Pipeline stages (kept from Rev-2 — deterministic internal functions).
# ---------------------------------------------------------------------------

def extract_product_data(raw: dict) -> dict:
    return {
        "source_url": raw.get("source_url") or raw.get("url") or "",
        "title": raw.get("title") or raw.get("name") or "",
        "price": float(raw.get("price") or raw.get("amount") or 0.0),
        "currency": raw.get("currency") or "INR",
        "brand": raw.get("brand") or "",
        "features": raw.get("features") or raw.get("tags") or [],
        "rating": float(raw.get("rating") or 0.0),
        "availability": raw.get("availability") or "unknown",
    }


def normalize_listing(extracted: dict) -> dict:
    title = (extracted.get("title") or "").strip().lower()
    brand = (extracted.get("brand") or "").strip().lower()
    return {
        "source_url": extracted.get("source_url", ""),
        "title": extracted.get("title", ""),
        "price": extracted.get("price", 0.0),
        "currency": extracted.get("currency", "INR"),
        "brand": extracted.get("brand", ""),
        "features": list(extracted.get("features") or []),
        "rating": extracted.get("rating", 0.0),
        "availability": extracted.get("availability", "unknown"),
        "_dedup_key": f"{title}|{brand}",
        "_dedup_group_id": None,
    }


def deduplicate_listings(normalized: list[dict]) -> list[dict]:
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for listing in normalized:
        groups[listing["_dedup_key"]].append(listing)

    deduped: list[dict] = []
    for key, members in groups.items():
        members.sort(key=lambda m: (-m.get("rating", 0.0), m.get("price", 0.0)))
        winner = dict(members[0])
        winner["_dedup_group_id"] = key
        deduped.append(winner)
    return deduped


def meets_hard_constraints(product: dict, requirements: dict) -> bool:
    budget = requirements.get("budget")
    if budget is not None and product.get("price", 0.0) > float(budget):
        return False

    req_features = requirements.get("required_features") or []
    haystack = " ".join(
        [product.get("title", "").lower()]
        + [str(f).lower() for f in product.get("features", [])]
    )
    for feature in req_features:
        tokens = str(feature).lower().split()
        if not tokens:
            continue
        if not any(tok in haystack for tok in tokens):
            return False

    availability = requirements.get("requires_availability", True)
    if availability and product.get("availability") == "out_of_stock":
        return False

    return True


def rank_by_fit(products: list[dict], requirements: dict) -> list[dict]:
    budget = requirements.get("budget")
    req_keywords = [
        str(k).lower() for k in (requirements.get("required_features") or [])
    ] + [str(k).lower() for k in (requirements.get("keywords") or [])]

    scored: list[tuple[float, dict]] = []
    for product in products:
        title = product.get("title", "").lower()
        features = [str(f).lower() for f in product.get("features", [])]
        haystack = " ".join([title] + features)

        keyword_bonus = sum(1 for kw in req_keywords if kw in haystack) / max(1, len(req_keywords or [1]))

        price = product.get("price", 0.0)
        if budget is not None and budget > 0:
            closeness = max(0.0, 1.0 - abs(price - float(budget)) / float(budget))
        else:
            closeness = 1.0

        req_ft = requirements.get("required_features") or []
        feature_bonus = sum(1 for f in req_ft if str(f).lower() in haystack) / max(1, len(req_ft or [1]))

        availability_bonus = 1.0 if product.get("availability") not in ("out_of_stock", "unknown") else 0.0

        score = (0.4 * keyword_bonus) + (0.3 * closeness) + (0.2 * feature_bonus) + (0.1 * availability_bonus)

        why_parts = []
        if keyword_bonus > 0:
            why_parts.append("matches your keywords")
        if product.get("price", 0) <= (budget or float("inf")):
            why_parts.append(f"within your ₹{budget} budget")
        if feature_bonus > 0:
            why_parts.append("covers your required features")
        if product.get("availability") == "out_of_stock":
            why_parts.append("currently out of stock")
        why = ", ".join(why_parts) if why_parts else "best fit among what's available"

        product["_score"] = round(score, 3)
        product["_why"] = why
        scored.append((score, product))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored]


# ---------------------------------------------------------------------------
# Public entry point — cache-aware, persists listings, never raises.
# ---------------------------------------------------------------------------

def discover(requirements: dict, session_id: str) -> dict:
    """Run discovery end-to-end.

    1. Semantic cache lookup (fresh + similar) -> short-circuit on hit.
    2. Otherwise: search external sources -> extract -> normalize -> dedupe ->
       filter -> rank -> top N; persist listings; cache the normalized set.
    3. Provider/cache failures degrade to no results — never raise.
    """
    cached = cache_cached_lookup(requirements)
    if cached:
        return cached

    candidates = search_external_sources(requirements)
    extracted = [extract_product_data(c) for c in candidates]
    normalized = [normalize_listing(e) for e in extracted]
    deduped = deduplicate_listings(normalized)
    filtered = [p for p in deduped if meets_hard_constraints(p, requirements)]
    ranked = rank_by_fit(filtered, requirements)

    top = ranked[:3]
    recs = [
        {
            "name": p.get("title", ""),
            "price": p.get("price", 0.0),
            "currency": p.get("currency", "INR"),
            "source": p.get("source_url", ""),
            "why": p.get("_why", ""),
            "_dedup_group_id": p.get("_dedup_group_id"),
        }
        for p in top
    ]

    _persist_listings(session_id, recs)
    try:
        # Never cache an empty result set — a fresh nearby query would short-
        # circuit on it and return "no listings" forever.
        if recs:
            cache_put(requirements, recs)
    except Exception:  # noqa: BLE001
        pass

    return {
        "cache_hit": False,
        "source": "serpapi",
        "raw_count": len(candidates),
        "count": len(recs),
        "recommendations": recs,
    }


def no_match_reply(discovery_result: dict) -> str:
    """Deterministic "no merchant match" reply from discovery results.

    Shared by the chat pipeline (discover_node) and the cascade Tier-2
    short-circuit so both produce byte-identical copy (Rev 3, Phase 1).

    Every market/SERP listing shown here IS addable (Rev 3 — external matches
    are payable via their EXT-xxx SKU), so the copy numbers the options with
    the exact SKUs the suggestion list resolves.
    """
    recs = discovery_result.get("recommendations") or []
    if not recs:
        return (
            "I couldn't find a strong merchant match for that requirement. "
            "Try rephrasing with a brand, category, or price range (for example "
            "\"wireless mouse under 3000\") and I'll search the market again."
        )
    lines = [
        "We don't stock a direct match, so here's what the market shows — "
        "you can add any of these to your cart:"
    ]
    for i, r in enumerate(recs[:3], 1):
        lines.append(
            f"  {i}) {r.get('name')} (EXT-{i:03d}) — ₹{r.get('price')} ({r.get('currency')})"
        )
    lines.append(
        'Reply "1", "2" or "3" (or say "add EXT-001") to add it to your cart, '
        'then say "checkout" when ready.'
    )
    return "\n".join(lines)


def _persist_listings(session_id: str, recs: list[dict]) -> None:
    """Persist normalized candidates to ExternalProductListing (market intel)."""
    if not recs:
        return
    db = db_module.SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(hours=24)
        for r in recs:
            db.add(
                ExternalProductListing(
                    session_id=session_id,
                    source=r.get("source") or "external",
                    source_url=r.get("source", ""),
                    name=r.get("name", ""),
                    price=float(r.get("price", 0.0) or 0),
                    currency=r.get("currency", "INR"),
                    features_json=json.dumps([]),
                    availability="in_stock",
                    dedup_group_id=r.get("_dedup_group_id"),
                    retrieved_at=now,
                    expires_at=expiry,
                )
            )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()