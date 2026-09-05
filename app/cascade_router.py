"""
Cascade router (Rev 3, Phase 1) — latency-optimizing intent dispatch.

The first node of the graph. It grades a turn from cheapest to most expensive
and either emits a *fast path* (no discovery, no LLM, no money) or fails OPEN
into the full agent pipeline:

  Tier 0 — deterministic affordances: greetings, thanks/goodbye, catalog
            browse, clear-cart, explicit merchant SKU mutations, cart show /
            total, checkout *preview*. Cheap, no DB-heavy work beyond a SKU
            lookup, never touches discovery.
  Tier 1 — first-turn product intent: grades confidence; a resolved *discover*
            intent still falls through to the pipeline (slot-filling + ranking
            must run), but the decision is recorded for the audit trail.
  Tier 2 — deterministic discover + fresh high-similarity semantic-cache hit
            AND no merchant capture match => short-circuit to the *identical*
            external-recommendation reply the pipeline would produce (the
            deterministic no-merchant-match path), skipping capture/external.
  Tier 3 — fall through to the agent pipeline (exact Rev-2 behaviour), FAIL-OPEN.

Money-safety contract (non-negotiable — pinned by
tests/test_cascade_never_fastpaths_payment.py):
  * route() NEVER emits a payment-executing action. Fast-path actions are
    limited to SAFE_ACTIONS / SAFE_TOOL_KINDS; the fast-path edges
    (route→tool→respond→audit and route→respond→audit) never cross
    approval/guardrail/pay, so `execute_payment` is structurally unreachable.
  * Any payment/approval phrasing, and any approval OR negation of a *shown*
    preview, fails open into the full pipeline (agent→approval→guardrail→pay),
    which is where approval + spend limits decide — never the cascade.
  * CASCADE_ENABLED=false reproduces Rev-2 byte-for-byte (every turn returns a
    Tier-3 fallthrough; route_node then delegates entirely to agent_node).
"""

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app import commerce
from app import router as router_module
from app.config import (
    CASCADE_ENABLED,
    CASCADE_TIER1_THRESHOLD,
    CASCADE_TIER2_THRESHOLD,
)

# Safe action names any tier may emit. None of these reaches `pay_node`
# (`approve_checkout` — the ONLY path into approval/guardrail/pay — is never
# a cascade action, and execution only lives on that path or /agent/checkout).
SAFE_ACTIONS = frozenset({"idle", "cart_show", "cart_total", "clear_cart", "checkout"})
# Tuple actions a tier may dispatch to `tool_node`: cart mutations and the
# propose-confirmation path only — no money, no approval.
SAFE_TOOL_KINDS = frozenset({"add", "remove", "propose_add"})

