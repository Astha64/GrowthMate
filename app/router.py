"""
Fast intent/domain/risk router (Revision 3) — deterministic rules first.

Implements LLD §5 + the build-prompt routing rules:

  - rule/pattern classification first (no network, no LLM)
  - explicit SKU / cart / total / catalog / checkout-previews run on the fast
    path with high confidence and `requires_llm=False`
  - open-ended product discovery uses category/budget extraction; only when
    a search intent is otherwise unclassifiable do we make ONE structured
    Gemini extraction call (`extract_requirements_llm`), and that call is
    failure-tolerant — it degrades to `{}` (=> clarification) rather than crash

RouteDecision shape matches LLD §3.2.
"""

import json
import os
import re

from app.config import ROUTER_CONFIDENCE_FAST_PATH, ROUTER_CONFIDENCE_LLM_PATH
from app.guardrail import is_explicit_approval

# ---------------------------------------------------------------------------
# Intent / affordance vocabulary
# ---------------------------------------------------------------------------

_SKU_RE = re.compile(r"\b([A-Z]{2,6}-\d{2,6})\b", re.IGNORECASE)
_QUANTITY_RE = re.compile(r"\b(\d+)\s*(?:x|times|units?|pieces?|items?)?\s+", re.IGNORECASE)
_TRAILING_QTY_RE = re.compile(r"\bqty\s*[:=]?\s*(\d+)\b", re.IGNORECASE)

_BUDGET_RE = re.compile(
    r"(?:under|below|less than|budget(?: is| of|:)?|upto|up to|max(?:imum)?)\s*"
    r"[₹rs]?\s*(\d[\d,]*)\s*(k|thousand)?(?![k\d])",
    re.IGNORECASE,
)

_CATEGORY_SYNONYMS = {
    "footwear": ["shoe", "shoes", "sneaker", "sneakers", "running shoe", "trainer", "boot", "sandal"],
    "apparel": ["tshirt", "t-shirt", "tee", "shirt", "hoodie", "jacket", "pants", "trousers", "chino", "apparel", "clothing", "clothes", "outfit", "wear", "dress", "garment"],
    "electronics": ["earbud", "headphone", "headphones", "charger", "speaker", "electronics", "laptop", "phone", "tablet", "gadget"],
    "home": ["mug", "lamp", "cushion", "furniture", "home", "decor", "diy", "desk lamp", "desk"],
    "accessories": ["wallet", "belt", "watch", "backpack", "sunglasses", "cap", "accessory"],
}

_FEATURE_HINTS = {
    "running": ["running", "run", "road", "marathon"],
    "wireless": ["wireless", "bluetooth", "true wireless", "tws"],
    "led": ["led", "adjustable"],
    "fast-charge": ["fast charg", "gan", "65w", "usb-c"],
    "leather": ["leather", "genuine"],
}

_ADD_RE = re.compile(r"\b(add|put|include|throw in|pick up|get me|i need .* in (?:my|the) cart)\b", re.IGNORECASE)
_REMOVE_RE = re.compile(r"\b(remove|delete|drop|take out|discard)\b", re.IGNORECASE)
_SET_QTY_RE = re.compile(r"\b(change|set|update|make it)\b.*\b(quantity|to)\b", re.IGNORECASE)
_SHOW_CART_RE = re.compile(r"\b(show|view|display|open|what.?s in|what is in)\b.*\b(cart|basket|bag)\b|\bmy (cart|basket|bag)\b", re.IGNORECASE)
_TOTAL_RE = re.compile(r"\b(total|subtotal|how much.*(?:pay|total|due)|balance|amount due)\b", re.IGNORECASE)
_CHECKOUT_RE = re.compile(r"\b(checkout|bill|ready to pay|prepare.*checkout|show.*checkout preview)\b", re.IGNORECASE)
_CATALOG_RE = re.compile(r"\b(catalog|catalogue|all (?:your )?products|what do you sell|list products|browse)\b", re.IGNORECASE)
_ORDER_STATUS_RE = re.compile(r"\b(status|track|where).*(order|payment)\b|\border.*(status|track)\b", re.IGNORECASE)
_INSIGHTS_RE = re.compile(r"\b(insight|analytics|top selling|top products|abandonment|growth|which products)\b", re.IGNORECASE)
_PURCHASE_RE = re.compile(r"\b(buy|purchase|order|pay for|complete.*purchase|proceed to payment)\b", re.IGNORECASE)
_SEARCH_RE = re.compile(r"\b(need|want|looking for|find|looking to buy|recommend|suggest|search for|budget)\b", re.IGNORECASE)
_LIVE_SEARCH_REQ_RE = re.compile(r"\b(market|alternatives?|compare|external|elsewhere|another store|other options|benchmark)\b", re.IGNORECASE)

