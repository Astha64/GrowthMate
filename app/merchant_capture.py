"""
Merchant Capture (Revision 3) — deterministic scoring to decide whether the
merchant can fulfill the query (LLD §11, HLD §9).

Scores the best merchant product against structured requirements on named,
explainable factors, then maps the score to one of three capture verdicts:

  - "capture"            -> SKU/product satisfies requirements -> move to quote
  - "recommend_offers"   -> product fits enough to steer, but surface the
                            next-best-offer engine on top of it
  - "no_merchant_match"  -> deterministic decline (external / out-of-range /
                            insufficient stock) -> do NOT answer with a made-up
                            merchant product

The LLM is never asked to convert an external product into a payable merchant
product — that is a deterministic boundary problem solved here.
"""

from app.config import (
    CAPTURE_W_ATTACH,
    CAPTURE_W_MARGIN,
    CAPTURE_W_PRICE,
    CAPTURE_W_PRIORITY,
    CAPTURE_W_REQUIREMENT,
    CAPTURE_W_SEMANTIC,
    CAPTURE_W_STOCK,
    STOCK_CONFIDENCE_THRESHOLD,
)
from app.embeddings import cosine, embed_text


def _semantic_component(product: dict, requirements: dict) -> float:
    """Similarity between the merchant product text and the query. If either
    side lacks an embedding, fall back to bag-of-words overlap."""
    p_text = product.get("semantic_text") or " ".join(
        [
            product.get("name", ""),
            product.get("category", "") or "",
            product.get("description", "") or "",
        ]
    )
    try:
        p_vec = product.get("_embedding") or embed_text(p_text)
        score = cosine(p_vec, embed_text(_requirements_text(requirements)))
    except Exception:  # noqa: BLE001
        score = 0.0
    return score


def _requirements_text(requirements: dict) -> str:
    category = requirements.get("category") or requirements.get("product_type") or ""
    keywords = " ".join(requirements.get("keywords") or requirements.get("required_features") or [])
    return " ".join([category, keywords]).strip()


def _requirement_component(product: dict, requirements: dict) -> float:
    category = (requirements.get("category") or "").strip().lower()
    keywords = [str(k).lower() for k in (requirements.get("keywords") or [])]
    haystack = " ".join(
        [
            str(product.get("name", "")).lower(),
            str(product.get("category", "") or "").lower(),
            str(product.get("description", "") or "").lower(),
        ]
    )
    hits = 0
    if category and category in haystack:
        hits += 1
    hits += sum(1 for kw in keywords if kw and kw in haystack)
    total = 1 + (len(keywords) if keywords else 0)
    return min(1.0, hits / max(1, total))


def _price_component(product: dict, requirements: dict) -> float:
    """Budged closeness: 1.0 when price is comfortably within budget, graded
    down as price approaches/surpasses the ceiling. No budget -> 0.8 default."""
    budget = requirements.get("budget")
    if budget is None:
        return 0.8
    budget = float(budget)
    if budget <= 0:
        return 0.5
    price = float(product.get("price", 0.0))
    ratio = price / budget
    if ratio <= 0.8:
        return 1.0
    if ratio <= 1.0:
        return 0.7
    return max(0.0, 0.5 - (ratio - 1.0))


def _stock_component(product: dict) -> float:
    stock = int(product.get("stock") or 0)
    if stock <= 0:
        return 0.0
    if stock >= STOCK_CONFIDENCE_THRESHOLD:
        return 1.0
    return round(stock / STOCK_CONFIDENCE_THRESHOLD, 2)


def _margin_component(product: dict) -> float:
    margin = product.get("margin_pct")
    if margin is None:
        return 0.3
    return min(1.0, max(0.0, float(margin) / 0.5))


def _attach_component(product: dict, requirements: dict) -> float:
    """Explicitly requested product_type/brand present in the catalog boosts
    attachment confidence (the user asked for exactly this kind of item)."""
    req_type = (requirements.get("product_type") or "").lower()
    req_brand = (requirements.get("brand") or "").lower()
    if not req_type and not req_brand:
        return 0.5
    hay = f"{str(product.get('name','')).lower()} {str(product.get('category','') or '').lower()}"
    hit = (req_type and req_type in hay) or (req_brand and req_brand in hay)
    return 1.0 if hit else 0.1


def score_merchant_product(product: dict, requirements: dict) -> dict:
    """Deterministic merchant-fit score with named components and reason codes."""
    components = {
        "semantic": CAPTURE_W_SEMANTIC * _semantic_component(product, requirements),
        "requirement": CAPTURE_W_REQUIREMENT * _requirement_component(product, requirements),
        "price": CAPTURE_W_PRICE * _price_component(product, requirements),
        "stock": CAPTURE_W_STOCK * _stock_component(product),
        "margin": CAPTURE_W_MARGIN * _margin_component(product),
        "attach": CAPTURE_W_ATTACH * _attach_component(product, requirements),
        "priority": CAPTURE_W_PRIORITY * min(1.0, float(product.get("merchant_priority") or 0.0)),
    }
    score = round(sum(components.values()), 3)
    return {"score": score, "components": components, "capped": min(1.0, score)}


def capture_verdict(product: dict | None, requirements: dict) -> dict:
    """Map the best merchant product to a capture verdict (LLD §11.2)."""
    if product is None:
        return {
            "verdict": "no_merchant_match",
            "reason": "no merchant catalog product satisfies these requirements",
            "score": 0.0,
        }
    result = score_merchant_product(product, requirements)
    score = result["capped"]
    stock = int(product.get("stock") or 0)
    price = float(product.get("price") or 0.0)
    budget = requirements.get("budget")

    if stock <= 0:
        return {
            **result,
            "verdict": "no_merchant_match",
            "reason": "merchant product is out of stock",
        }
    if budget is not None and price > float(budget):
        return {
            **result,
            "verdict": "no_merchant_match",
            "reason": "product exceeds the stated budget",
        }
    if score >= 0.7:
        return {
            **result,
            "verdict": "capture",
            "reason": "strong merchant fit",
            "product": _product_card(product),
        }
    if score >= 0.45:
        return {
            **result,
            "verdict": "recommend_offers",
            "reason": "partial fit — surface next-best-offer",
            "product": _product_card(product),
        }
    return {
        **result,
        "verdict": "no_merchant_match",
        "reason": "product does not satisfy the requirements deterministically",
    }


def top_merchant_matches(products: list[dict], requirements: dict, top_n: int = 3) -> list[dict]:
    """Rank the merchant catalog by deterministic fit score and return the top
    `top_n` capture-able candidates (verdict capture / recommend_offers), best
    first. Over-budget / out-of-stock / weak-fit products are filtered out just
    like the single-candidate path — no-LLM, no negotiation."""
    verdicts = [capture_verdict(p, requirements) for p in products]
    keep = [v for v in verdicts if v.get("verdict") in ("capture", "recommend_offers")]
    keep.sort(key=lambda v: float(v.get("capped") or v.get("score") or 0.0), reverse=True)
    return keep[:top_n]


def _product_card(product: dict) -> dict:
    return {
        "sku": product.get("sku"),
        "name": product.get("name"),
        "price": product.get("price"),
        "currency": product.get("currency", "INR"),
        "stock": product.get("stock"),
        "category": product.get("category"),
        "image_url": product.get("image_url"),
    }