_SKU_RE = re.compile(r"\b([A-Z]{2,6}-\d{2,6})\b", re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _has(text: str, needles) -> bool:
    t = f" {text} "
    return any(n in t for n in needles)


# ---------------------------------------------------------------------------
# Payment-adjacent guard — fail-open into the full approval/guardrail/pay path.
# ---------------------------------------------------------------------------

_PAYMENT_NEEDLES = (
    "pay now", "pay", "approve", "confirm", "proceed", "charge",
    "complete the purchase", "complete my order", "complete the order",
    "buy now", "settle", "authorize", "authenticate", "make the payment",
    "payment", "card",
)

_PREVIEW_APPROVALS = (
    "yes", "yes please", "ok", "okay", "sure", "go ahead", "yep", "yeah",
    "yup", "that works", "sounds good", "do it", "looks good", "affirmative",
)

_PREVIEW_NEGATIONS = (
    "no", "nope", "nah", "not now", "cancel", "decline", "forget it", "stop",
    "wait", "hold on",
)


def _is_payment_adjacent(text: str, has_preview: bool) -> bool:
    if any(n in f" {text} " for n in _PAYMENT_NEEDLES):
        return True
    if has_preview:
        if any(n in f" {text} " for n in _PREVIEW_APPROVALS):
            return True
        if any(n in f" {text} " for n in _PREVIEW_NEGATIONS):
            return True
    return False


# ---------------------------------------------------------------------------
# Tier 0 — deterministic affordances (mirrors the agent's own tasking rules so
# a fast path never drifts from what the full pipeline would have answered).
# ---------------------------------------------------------------------------

_GREETINGS = {
    "hi", "hello", "hey", "yo", "howdy", "hi there", "hello there",
    "hey there", "good morning", "good afternoon", "good evening", "namaste",
}
_THANKS = {
    "thanks", "thank you", "thank you very much", "thx", "ty", "cheers",
}
_GOODBYES = {
    "bye", "goodbye", "good night", "see you", "see you later", "take care",
}

_BROWSE_RE = re.compile(
    r"^(?:"
    r"what do you (?:guys )?(?:sell|have|offer|stock)"
    r"|show me (?:your |the )?(?:catalog|catalogue|products|items)"
    r"|browse (?:the )?(?:catalog|catalogue|products)"
    r"|catalog(?:ue)?"
    r"|your (?:catalog|catalogue|products)"
    r"|what products do you (?:have|sell)"
    r"|products you (?:have|sell)"
    r"|list (?:your )?(?:products|items)"
    r"|all (?:your )?products"
    r")$"
)

_CLEAR_WORDS = ("clear", "empty", "wipe", "flush", "remove everything", "remove all")
_CLEAR_TARGETS = ("cart", "basket", "bag", "everything", "all items", "all the items")

_ADD_WORDS = ("add", "put", "grab", "get me", "get", "buy", "want", "need",
              "wish", "include", "throw in", "cart")
_REMOVE_WORDS = ("remove", "delete", "drop", "clear", "take out")
_SHOW_WORDS = ("show", "view", "display", "open")
_SHOW_NEEDLES = ("show", "view", "display", "open", "what", "my cart", "my basket", "my bag")
_CART_WORDS = ("cart", "basket", "bag")
_TOTAL_WORDS = ("total", "subtotal", "balance", "how much", "amount", "value")
_TOTAL_PHRASES = ("my total", "cart total", "total bill", "subtotal", "balance",
                  "amount due", "how much")
_CHECKOUT_WORDS = ("checkout", "check out")


def _greeting_reply() -> str:
    return (
        "Hi! I can search our catalog, add merchant SKUs to your cart, and "
        "set up a guarded checkout. What are you looking for?"
    )


def _thanks_reply() -> str:
    return "You're welcome! Let me know if you need anything else."


def _goodbye_reply() -> str:
    return "Goodbye! Come back anytime — I'll keep your cart ready."


def _browse_reply() -> str:
    """Deterministic catalog summary. Never raises — degrades to a pointer."""
    try:
        from app import orchestration

        products = orchestration._merchant_products()[:6]
    except Exception:  # noqa: BLE001 — browse is sugar
        products = []
    if not products:
        return "Here's our catalog: https://<host>/catalog — tell me a product to search."
    lines = ["Here's a taste of our catalog:"]
    for p in products:
        lines.append(f"  - {p['name']} ({p['sku']}) — ₹{p['price']} — {p['category']}")
    lines.append("Say a product, a SKU, or \"add <SKU>\" to place it in your cart.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tier 1 — first-turn *discover* intent grading (embedding confidence gate).
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _discover_examples() -> tuple[str, ...]:
    try:
        payload = json.loads(
            Path(__file__).with_name("intent_examples.json").read_text(encoding="utf-8")
        )
        return tuple(payload.get("discover") or ())
    except Exception:  # noqa: BLE001
        return ()


@lru_cache(maxsize=512)
def _embed(text: str) -> tuple[float, ...]:
    from app.embeddings import embed_text

    return tuple(embed_text(text or ""))


def _cos(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    from app.embeddings import cosine

    return cosine(list(a), list(b))


def _discover_confidence(text: str) -> float:
    """Max embedding cosine against the discover exemplars. Lexical-hashing
    vectors are cheap and deterministic; used only to confirm intent."""
    try:
        vec = _embed(text)
        best = 0.0
        for ex in _discover_examples():
            ev = _embed(ex)
            if ev:
                best = max(best, _cos(vec, ev))
        return round(best, 3)
    except Exception:  # noqa: BLE001 — grading is advisory
        return 0.0


# ---------------------------------------------------------------------------
# RouteDecision
# ---------------------------------------------------------------------------

@dataclass
class RouteDecision:
    intent: str | None
    tier: int
    fast_path: bool
    action: str | tuple | None = None
    direct_reply: str | None = None
    requirements: dict | None = None
    pre_seed: dict | None = None
    confidence: float | None = None
    reason_codes: list[str] = field(default_factory=list)

    def can_reach_pay_fast(self) -> bool:
        """True only if a fast-path action could tunnel toward `execute_payment`.
        By construction it never can — pinned by the safety test battery."""
        if not self.fast_path:
            return False
        if isinstance(self.action, tuple):
            return False
        return self.action not in SAFE_ACTIONS and self.action is not None


def _fallthrough(tier: int, *, intent: str | None = None,
                 requirements: dict | None = None, confidence: float | None = None,
                 reason_codes: list[str] | None = None) -> RouteDecision:
    return RouteDecision(intent=intent, tier=tier, fast_path=False,
                         requirements=requirements, confidence=confidence,
                         reason_codes=reason_codes or [])


def _action_is_safe(action) -> bool:
    if action is None:
        return False
    if isinstance(action, tuple):
        return action[0] in SAFE_TOOL_KINDS
    return action in SAFE_ACTIONS


def _fast(tier: int, action: str | tuple | None, *, intent: str | None = None,
          direct_reply: str | None = None, confidence: float | None = None,
          reason_codes: list[str] | None = None, pre_seed: dict | None = None) -> RouteDecision:
    assert action is None or _action_is_safe(action), (
        f"cascade attempted unsafe action {action!r}"
    )
    return RouteDecision(intent=intent, tier=tier, fast_path=True, action=action,
                         direct_reply=direct_reply, requirements=None,
                         confidence=confidence,
                         reason_codes=reason_codes or [], pre_seed=pre_seed)


# ---------------------------------------------------------------------------
# Helpers shared with the agent pipeline (lazy import avoids a module cycle —
# orchestration imports cascade_router at top, cascade_router only references
# orchestration's deterministic helpers at call time).
# ---------------------------------------------------------------------------

def _catalog_keywords(text: str) -> list[str]:
    from app import orchestration

    return orchestration._catalog_keywords(text) or []


def _explicit_item_terms(text: str) -> list[str]:
    from app import orchestration

    return orchestration._explicit_item_terms(text) or []


def _catalog_skus_for(terms: list[str]):
    from app import orchestration

    return orchestration._catalog_skus_for(terms) or set()


def _merchant_products() -> list[dict]:
    from app import orchestration

    return orchestration._merchant_products() or []


def _find_sku(text: str) -> str | None:
    m = _SKU_RE.search(text)
    if m:
        sku = m.group(1).upper()
        try:
            if commerce.get_product_by_sku(sku) is not None:
                return sku
        except Exception:  # noqa: BLE001
            return None
    return None


def _build_requirements(text: str) -> dict:
    """Exact mirror of orchestration._structured_requirements' deterministic
    outputs so Tier-2's merchant-capture verdict matches pipeline behaviour."""
    category = router_module.extract_category(text)
    features = list(router_module.extract_features(text) or [])
    catalog = _catalog_keywords(text)
    explicit = _explicit_item_terms(text)
    budget = router_module.extract_budget(text)
    req = {
        "category": category,
        "product_type": category,
        "budget": budget,
        "keywords": features + catalog + explicit,
        "required_features": features + catalog,
        "brand": None,
        "explicit_item": explicit,
    }
    signal = bool(category or features or catalog or budget is not None)
    return req, signal


# ---------------------------------------------------------------------------
# Main entry — `route()`. Never raises; total fail-open to Tier 3.
# ---------------------------------------------------------------------------

def route(message: str, *, session_id: str = "", is_first_turn: bool = False,
          has_preview: bool = False, cart_items: list | None = None,
          actor: str = "human") -> RouteDecision:
    del session_id, cart_items, actor  # reserved for later tiers / actors
    try:
        return _route_inner(message, is_first_turn=is_first_turn,
                            has_preview=has_preview)
    except Exception:  # noqa: BLE001 — fail-open: the agent pipeline decides
        return _fallthrough(3, reason_codes=["route_error"])


def _route_inner(message: str, *, is_first_turn: bool, has_preview: bool) -> RouteDecision:
    text = _norm(message)
    if not text:
        return _fallthrough(0, intent="other", reason_codes=["empty"])

    # --- Money guard (highest priority, fail-open; runs even when the cascade
    # is disabled so the safety contract never depends on config). ----------
    if _is_payment_adjacent(text, has_preview):
        intent = "checkout_approval" if (has_preview and any(
            n in f" {text} " for n in _PREVIEW_APPROVALS)) else "payment"
        return _fallthrough(0, intent=intent, confidence=0.99,
                            reason_codes=["payment_adjacent_guard"])

    if not CASCADE_ENABLED:
        # Bit-for-bit Rev-2: every turn is delegated to the agent pipeline.
        return _fallthrough(3, reason_codes=["cascade_disabled"])

    # --- Tier 0: deterministic affordances. --------------------------------
    if text in _GREETINGS:
        return _fast(0, "idle", intent="greeting", direct_reply=_greeting_reply(),
                     confidence=1.0, reason_codes=["greeting"])
    if text in _THANKS:
        return _fast(0, "idle", intent="thanks", direct_reply=_thanks_reply(),
                     confidence=1.0, reason_codes=["thanks"])
    if text in _GOODBYES:
        return _fast(0, "idle", intent="goodbye", direct_reply=_goodbye_reply(),
                     confidence=1.0, reason_codes=["goodbye"])
    if _BROWSE_RE.match(text):
        return _fast(0, "idle", intent="catalog", direct_reply=_browse_reply(),
                     confidence=0.98, reason_codes=["catalog_browse"])

    if _has(text, _CLEAR_WORDS) and _has(text, _CLEAR_TARGETS):
        return _fast(0, "clear_cart", intent="clear_cart", confidence=0.95,
                     reason_codes=["clear_cart"])

    sku = _find_sku(text)
    if sku:
        if _has(text, _REMOVE_WORDS):
            return _fast(0, ("remove", sku), intent="cart",
                         confidence=0.97, reason_codes=["explicit_sku"])
        if _has(text, _SHOW_WORDS + _CART_WORDS) and not _has(
            text, ("add", "include", "put", "buy", "purchase")
        ):
            return _fast(0, "cart_show", intent="cart",
                         confidence=0.97, reason_codes=["explicit_sku"])
        return _fast(0, ("add", sku), intent="cart",
                     confidence=0.97, reason_codes=["explicit_sku"])

    # Natural-language catalog add (mirrors agent rule 2b): a catalog keyword
    # with add-intent deserves the propose-confirmation, exactly as the
    # pipeline would handle it. Multi-SKU ambiguity stays on the pipeline.
    catalog = _catalog_keywords(text)
    if catalog and _has(text, _ADD_WORDS):
        skus = _catalog_skus_for(catalog)
        if len(skus) == 1:
            return _fast(0, ("propose_add", skus.pop()), intent="cart",
                         confidence=0.9, reason_codes=["catalog_keyword"])
        if len(skus) > 1:
            return _fallthrough(0, intent="cart",
                                reason_codes=["catalog_keyword_multi"])

    # Cart total with the same productish guard the pipeline uses.
    productish = bool(router_module.extract_category(text) or catalog)
    is_total = (
        _has(text, _CART_WORDS) and _has(text, _TOTAL_WORDS)
    ) or (
        _has(text, _TOTAL_PHRASES) and not productish
    )
    if is_total:
        return _fast(0, "cart_total", intent="cart_total", confidence=0.92,
                     reason_codes=["cart_total"])

    if any(w in f" {text} " for w in _SHOW_NEEDLES) and _has(text, _CART_WORDS):
        return _fast(0, "cart_show", intent="cart_show", confidence=0.92,
                     reason_codes=["cart_show"])

    if _has(text, _CHECKOUT_WORDS):
        words = len(text.split())
        if not productish and words <= 4:
            return _fast(0, "checkout", intent="checkout", confidence=0.9,
                         reason_codes=["checkout_preview"])

    # --- Tier 1: first-turn discover intent (grades, then falls through). ---
    if is_first_turn:
        category = router_module.extract_category(text)
        features = router_module.extract_features(text)
        budget = router_module.extract_budget(text)
        signal = bool(category or features or budget is not None)
        if signal:
            emb_conf = _discover_confidence(text)
            det_conf = 0.95 if category else (0.85 if (features or budget is not None) else 0.0)
            conf = round(max(det_conf, emb_conf), 3)
            if conf >= CASCADE_TIER1_THRESHOLD:
                req, _sig = _build_requirements(text)
                return _fallthrough(1, intent="discover", requirements=req,
                                    confidence=conf,
                                    reason_codes=["tier1_resolved", "first_turn"])
            return _fallthrough(1, intent="discover", confidence=conf,
                                reason_codes=["tier1_low_confidence"])
        return _fallthrough(1, reason_codes=["tier1_no_signal"])

    # --- Tier 2: cached discovery short-circuit (identical output guarantee
    # via the deterministic no-merchant-match verdict; see module docstring). --
    req, signal = _build_requirements(text)
    if signal:
        cached = _cached_discovery_lookup(text, req)
        if cached is not None:
            hit, sim = cached
            # The pipeline would run merchant capture first; it must agree
            # there is NO payable match before we pre-compose the reply.
            if _no_merchant_match(text, req):
                return _fast(
                    2, "idle", intent="discover", direct_reply=_no_match_reply(hit),
                    confidence=sim, reason_codes=["tier2_cache_hit", "no_merchant_match"],
                    pre_seed=hit,
                )
            return _fallthrough(2, intent="discover", requirements=req,
                                confidence=sim,
                                reason_codes=["tier2_capture_found"])

    # --- Tier 3: fall through to the full pipeline (fail-open). -------------
    return _fallthrough(3, intent="discover" if signal else None,
                        requirements=req if signal else None,
                        reason_codes=["tier3_fallthrough"])


def _cached_discovery_lookup(text: str, req: dict):
    """(discovery_result, similarity) for a fresh + similar cache hit, else None.
    Never raises."""
    del text
    try:
        from app import semantic_cache

        hit = semantic_cache.cached_lookup(
            req, min_similarity=CASCADE_TIER2_THRESHOLD, max_items=3
        )
        if not hit:
            return None
        return hit, float(hit.get("similarity") or 0.0)
    except Exception:  # noqa: BLE001
        return None


def _no_merchant_match(text: str, req: dict) -> bool:
    """Deterministic merchant-capture verdict — identical logic to the
    discover-node no-match path (including the explicit-item force-fit guard)."""
    del text
    try:
        from app import merchant_capture

        matches = merchant_capture.top_merchant_matches(_merchant_products(), req)
        explicit = req.get("explicit_item") or []
        if explicit:
            kept = []
            for cap in matches:
                card = cap.get("product") or {}
                hay = " ".join(
                    [str(card.get("name", "")), str(card.get("category", "") or ""),
                     str(card.get("description", "") or "")]
                ).lower()
                if any(n in hay for n in explicit):
                    kept.append(cap)
            matches = kept
        return not matches
    except Exception:  # noqa: BLE001 — doubt means fall through
        return False


def _no_match_reply(discovery_result: dict) -> str:
    from app import discovery

    return discovery.no_match_reply(discovery_result or {})