_DOMAIN_HINTS = {
    "cart": _SHOW_CART_RE,
    "quote": _CHECKOUT_RE,
    "commerce": _TOTAL_RE,
    "catalog": _CATALOG_RE,
    "policy": _ORDER_STATUS_RE,
    "growth": _INSIGHTS_RE,
    "discovery": _SEARCH_RE,
}

# ---------------------------------------------------------------------------
# Deterministic feature extractors
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def extract_budget(text: str) -> float | None:
    m = _BUDGET_RE.search(text)
    if not m:
        return None
    try:
        amount = round(float(m.group(1).replace(",", "")), 2)
        if (m.group(2) or "").lower().startswith("k"):
            amount *= 1000.0
        return amount
    except ValueError:
        return None


def extract_category(text: str) -> str | None:
    lower = normalize(text)
    best: tuple[int, str] | None = None
    for category, synonyms in _CATEGORY_SYNONYMS.items():
        for word in synonyms:
            if word in lower:
                score = len(word)
                if best is None or score > best[0]:
                    best = (score, category)
    return best[1] if best else None


def extract_features(text: str) -> list[str]:
    lower = normalize(text)
    hits = [feat for feat, hints in _FEATURE_HINTS.items() if any(h in lower for h in hints)]
    return hits


def extract_sku(text: str) -> str | None:
    m = _SKU_RE.search(text)
    return m.group(1).upper() if m else None


def extract_quantity(text: str, default: int = 1) -> int:
    m = _TRAILING_QTY_RE.search(text) or _QUANTITY_RE.search(text)
    if not m:
        return default
    try:
        qty = int(m.group(1))
        return max(1, min(qty, 100))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(message: str, state: dict) -> dict:
    """Return a RouteDecision dict (LLD §3.2) for the latest user message.

    `state` is the compact session state (quote/preview/cart flags matter for
    approval and checkout decisions).
    """
    raw = message or ""
    text = normalize(raw)
    category = extract_category(text)
    budget = extract_budget(text)
    sku = extract_sku(text)
    features = extract_features(text)
    reason_codes: list[str] = []

    quote = state.get("quote")
    preview_shown = bool(quote) or bool(state.get("checkout_preview"))
    has_cart = bool(state.get("cart"))

    # 1) Explicit approval of a *shown* preview — fast path, high confidence.
    if preview_shown and is_explicit_approval(raw):
        return _decision(
            intent="purchase", action="approve_checkout", domain="commerce",
            complexity="low", risk="high", requires_llm=False,
            requires_live_search=False, requires_merchant_retrieval=False,
            confidence=0.98, reason_codes=["explicit_approval", "preview_shown"],
        )

    # 2) Cart mutations with an explicit SKU — deterministic, no LLM.
    if sku and (_ADD_RE.search(text) or _PURCHASE_RE.search(text)):
        reason_codes += ["explicit_sku"]
        return _decision(
            intent="cart", action="cart_add", domain="cart", complexity="low",
            risk="low", requires_llm=False, requires_live_search=False,
            requires_merchant_retrieval=True, confidence=0.97,
            reason_codes=reason_codes,
        )
    if sku and _REMOVE_RE.search(text):
        return _decision(
            intent="cart", action="cart_remove", domain="cart", complexity="low",
            risk="low", requires_llm=False, requires_live_search=False,
            requires_merchant_retrieval=True, confidence=0.97,
            reason_codes=["explicit_sku"],
        )
    if sku and _SET_QTY_RE.search(text):
        return _decision(
            intent="cart", action="cart_set", domain="cart", complexity="low",
            risk="low", requires_llm=False, requires_live_search=False,
            requires_merchant_retrieval=True, confidence=0.97,
            reason_codes=["explicit_sku"],
        )

    # 3) Affordance-style commands (no SKU needed).
    action, domain = _detect_affordance(text)
    if action:
        live = _LIVE_SEARCH_REQ_RE.search(text) is not None
        return _decision(
            intent=action, action=action, domain=domain, complexity="low",
            risk="high" if action == "approve_checkout" else "low",
            requires_llm=False, requires_live_search=live,
            requires_merchant_retrieval=(action == "checkout_preview"),
            confidence=0.96, reason_codes=["affordance"],
        )

    # 4) Purchase intent without an explicit SKU.
    if _PURCHASE_RE.search(text):
        if has_cart:
            return _decision(
                intent="purchase", action="approve_checkout", domain="commerce",
                complexity="low", risk="high", requires_llm=False,
                requires_live_search=False, requires_merchant_retrieval=False,
                confidence=0.93, reason_codes=["has_cart", "purchase_intent"],
            )
        return _decision(
            intent="purchase", action="cart_add", domain="commerce",
            complexity="low", risk="medium", requires_llm=False,
            requires_live_search=False, requires_merchant_retrieval=True,
            confidence=0.90, reason_codes=["purchase_intent_no_cart"],
        )

    # 5) Open-ended discovery / recommendation.
    if _SEARCH_RE.search(text) or category or budget is not None:
        live = bool(_LIVE_SEARCH_REQ_RE.search(text))
        if category:
            reason_codes += ["category_known"]
            conf = 0.95 if budget is not None else 0.88
        elif budget is not None:
            reason_codes += ["budget_known"]
            conf = 0.82
        else:
            conf = 0.7
        requires_llm = conf < ROUTER_CONFIDENCE_FAST_PATH
        return _decision(
            intent="discovery", action="recommend", domain="discovery",
            complexity="medium", risk="low", requires_llm=requires_llm,
            requires_live_search=live, requires_merchant_retrieval=True,
            confidence=round(conf, 2), reason_codes=reason_codes,
            category=category, budget=budget, features=features,
        )

    # 6) Too vague — ask a clarifying question (never guess with an LLM).
    return _decision(
        intent="clarify", action="clarify", domain="general", complexity="low",
        risk="low", requires_llm=False, requires_live_search=False,
        requires_merchant_retrieval=False, confidence=0.6,
        reason_codes=["no_intent"],
    )


def _detect_affordance(text: str) -> tuple[str | None, str]:
    if _REMOVE_RE.search(text):
        return "cart_remove", "cart"
    if _SET_QTY_RE.search(text):
        return "cart_set", "cart"
    if _SHOW_CART_RE.search(text) and ("total" in text or "show" in text):
        return "cart_show", "cart"
    if _TOTAL_RE.search(text):
        return "total", "commerce"
    if _CHECKOUT_RE.search(text):
        return "checkout_preview", "quote"
    if _CATALOG_RE.search(text):
        return "catalog", "catalog"
    if _ORDER_STATUS_RE.search(text):
        return "order_status", "policy"
    if _INSIGHTS_RE.search(text):
        return "growth_insights", "growth"
    if _ADD_RE.search(text) and not _SKU_RE.search(text):
        return "cart_add", "cart"
    if is_explicit_approval(text) and "checkout" in text:
        return "approve_checkout", "commerce"
    return None, "general"


def _decision(**kw) -> dict:
    d = {
        "intent": None,
        "domain": "general",
        "category": None,
        "budget": None,
        "features": None,
        "complexity": "low",
        "risk": "low",
        "action": "clarify",
        "requires_llm": False,
        "requires_live_search": False,
        "requires_merchant_retrieval": False,
        "confidence": 0.0,
        "reason_codes": [],
    }
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
# Optional structured requirement extraction (LLD §5 third rung).
# Failure must never leak to the client — returns {} and the caller clarifies.
# ---------------------------------------------------------------------------

def extract_requirements_llm(message: str) -> dict:
    """One lightweight structured Gemini call filling structured_requirements.

    Guarded end-to-end: any failure returns {} so the router can clarify
    instead of crashing. Returns tiny JSON keys only — never control flow.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model="gemini-3.5-flash-lite",
            google_api_key=os.getenv("GEMINI_API_KEY"),
        )
        prompt = (
            "Extract buyer purchase requirements from this request as strict JSON with keys: "
            "category, budget_max, features, brand, quantity. Use null when absent. "
            f"Request: {message!r}"
        )
        raw = llm.invoke(prompt)
        content = getattr(raw, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return {}
        allowed = {"category", "budget_max", "features", "brand", "quantity"}
        return {k: v for k, v in data.items() if k in allowed and v not in (None, "", [])}
    except Exception:  # noqa: BLE001 — degrade to empty; caller clarifies
        return {}


def extract_intent_llm(message: str) -> dict:
    """One structured Gemini call to rescue phrasings the deterministic router
    could not parse ("please take everything off my list").

    Returns tiny JSON keys only — NEVER control flow on its own. Any failure
    (bad key, non-JSON, unreachable) degrades to {} so the deterministic
    fallback (clarify/gather) runs untouched.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model="gemini-3.5-flash-lite",
            google_api_key=os.getenv("GEMINI_API_KEY"),
        )
        prompt = (
            "You classify a shopper's message for an e-commerce assistant. "
            "Return strict JSON with ONLY these keys: "
            'intent (one of "add_to_cart","search","show_cart","show_total",'
            '"checkout","clear_cart","remove","other"), '
            "product (the thing they want, or null), "
            "category (footwear|apparel|electronics|home|accessories or null), "
            "quantity (integer or null). "
            "Example: \"could you please wipe my cart clean\" -> "
            '{"intent":"clear_cart","product":null,"category":null,"quantity":null}. '
            f"Shopper message: {message!r}"
        )
        raw = llm.invoke(prompt)
        content = getattr(raw, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return {}
        allowed = {"intent", "product", "category", "quantity"}
        return {k: v for k, v in data.items() if k in allowed and v not in (None, "")}
    except Exception:  # noqa: BLE001 — degrade; deterministic fallback decides
        return {}