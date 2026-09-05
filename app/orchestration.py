"""
Orchestration (Revision 3) — thin forward-pass coordinator (LLD §21 / HLD §11).

Replaces the Rev-2 "LLM re-enters after every tool" loop. The pipeline is a
single deterministic forward pass through a small LangGraph StateGraph:

        START
          |
        route    (cascade router: tiered fast paths; money guards fail-open)
          |      fast path -> tool/respond (never approval/guardrail/pay)
          |
        agent    (router + requirements extraction; tasks the turn)
          |
        discover (merchant capture + cached/live external discovery + offer)
          |
        tool     (deterministic cart/checkout/quote actions, no LLM)
          |
        approval (deterministic approval check, fail-closed)
          |
        guardrail(quote <-> cart integrity + spend limits, fail-closed)
          |
        pay      (Razorpay ONLY if every precondition passed)
          |
        respond  (deterministic reply composition + compact state persist)
          |
        audit    (final per-turn audit row) -> END

`route` is a latency optimizer, never a money authority: a fast-path action is
restricted to SAFE_ACTIONS / SAFE_TOOL_KINDS and the fast-path edges skip the
approval/guardrail/pay nodes entirely, so `execute_payment` is structurally
unreachable from a fast path. Any payment/approval phrasing or response to a
*shown* preview falls through to the full pipeline.

Every node records its own AuditLog row (trace_id + stage + latency) via
`observability`. Money decisions never touch the LLM; the LLM's only optional
role in the whole pipeline is one structured requirement-extraction call
inside the router when deterministic confidence is too low.
"""

import re
from decimal import Decimal

from sqlalchemy import func

from langgraph.graph import END, START, StateGraph
from typing import TypedDict

from app import commerce
from app import cascade_router
from app import cross_sell
from app import db as db_module
from app import discovery
from app import guardrail as guardrail_module
from app import mandate as mandate_module
from app import merchant_capture
from app import observability
from app import quote as quote_module
from app import router as router_module
from app import session_state
from app.models import Order, Product
from app.observability import new_trace_id
from app.schemas import ChatRequest, ChatResponse


class AgentState(TypedDict):
    session_id: str
    actor: str
    user_text: str
    persisted: dict
    classification: dict | None
    structured_requirements: dict | None
    next_action: str
    discovery_result: dict | None
    captured: dict | None
    offer_result: dict | None
    cart_items: list
    cart_version: int
    amount: str | None
    quote: dict | None
    checkout_preview: dict | None
    approval_confirmed: bool
    policy_decision: str | None  # "ALLOW" | "BLOCK"
    policy_reason: str | None
    payment_result: dict | None
    block_reason: str | None
    reply: str
    trace_id: str
    stages: list  # audit events emitted during this turn
    tools_called: list
    spend_so_far: float
    llm_calls: int
    llm_intent: dict | None = None
    pending_suggested_sku: str | None = None
    candidates: list | None = None
    fast_path: bool = False
    tier_used: int | None = None
    route_intent: str | None = None
    discovery_pre_seed: dict | None = None
    first_turn: bool = False
    mandate_signature: str | None = None  # HMAC binding, minted on ALLOW (Phase 4)
    suggestions: list | None = None  # 2-3 selectable merchant options (suggestions UX)


_SKU_RE = re.compile(r"\b([A-Z]{2,6}-\d{2,6})\b", re.IGNORECASE)
_SPEND_STATUSES = ("created", "paid")


# ---------------------------------------------------------------------------
# Node: route — cascade router (Rev 3). Fast-path short-circuits forward.
# ---------------------------------------------------------------------------

def route_node(state):
    with observability.stage("route"):
        decision = cascade_router.route(
            state["user_text"],
            session_id=state["session_id"],
            is_first_turn=state.get("first_turn", False),
            has_preview=bool(state.get("checkout_preview")),
            cart_items=state.get("cart_items") or [],
            actor=state["actor"],
        )
    state["fast_path"] = decision.fast_path
    state["tier_used"] = decision.tier
    state["route_intent"] = decision.intent
    if decision.fast_path:
        state["candidates"] = None
        if decision.direct_reply is not None:
            state["next_action"] = "idle"
            state["reply"] = decision.direct_reply
        else:
            state["next_action"] = decision.action or "idle"
        if decision.pre_seed:
            state["discovery_result"] = decision.pre_seed
            # Tier-2 short-circuit surfaces selectable market (EXT-xxx) options
            # identical to the full discover_node branch so numbered picks work.
            state["suggestions"] = _reference_suggestions(state)
            sel = state["suggestions"]
            state["pending_suggested_sku"] = sel[0]["sku"] if sel else None
    else:
        # Fall-through: agent_node re-tasks the turn exactly as Rev-2 did. A
        # pre-seeded requirement set only exists when the deterministic signal
        # was already complete, so agent_node reproduces the same requirements.
        state["next_action"] = "idle"
        if decision.requirements:
            state["structured_requirements"] = decision.requirements

    state["classification"] = {
        "intent": decision.intent,
        "source": "cascade",
        "confidence": decision.confidence,
    }

    # Every routing decision is audited (fast path AND fall-through): the
    # money-safety invariant is that a fast-path action is never a money action
    # (route() only emits SAFE_ACTIONS / SAFE_TOOL_KINDS, and the fast-path
    # edges below never cross approval/guardrail/pay).
    observability.write_audit(
        session_id=state["session_id"],
        actor=state["actor"],
        event_type="cascade_route",
        tool_name="route",
        parameters={
            "intent": decision.intent,
            "tier": decision.tier,
            "fast_path": decision.fast_path,
            "action": str(decision.action),
            "reason_codes": decision.reason_codes,
        },
        decision="FAST" if decision.fast_path else "FALLTHROUGH",
        outcome="success",
        trace_id=state["trace_id"],
        stage="route",
        source="cascade",
        confidence=decision.confidence,
        tier_used=decision.tier,
    )
    return state


# ---------------------------------------------------------------------------
# Node: agent — router, then task the turn.
# ---------------------------------------------------------------------------

def agent_node(state):
    with observability.stage("router"):
        classification = router_module.classify(state["user_text"], {
            "cart_non_empty": bool(state.get("cart_items")),
            "has_preview": bool(state.get("checkout_preview")),
        })
    state["classification"] = classification

    # Deterministic tasking — no LLM for simple operations (routing_rules).
    decision = _task_turn(state)
    state["next_action"] = decision.action

    if decision.status == "gather":
        state["reply"] = _clarify_prompt(state)
        return state

    if decision.status == "ready" and decision.prompt:
        state["reply"] = decision.prompt
        return state

    return state


def _task_turn(state):
    """A small dataclass-free decision: returns .status in
    {'gather','ready'} and, when ready, an action (or a pre-built reply)."""
    from types import SimpleNamespace

    text = state["user_text"].strip().lower()
    classification = state["classification"] or {}

    # 1) Approval: explicit "yes" AFTER a preview has been shown this session.
    if state.get("checkout_preview") and guardrail_module.is_explicit_approval(text):
        return SimpleNamespace(status="ready", prompt=None, action="approve_checkout")

    # 2) Cart ops with a resolvable merchant SKU (fast path, no LLM).
    sku = _find_sku(state, text)
    if sku:
        if _has(text, ("remove", "delete", "drop", "clear")):
            return SimpleNamespace(status="ready", prompt=None, action=("remove", sku))
        if _has(text, ("show", "view", "cart")) and not _has(text, ("add", "include", "put", "buy")):
            return SimpleNamespace(status="ready", prompt=None, action="cart_show")
        return SimpleNamespace(status="ready", prompt=None, action=("add", sku))

    # 2b) Catalog item named in natural language ("put the mug in my cart").
    #     An exact product name is added directly (step 2); a catalog keyword
    #     that maps to ONE merchant SKU must be confirmed before entering the
    #     cart — never auto-added from ambiguous phrasing.
    catalog = _catalog_keywords(text)
    if catalog and _has(text, ("add", "put", "grab", "get me", "get", "buy", "want", "need", "wish", "include", "throw in", "cart")):
        skus = _catalog_skus_for(catalog)
        if len(skus) == 1:
            return SimpleNamespace(status="ready", prompt=None, action=("propose_add", skus.pop()))
        if len(skus) > 1:
            names = " or ".join(sorted({p["name"] for p in _merchant_products() if p["sku"] in skus}))
            return SimpleNamespace(
                status="ready",
                prompt=f"I see a few options for that: {names}. Which one should I add to your cart?",
                action=None,
            )

    # 2c) Confirming a previously-suggested merchant item ("yes", "add it").
    #     Fires only when a discovery/suggestion turn left a pending SKU behind.
    pending = state.get("pending_suggested_sku")
    if pending and (_has(text, ("yes", "yeah", "yep", "sure", "ok", "okay", "go ahead", "add it", "add this", "put it", "that works", "ld like")) or text in _ADD_AFFIRMS):
        return SimpleNamespace(status="ready", prompt=None, action=("add", pending))

    # 2d) Selecting from the 2-3 suggestions (or the ranked shortlist):
    #     "2", "third one", "option 3" -> add the chosen SKU.
    pick = _selection_pick(text, state.get("suggestions") or state.get("candidates") or [])
    if pick is not None:
        return SimpleNamespace(status="ready", prompt=None, action=("add", pick))

    # 3) Junk SKU-shaped tokens (e.g. asking to add an unknown merchant item).
    if _has(text, ("add", "include", "put", "pick up", "throw in")):
        return SimpleNamespace(
            status="ready",
            prompt=(
                "That SKU isn't in our catalog. Tell me a product to search and I'll "
                "pull live market options (EXT-xxx) you can add to your cart, or use "
                'one of the numbered suggestions above. Then say "checkout" when ready.'
            ),
            action=None,
        )

    # 4) Cart / checkout affordances.
    if _has(text, ("cart", "basket", "bag")) and _has(text, ("show", "view", "what", "open")):
        return SimpleNamespace(status="ready", prompt=None, action="cart_show")
    # Product-price questions ("how much is running shoes?") are discovery, NOT
    # the cart total — so cart_total only fires when the query is cart-focused
    # (cart/bag words + a money word, or a money phrase with no product terms).
    productish = bool(router_module.extract_category(text) or _catalog_keywords(text))
    if (
        _has(text, ("cart", "basket", "bag"))
        and _has(text, ("total", "subtotal", "balance", "how much", "amount", "value"))
    ) or (
        _has(text, ("my total", "cart total", "total bill", "subtotal", "balance", "amount due", "how much"))
        and not productish
    ):
        return SimpleNamespace(status="ready", prompt=None, action="cart_total")
    if _has(text, ("checkout", "check out", "bill", "ready to pay", "proceed to payment")):
        return SimpleNamespace(status="ready", prompt=None, action="checkout")
    if _has(text, ("cart", "basket", "bag")):
        return SimpleNamespace(status="ready", prompt=None, action="cart_show")

    # 5) Payment intents map to checkout (which needs a non-empty merchant cart).
    if _has(text, ("pay", "pay now", "approve", "confirm", "proceed")):
        return SimpleNamespace(status="ready", prompt=None, action="checkout")

    # 6) Discovery / requirements gathering. "buy"/"purchase" without a resolvable
    #    SKU is treated as open-ended discovery, never a made-up cart entry.
    requirements, complete = _structured_requirements(state, classification)
    state["structured_requirements"] = requirements
    if not complete:
        # 6b) Gemini intent rescue. The deterministic rules could not parse the
        #     phrasing at all ("could you kindly take everything off my list").
        #     One tolerated structured call decides whether the user meant a
        #     cart op or a still-product request; on any failure we simply ask
        #     the clarifying question. Never decides money.
        rescued = _apply_llm_intent(state)
        if rescued is not None:
            return rescued
        return SimpleNamespace(status="ready", prompt=None, action="gather")
    return SimpleNamespace(status="ready", prompt=None, action="discover")


def _has(text, needles):
    t = " " + text + " "
    return any(n in t for n in needles)


def _find_sku(state, text):
    m = _SKU_RE.search(text)
    if m:
        sku = m.group(1).upper()
        if _merchant_has(sku):
            return sku
        # Reference (SERP) SKUs from the live suggestion list — EXT-xxx — are
        # addable too.
        if _reference_item(state, sku) is not None:
            return sku
    for p in _merchant_products():
        if p["name"].lower() in text:
            return p["sku"]
    return None


def _merchant_has(sku):
    return commerce.get_product_by_sku(sku) is not None


def _reference_item(state, sku):
    """The suggestion dict (SERP/reference EXT-xxx or merchant) matching a SKU,
    else None. Suggestions carry the name/price/source needed to add to cart."""
    sku = str(sku or "").upper()
    for s in state.get("suggestions") or []:
        if isinstance(s, dict) and s.get("sku") == sku:
            return s
    return None


_STOPWORDS = {
    "i", "a", "an", "the", "and", "or", "for", "to", "of", "on", "in",
    "with", "under", "over", "up", "some", "any", "new", "nice", "good",
    "best", "wear", "buy", "want", "need", "me", "my", "your", "is", "it",
    "that", "this", "at", "from", "about", "around",
}


def _catalog_keywords(text: str) -> list[str]:
    """Discriminating words the user said that appear in the merchant catalog
    names (e.g. "mug", "wallet", "earbuds"). Deterministic, no LLM. Lets
    merchant capture tell Ceramic Coffee Mug from Desk Lamp LED when both are
    in the same `home` category."""
    if not text:
        return []
    tokens = {t for t in re.split(r"\W+", text.strip().lower()) if t}
    vocab: set[str] = set()
    for p in _merchant_products():
        for t in re.split(r"\W+", (p["name"] or "").lower()):
            if t:
                vocab.add(t)
    hits = sorted(t for t in tokens if t in vocab and t not in _STOPWORDS and not t.isdigit())
    return hits


def _explicit_item_terms(text: str) -> list[str]:
    """Non-generic nouns the user explicitly named that are NOT covered by a
    merchant category and NOT present in the merchant catalog vocabulary
    (e.g. "mouse", "grinder", "gaming chair"). When present, capture must not
    force-fit a same-category product that lacks the named item — we degrade to
    external discovery instead. Deterministic, no LLM."""
    if not text or router_module.extract_category(text):
        return []
    tokens = {t for t in re.split(r"\W+", text.strip().lower()) if t and len(t) > 2}
    vocab: set[str] = set()
    for p in _merchant_products():
        for t in re.split(r"\W+", ((p["name"] or "") + " " + (p["category"] or "")).lower()):
            if t:
                vocab.add(t)
    return sorted(
        t for t in tokens
        if t not in _STOPWORDS and t not in vocab and not t.isdigit()
    )


def _catalog_skus_for(catalog_terms: list[str]) -> set[str]:
    """Map catalog-vocabulary words back to the merchant SKUs whose name /
    category / description carry them ("mug" -> {HOME-001}). Deterministic."""
    terms = set(catalog_terms or [])
    if not terms:
        return set()
    skus: set[str] = set()
    for p in _merchant_products():
        hay = " ".join(
            [str(p.get("name") or ""), str(p.get("category") or ""), str(p.get("description") or "")]
        ).lower()
        if any(t in hay for t in terms):
            skus.add(p["sku"])
    return skus


_ADD_AFFIRMS = {"yes", "sure", "ok", "okay", "yep", "yeah", "go ahead", "go on", "add"}

_SELECTION_WORDS = {
    "one": 1, "first": 1, "1st": 1,
    "two": 2, "second": 2, "2nd": 2,
    "three": 3, "third": 3, "3rd": 3,
}


def _selection_skus(items) -> list[str]:
    """SKUs of a selectable list. Accepts suggestion dicts (sku at top level)
    and merchant-capture candidates (sku under 'product')."""
    skus = []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        product = c.get("product")
        sku = product.get("sku") if isinstance(product, dict) else c.get("sku")
        if sku:
            skus.append(sku)
    return skus


def _selection_pick(text: str, selectables) -> str | None:
    """Map a ranked-shortlist pick ("2", "third one", "option 1", "first") to
    the chosen merchant SKU. Works on suggestion dicts (sku at top level) or
    merchant-capture candidates (sku under 'product'). Deterministic; None when
    nothing matched."""
    skus = _selection_skus(selectables)
    if not skus:
        return None
    t = f" {text} ".lower()
    # Direct ordinal / cardinal picks.
    for word, idx in _SELECTION_WORDS.items():
        if idx > len(skus):
            continue
        if f" {word} " in t or text.strip() == word:
            return skus[idx - 1]
    # Bare digits 1..3, optionally preceded by pick/option/number words.
    m = re.search(r"\b(?:pick|choose|select|option|number|no\.?|want|take|add)?\s*([1-3])\b", text)
    if m and int(m.group(1)) <= len(skus):
        return skus[int(m.group(1)) - 1]
    return None


def _apply_llm_intent(state) -> "SimpleNamespace | None":
    """Gemini intent rescue — runs ONLY when the deterministic router could not
    parse the message (step 6b). Maps the (tolerated, discardable) result to a
    cart action or re-seeds discovery requirements. Never touches money."""
    from types import SimpleNamespace

    text = (state.get("user_text") or "").strip()
    tokens = [t for t in re.split(r"\W+", text) if t]
    if len(tokens) < 3:
        return None  # too terse to bother Gemini with; clarify instead
    with observability.stage("llm"):
        llm_intent = router_module.extract_intent_llm(text)
    if not llm_intent:
        return None
    state["llm_intent"] = llm_intent
    state["llm_calls"] = state.get("llm_calls", 0) + 1
    intent = llm_intent.get("intent") or "other"

    if intent in ("add_to_cart", "search"):
        product = llm_intent.get("product") or llm_intent.get("category")
        if not product:
            return None
        requirements, _complete = _structured_requirements_from(
            state["user_text"], llm_intent, state.get("classification") or {})
        state["structured_requirements"] = requirements
        return SimpleNamespace(status="ready", prompt=None, action="discover")
    if intent == "show_cart":
        return SimpleNamespace(status="ready", prompt=None, action="cart_show")
    if intent == "show_total":
        return SimpleNamespace(status="ready", prompt=None, action="cart_total")
    if intent == "checkout":
        return SimpleNamespace(status="ready", prompt=None, action="checkout")
    if intent == "clear_cart":
        return SimpleNamespace(status="ready", prompt=None, action="clear_cart")
    if intent == "remove":
        product = llm_intent.get("product") or ""
        skus = _catalog_skus_for(_catalog_keywords(product))
        if len(skus) == 1:
            return SimpleNamespace(status="ready", prompt=None, action=("remove", skus.pop()))
    return None


def _structured_requirements_from(user_text, llm_intent, classification):
    """Seed discovery requirements from the LLM intent result, canonicalizing
    the category through the deterministic synonym map. Deterministic extractor
    still wins for budget/features when present in the original message."""
    category = llm_intent.get("category")
    product = llm_intent.get("product") or category
    canonical = router_module.extract_category(str(category)) if category else None
    product_type = canonical or product
    features = list(classification.get("features") or [])
    catalog = _catalog_keywords(user_text)
    explicit = _explicit_item_terms(user_text)
    req = {
        "category": canonical,
        "product_type": product_type or None,
        "budget": classification.get("budget"),
        "keywords": features + catalog + explicit + ([str(product)] if product else []),
        "required_features": features + catalog,
        "brand": classification.get("brand"),
        "explicit_item": explicit,
    }
    req["budget"] = _try_budget(req)
    return req, bool(canonical or product_type or features or catalog)


def _merchant_products():
    db = db_module.SessionLocal()
    try:
        rows = db.query(Product).all()
        return [
            {
                "sku": r.sku, "name": r.name, "price": r.price,
                "stock": r.stock, "category": r.category,
                "description": r.description or "",
                "currency": r.currency, "merchant_priority": r.merchant_priority,
                "unit_cost": r.unit_cost, "margin_pct": r.margin_pct,
                "semantic_text": r.semantic_text,
            }
            for r in rows
        ]
    finally:
        db.close()


def _structured_requirements(state, classification):
    """Deterministic extraction first; router escalates to one structured LLM
    call only when its confidence gates ask for it. Failures degrade to {}."""
    features = classification.get("features") or []
    catalog = _catalog_keywords(state.get("user_text") or "")
    explicit = _explicit_item_terms(state.get("user_text") or "")
    req = {
        "category": classification.get("category"),
        "product_type": classification.get("product_type"),
        "budget": classification.get("budget"),
        # Soft terms: drive the external search query + ranking, NOT hard filters.
        "keywords": features + catalog + explicit,
        # Hard terms: real feature requirements (wireless, running, led...) and
        # catalog vocabulary only — context words like "kitchen"/"gaming" must
        # not over-filter external listings to zero.
        "required_features": features + catalog,
        "brand": classification.get("brand"),
        "explicit_item": explicit,
    }
    budget = _try_budget(req)
    req["budget"] = budget
    # Completeness is judged ONLY on real signals (category / features / catalog
    # vocabulary). Explicit-item nouns are contextual ("vintage", "collect")
    # and must not pre-empt the LLM escape hatch for genuinely vague queries.
    complete = bool(
        req.get("category")
        or req.get("product_type")
        or (features or catalog)
    )
    if complete:
        state["llm_calls"] = state.get("llm_calls", 0)
        return req, True
    # Low confidence: one structured LLM call, tolerated failure.
    with observability.stage("llm"):
        llm_req = router_module.extract_requirements_llm(state["user_text"])
    if not llm_req:
        return req, False
    state["llm_calls"] = state.get("llm_calls", 0) + 1
    # Canonicalize free-text LLM categories through the deterministic synonym
    # map so "clothing" -> apparel, "earbuds" -> electronics, etc.
    llm_category = llm_req.get("category")
    canonical = router_module.extract_category(str(llm_category)) if llm_category else None
    if canonical:
        req["category"] = canonical
    if canonical is None and llm_category:
        req["product_type"] = llm_category
    if llm_req.get("budget_max") is not None:
        req["budget"] = _try_budget({"budget": llm_req["budget_max"]})
    if llm_req.get("features"):
        # LLM free-text descriptors ("vintage", "nice") are soft keywords only —
        # hard `required_features` stay deterministic (wireless, running, ...)
        # so external listings are never over-filtered to zero.
        req["keywords"] = list(req.get("keywords") or []) + list(llm_req["features"])
    if llm_req.get("brand"):
        req["brand"] = llm_req["brand"]
    req["budget"] = _try_budget(req)
    return req, True


def _try_budget(req):
    budget = req.get("budget")
    if budget is None:
        return None
    try:
        return float(budget)
    except (TypeError, ValueError):
        return None


def _clarify_prompt(state) -> str:
    missing = []
    if not state["structured_requirements"]:
        missing.append("what kind of product you're looking for")
    if "budget" not in (state.get("structured_requirements") or {}) or state["structured_requirements"].get("budget") is None:
        missing.append("an approximate budget")
    if missing:
        return "Before I look, could you tell me " + " and ".join(missing) + "? For example: 'running shoes under ₹2500'."
    return "Could you give me a little more detail (brand, key features, or budget) so I can narrow the search?"


# ---------------------------------------------------------------------------
# Node: discover — merchant capture, optional external, then a single offer.
# ---------------------------------------------------------------------------

def discover_node(state):
    # "gather" just means we posed a clarifying question — no discovery work.
    if state["next_action"] != "discover":
        return state
    products = _merchant_products()
    req = state["structured_requirements"] or {}
    with observability.stage("merchant_retrieval"):
        # Ranked shortlist (top N) gives the shopper options to select from.
        candidates = merchant_capture.top_merchant_matches(products, req)
        # The user explicitly named an item the merchant catalog does not
        # stock (e.g. "wireless mouse", "coffee grinder"). A same-category
        # neighbor (earbuds, mug) is NOT the requested product — drop it and
        # degrade to external discovery rather than force-fitting a misleading
        # match.
        explicit = req.get("explicit_item") or []
        if explicit:
            kept = []
            for cap in candidates:
                card = cap.get("product") or {}
                hay = " ".join(
                    [
                        str(card.get("name", "")),
                        str(card.get("category", "") or ""),
                        str(card.get("description", "") or ""),
                    ]
                ).lower()
                if any(n in hay for n in explicit):
                    kept.append(cap)
            candidates = kept
        capture = candidates[0] if candidates else {
            "verdict": "no_merchant_match",
            "reason": "merchant catalog does not stock the named item" if explicit else
                      "no merchant catalog product satisfies these requirements",
            "capped": 0.0,
        }

    state["candidates"] = candidates
    state["captured"] = capture
    if capture.get("verdict") == "no_merchant_match":
        with observability.stage("discovery"):
            state["discovery_result"] = discovery.discover(req, state["session_id"])
        # No merchant direct hit. Build a SELECTABLE reference (SERP/EXT-xxx)
        # suggestion list so every market listing is addable and flows through
        # cart -> checkout -> payment like any merchant item (Rev 3).
        state["suggestions"] = _reference_suggestions(state)
        sel = state["suggestions"]
        state["pending_suggested_sku"] = sel[0]["sku"] if sel else None
        state["reply"] = _no_match_reply(state.get("discovery_result") or {})
        return state

    with observability.stage("offer_engine"):
        state["offer_result"] = _offers(state, capture)

    # The 2-3 selectable suggestion list: captured product + payable catalog
    # complements + next-best captures, in stock, in-budget, not already carted.
    suggestions = _suggestion_list(state, capture, candidates)
    state["suggestions"] = suggestions
    # Park the top suggestion so "add it / yes" approves it; numbered picks
    # ("2", "third one") resolve against `suggestions` in the task turn.
    state["pending_suggested_sku"] = suggestions[0]["sku"] if suggestions else None
    if suggestions:
        state["reply"] = _suggestions_reply(state, suggestions)
    else:
        state["reply"] = _no_match_reply(state.get("discovery_result") or {})
    return state


def _suggestion_list(state, capture, candidates) -> list[dict]:
    """Deterministic 2-3 selectable merchant options for a merchant query.

    Order is by merchant fit: the captured product first, then its payable
    catalog complements ("pairs well"), then the next-best captures. Anything
    out of stock, over budget, or already in the cart is dropped. Caps at 3 so
    the shopper has a real choice without option fatigue.
    """
    req = state["structured_requirements"] or {}
    budget = req.get("budget")
    cart_skus = {i["sku"] for i in commerce.list_cart_items(state["session_id"])}
    out: list[dict] = []
    seen: set[str] = set()

    def push(item: dict, fit: float | None, why: str, allow_carted: bool = False) -> None:
        sku = str(item.get("sku") or "").upper()
        if sku in seen or not sku:
            return
        try:
            price = float(item.get("price") or 0.0)
            stock = int(item.get("stock") or 0)
        except (TypeError, ValueError):
            return
        if stock <= 0 or price <= 0:
            return
        if budget and price > budget:
            return
        if not allow_carted and sku in cart_skus:
            return
        seen.add(sku)
        out.append(
            {
                "sku": sku,
                "name": item.get("name"),
                "price": price,
                "currency": item.get("currency", "INR"),
                "stock": stock,
                "fit": round(float(fit), 2) if fit is not None else None,
                "why": why,
            }
        )

    verdict = capture.get("verdict")
    captured = capture.get("product") or {}
    if verdict in ("capture", "recommend_offers") and captured:
        fit = float(capture.get("capped") or 0.0)
        # The captured product is ALWAYS option #1 — even when it is already in
        # the cart (selecting it again simply bumps the quantity).
        push(captured, fit, "{}% fit".format(round(fit * 100)), allow_carted=True)
        for comp in cross_sell.catalog_complements(captured.get("sku", "")):
            comp = {**comp, "sku": str(comp.get("sku") or "").upper()}
            push(comp, None, comp.get("why") or "pairs well with your selected item")
    for cap in candidates:
        card = cap.get("product") or {}
        if not card:
            continue
        fit = float(cap.get("capped") or cap.get("score") or 0.0)
        push(card, fit, "{}% fit".format(round(fit * 100)))
        if len(out) >= 3:
            break
    return out[:3]


def _suggestions_reply(state, suggestions) -> str:
    """Numbered 2-3 option list the shopper can pick from (or add by SKU)."""
    lines = ["Here are the best options (merchant match), ranked by fit:"]
    for i, s in enumerate(suggestions, 1):
        suffix = s.get("why") or "in stock"
        lines.append(
            f"  {i}) {s['name']} ({s['sku']}) — ₹{s['price']} — {suffix}"
        )
    lines.append('Reply "1", "2" or "3" (or say "add <SKU>") to add a pick to your cart, then say "checkout" when ready.')
    return "\n".join(lines)


def _cross_sell_reply(state, sku: str, session_id: str) -> str:
    """Complementary suggestions appended after an item is added to the cart.
    Market complements (from SERP discovery) are addable — an EXT-xxx SKU is
    minted with the listing's name/price/source and parked in the live
    suggestion list so "add EXT-xxx" resolves. Deterministic; never blocks."""
    if not sku:
        return ""
    try:
        with observability.stage("cross_sell"):
            comps = cross_sell.complements_for(sku, session_id)
    except Exception:  # noqa: BLE001 — cross-sell is optional sugar
        return ""
    lines: list[str] = []
    cat = comps.get("catalog") or []
    ext = comps.get("external") or []
    if cat:
        c = cat[0]
        lines.append(
            f" Also pairs well: {c['name']} ({c['sku']}) at ₹{c['price']} — say \"add {c['sku']}\""
        )
    if ext:
        e = ext[0]
        ext_sku = f"EXT-{len(state.get('suggestions') or []) + 1:03d}"
        state["suggestions"] = (state.get("suggestions") or []) + [
            {
                "sku": ext_sku,
                "name": e["name"],
                "price": float(e.get("price") or 0.0),
                "currency": e.get("currency", "INR"),
                "source": e.get("source", ""),
                "item_type": "reference",
                "type": "reference",
            }
        ]
        lines.append(
            f" Market complement: {e['name']} ({ext_sku}) — ₹{e['price']} "
            f"({e['currency']}) — say \"add {ext_sku}\""
        )
    if lines:
        return "\n" + "\n".join(lines)
    return ""


def _offers(state, capture):
    from app import offer_engine
    from app.growth import record_offer_event

    product = capture.get("product") or {}
    req = state["structured_requirements"] or {}
    selected = offer_engine.select_offers(
        {**product, "price": product.get("price", 0.0)},
        state["session_id"],
        _merchant_products(),
    )
    for offer in selected.get("offers", []):
        record_offer_event(
            session_id=state["session_id"],
            base_sku=product.get("sku", ""),
            offer_sku=offer["sku"],
            shown=True,
            accepted=False,
            p_accept=offer.get("p_accept"),
            eir=offer.get("expected_incremental_revenue"),
        )
    state["llm_calls"] = state.get("llm_calls", 0)
    return selected


def _no_match_reply(discovery_result) -> str:
    """Deterministic no-merchant-match copy — shared with the cascade Tier-2
    short-circuit so both paths answer byte-identically."""
    return discovery.no_match_reply(discovery_result or {})


def _reference_suggestions(state) -> list[dict]:
    """Build the selectable suggestion list for the no-merchant-match branch.

    Each market/SERP listing from discovery becomes an EXT-xxx suggestion that
    carries the name/price/source the cart add needs. These ARE payable (Rev 3:
    external matches are addable and flow through checkout like any merchant
    item), so they resolve through `_find_sku` / numbered picks just like the
    merchant suggestions. Stock is open (no catalog ceiling).
    """
    disc = state.get("discovery_result") or {}
    recs = disc.get("recommendations") or []
    suggestions = []
    for i, r in enumerate(recs[:3], 1):
        try:
            price = float(r.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        name = r.get("name") or r.get("title")
        if not name:
            continue
        suggestions.append(
            {
                "sku": f"EXT-{i:03d}",
                "name": str(name),
                "price": price,
                "currency": r.get("currency") or "INR",
                "source": r.get("source") or "market",
                "item_type": "reference",
                "type": "reference",
            }
        )
    return suggestions


# ---------------------------------------------------------------------------
# Node: tool — deterministic cart / checkout / quote actions (no LLM).
# ---------------------------------------------------------------------------

def tool_node(state):
    action = state["next_action"]
    # gather/discover turns already produced their reply in agent/discover nodes.
    if action is None or action == "idle" or action in ("gather", "discover"):
        return state
    if isinstance(action, tuple):
        kind, sku = action
        if kind == "add":
            ref = _reference_item(state, sku)
            if ref is not None and not _merchant_has(sku):
                # SERP/reference listing (EXT-xxx) — addable with listing details.
                result = commerce.add_reference_to_cart(
                    state["session_id"],
                    state["actor"],
                    ref["sku"],
                    ref.get("name") or ref.get("title") or sku,
                    ref.get("price"),
                    source=ref.get("source"),
                )
            else:
                result = commerce.add_to_cart(state["session_id"], state["actor"], sku)
            state["pending_suggested_sku"] = None
            state["candidates"] = None
            state["tools_called"] = state.get("tools_called", []) + [f"add_{sku}"]
            if "error" in result:
                state["reply"] = result["error"]
            else:
                state["reply"] = f"Added {sku} to your cart. Current total: ₹{result['total']}. Say 'checkout' when ready."
                state["reply"] += _cross_sell_reply(state, sku, state["session_id"])
        elif kind == "remove":
            result = commerce.remove_from_cart(state["session_id"], state["actor"], sku)
            state["pending_suggested_sku"] = None
            state["tools_called"] = state.get("tools_called", []) + [f"remove_{sku}"]
            if "error" in result:
                state["reply"] = result["error"]
            else:
                state["reply"] = f"Removed {sku} from your cart. Current total: ₹{result['total']}."
        elif kind == "propose_add":
            product = commerce.get_product_by_sku(sku)
            if not product:
                state["reply"] = f"Sorry, {sku} is not in our catalog."
            else:
                state["pending_suggested_sku"] = sku
                state["reply"] = (
                    f"I can add {product.name} ({product.sku}) at ₹{product.price} to "
                    "your cart. Should I go ahead?"
                )
        state["cart_items"] = commerce.list_cart_items(state["session_id"])
        state["cart_version"] = _cart_version(state)
        return state

    if action == "clear_cart":
        items = commerce.list_cart_items(state["session_id"])
        removed: list[str] = []
        for item in items:
            res = commerce.remove_from_cart(state["session_id"], state["actor"], item["sku"])
            if "error" not in res:
                removed.append(item["sku"])
        state["pending_suggested_sku"] = None
        state["tools_called"] = state.get("tools_called", []) + [f"remove_{s}" for s in removed]
        state["cart_items"] = commerce.list_cart_items(state["session_id"])
        state["cart_version"] = _cart_version(state)
        state["reply"] = "Emptied your cart." if removed else "Your cart was already empty."
        return state

    if action in ("cart_show", "cart_total"):
        summary = commerce.cart_summary(state["session_id"])
        state["cart_items"] = summary["items"]
        state["cart_version"] = _cart_version(state)
        if action == "cart_total":
            if not summary["items"]:
                state["reply"] = (
                    "Your cart is empty, so the total is ₹0.00. Tell me a SKU (e.g. APP-001) "
                    "or search a product to add."
                )
            else:
                state["reply"] = _total_reply(summary)
        else:
            state["reply"] = _cart_reply(summary)
        return state

    if action == "checkout":
        summary = commerce.cart_summary(state["session_id"])
        if not summary["items"]:
            state["reply"] = "Your cart is empty. Search for a product or tell me a SKU to add first."
            return state
        state["cart_items"] = summary["items"]
        state["cart_version"] = _cart_version(state)
        with observability.stage("quote"):
            quote = quote_module.create_quote(
                state["session_id"], state["actor"], summary["items"], state["cart_version"]
            )
        state["quote"] = quote
        state["checkout_preview"] = {
            "quote_id": quote["quote_id"],
            "nonce": quote["nonce"],
            "items": summary["items"],
            "subtotal": summary["subtotal"],
            "total": summary["total"],
            "currency": summary["currency"],
            "cart_hash": quote["cart_hash"],
            "cart_version": quote["cart_version"],
            "expires_at": quote["expires_at"],
        }
        state["reply"] = _preview_reply(state["checkout_preview"])
        return state

    # Unknown action — default to show cart.
    summary = commerce.cart_summary(state["session_id"])
    state["reply"] = _cart_reply(summary)
    return state


def _cart_version(state) -> int:
    return quote_module.load_session_version(state["session_id"], state["actor"])


def _cart_reply(summary) -> str:
    if not summary["items"]:
        return "Your cart is empty. Tell me a SKU (e.g. APP-001) or search for a product."
    lines = ["Your cart:"]
    for i in summary["items"]:
        lines.append(f"  - {i['name']} × {i['quantity']} — ₹{i['price']} each")
    lines.append(f"Total: ₹{summary['total']} (plus any checkout fee)")
    lines.append("Reply 'checkout' to see the payable preview.")
    return "\n".join(lines)


def _total_reply(summary) -> str:
    return f"Your current cart total is ₹{summary['total']} ({summary['count']} item(s))."


def _preview_reply(preview) -> str:
    lines = ["Checkout preview:"]
    for i in preview["items"]:
        lines.append(f"  - {i['name']} ({i['sku']}) × {i['quantity']} — ₹{i['price']} each")
    lines.append(f"Subtotal: ₹{preview['subtotal']}  |  Total: ₹{preview['total']} {preview['currency']}")
    lines.append(f"Quote {preview['quote_id']} is valid until {preview['expires_at']}.")
    lines.append("Reply \"Yes, proceed\" to approve this exact amount, or 'no' to adjust the cart.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node: approval — deterministic, fail-closed (LLD §6.1).
# ---------------------------------------------------------------------------

def approval_node(state):
    if state["next_action"] != "approve_checkout":
        state["approval_confirmed"] = False
        return state
    with observability.stage("approval"):
        state["approval_confirmed"] = guardrail_module.validate_approval(
            {
                "checkout_preview": state.get("checkout_preview"),
                "quote": state.get("quote"),
                "messages": [{"role": "user", "content": state["user_text"]}],
            }
        )
    return state


# ---------------------------------------------------------------------------
# Node: guardrail — quote<->cart integrity + actor spend limits (LLD §6.2/§16).
# ---------------------------------------------------------------------------

def guardrail_node(state):
    state["mandate_signature"] = None  # only the ALLOW path mints one
    if state["next_action"] != "approve_checkout":
        state["policy_decision"] = None
        return state
    if not state.get("approval_confirmed"):
        state["policy_decision"] = "BLOCK"
        state["policy_reason"] = "checkout was not explicitly approved"
        return state

    with observability.stage("guardrail"):
        quote = state.get("quote")
        cart_items = commerce.list_cart_items(state["session_id"])
        version = _cart_version(state)
        ok, reason = guardrail_module.validate_quote_against_cart(quote, cart_items, version)
        if not ok:
            state["policy_decision"] = "BLOCK"
            state["policy_reason"] = f"quote integrity: {reason}"
            # A stale quote is superseded — anyone who approves again must get a
            # fresh preview first.
            quote_module.invalidate_quote(state["session_id"])
            return state

        amount = Decimal(quote["amount"])
        decision = guardrail_module.check_transaction(
            state["actor"], amount, state.get("spend_so_far", 0.0)
        )
        state["policy_decision"] = "ALLOW" if decision.allowed else "BLOCK"
        state["policy_reason"] = decision.reason
        state["amount"] = str(amount)
        if decision.allowed:
            # HMAC the exact approved snapshot (Phase 4): session + actor +
            # cart_hash + amount + quote_id. A changed cart or amount after this
            # point invalidates the signature — tamper evidence for the audit.
            state["mandate_signature"] = mandate_module.sign_mandate(
                session_id=state["session_id"],
                actor=state["actor"],
                cart_hash=quote["cart_hash"],
                amount=amount,
                quote_id=quote.get("quote_id"),
            )
    return state


# ---------------------------------------------------------------------------
# Node: pay — Razorpay ONLY when policy said ALLOW (money_action_contract).
# ---------------------------------------------------------------------------

def pay_node(state):
    if state["next_action"] != "approve_checkout":
        return state
    if state.get("policy_decision") != "ALLOW":
        state["block_reason"] = state.get("policy_reason") or "payment blocked"
        state["payment_result"] = {"blocked": True, "reason": state["block_reason"]}
        return state

    try:
        with observability.stage("razorpay"):
            result = commerce.execute_payment(
                state["session_id"], state["actor"], state["quote"]["cart_hash"],
                quote_id=state["quote"].get("quote_id"),
                mandate_signature=state.get("mandate_signature"),
            )
    except Exception as e:  # noqa: BLE001 — Razorpay failure is a normal reply
        state["payment_result"] = {
            "error": "payment could not be initiated — no charge was made",
            "detail": str(e),
        }
        return state

    if "error" in result:
        state["payment_result"] = {"error": result["error"]}
        return state

    order_total = result.get("amount")
    state["payment_result"] = {
        "order_id": result["order_id"],
        "payment_link": result.get("short_url"),
        "amount": order_total,
        "currency": "INR",
        "mandate_signature": (result.get("mandate_signature")
                              or state.get("mandate_signature")),
    }
    # Consume the quote so it cannot be reused for a second charge snapshot.
    quote_module.invalidate_quote(state["session_id"])
    return state


# ---------------------------------------------------------------------------
# Node: respond — deterministic reply + compact durable state persist.
# ---------------------------------------------------------------------------

def respond_node(state):
    if state.get("next_action") == "approve_checkout":
        pr = state.get("payment_result") or {}
        if state.get("policy_decision") != "ALLOW":
            state["reply"] = (
                f"Your checkout was blocked before any charge: {state.get('block_reason')}. "
                "No payment was processed. You can adjust your cart and ask for a fresh preview."
            )
        elif "error" in pr:
            state["reply"] = (
                f"Payment could not be initiated ({pr['error']}). No charge was made — "
                "you can try again once the payment service recovers."
            )
        else:
            state["reply"] = (
                f"Payment is ready. Order #{pr.get('order_id')} for ₹{pr.get('amount')}."
            )
            if pr.get("payment_link"):
                state["reply"] += f" Complete it here: {pr['payment_link']}"
    elif not state.get("reply"):
        state["reply"] = "How can I help? I can search our catalog, add items to your cart, and set up checkout for merchant SKUs."

    # Compact durable state (LLD §4 / HLD §3.5) — not raw history.
    save = {
        "structured_requirements": state.get("structured_requirements"),
        "captured": state.get("captured"),
        "discovery_result": state.get("discovery_result"),
        "offer_result": state.get("offer_result"),
        "checkout_preview": state.get("checkout_preview"),
        "quote_id": (state.get("quote") or {}).get("quote_id"),
        "approval_pending": state.get("next_action") == "checkout",
        "last_outcome": state.get("policy_decision"),
        "cart_version": state.get("cart_version"),
        "pending_suggested_sku": state.get("pending_suggested_sku"),
        "candidates": state.get("candidates"),
        "suggestions": state.get("suggestions"),
        "turns_so_far": (state.get("persisted") or {}).get("turns_so_far", 0) + 1,
    }
    session_state.save_state(state["session_id"], state["actor"], save)
    return state


# ---------------------------------------------------------------------------
# Node: audit — final per-turn row, locking in the trace envelope.
# ---------------------------------------------------------------------------

def audit_node(state):
    decision = state.get("policy_decision")
    payment_error = bool((state.get("payment_result") or {}).get("error"))
    if decision or not state.get("stages"):
        outcome = "blocked"
        if decision != "BLOCK":
            outcome = "failed" if payment_error else "success"
        observability.write_audit(
            session_id=state["session_id"],
            actor=state["actor"],
            event_type="chat_turn",
            tool_name="chat",
            parameters={"action": state.get("next_action")},
            decision=decision,
            reason=state.get("policy_reason") or (state.get("payment_result") or {}).get("error"),
            outcome=outcome,
            trace_id=state["trace_id"],
            stage="audit",
            latency_ms=state.get("_latency"),
            source=state.get("classification", {}).get("source", "router"),
            cache_hit=bool((state.get("discovery_result") or {}).get("cache_hit")),
            confidence=state.get("classification", {}).get("confidence"),
            tier_used=state.get("tier_used"),
            mandate_signature=(state.get("mandate_signature")
                               or (state.get("payment_result") or {}).get("mandate_signature")),
        )
    return state


# ---------------------------------------------------------------------------
# Public entry — runs the forward pass and returns the final state.
# ---------------------------------------------------------------------------

def run_turn(session_id: str, actor: str, user_text: str, persisted: dict | None = None) -> dict:
    """A single chat turn. Deterministic except for the one optional LLM call
    the router makes behind its confidence gate. Never raises."""
    trace_id = new_trace_id()
    persisted = persisted or {}
    initial = {
        "session_id": session_id,
        "actor": actor,
        "user_text": user_text,
        "persisted": persisted,
        "classification": None,
        "structured_requirements": persisted.get("structured_requirements"),
        "next_action": "idle",
        "discovery_result": persisted.get("discovery_result"),
        "captured": persisted.get("captured"),
        "offer_result": persisted.get("offer_result"),
        "cart_items": commerce.list_cart_items(session_id),
        "cart_version": quote_module.load_session_version(session_id, actor),
        "amount": None,
        "quote": persisted.get("quote"),
        "checkout_preview": persisted.get("checkout_preview"),
        "approval_confirmed": False,
        "policy_decision": None,
        "policy_reason": None,
        "payment_result": None,
        "block_reason": None,
        "reply": "",
        "trace_id": trace_id,
        "stages": [],
        "tools_called": [],
        "spend_so_far": _session_spend(session_id, actor),
        "llm_calls": 0,
        "pending_suggested_sku": persisted.get("pending_suggested_sku"),
        "candidates": persisted.get("candidates"),
        "fast_path": False,
        "tier_used": None,
        "route_intent": None,
        "discovery_pre_seed": None,
        "first_turn": _is_first_turn(persisted),
        "mandate_signature": None,
        "suggestions": persisted.get("suggestions"),
    }
    if initial["checkout_preview"]:
        initial["quote"] = quote_module.current_quote(session_id) or initial["quote"]

    try:
        with observability.run_turn_time(trace_id):
            state = graph.invoke(initial)
    except Exception:  # noqa: BLE001 — orchestration must never crash a turn
        state = {**initial, "reply": "I hit a snag processing that — please try again.", "policy_decision": "BLOCK"}
    state["_latency"] = observability.total_ms(trace_id)
    return state


def _is_first_turn(persisted: dict) -> bool:
    """True when nothing substantive has been persisted for this session yet
    (cascade Tier 1 only grades IS_FIRST_TURN product queries)."""
    return not bool(
        (persisted or {}).get("turns_so_far")
        or (persisted or {}).get("structured_requirements")
        or (persisted or {}).get("checkout_preview")
        or (persisted or {}).get("candidates")
        or (persisted or {}).get("pending_suggested_sku")
    )


def _session_spend(session_id: str, actor: str) -> float:
    db = db_module.SessionLocal()
    try:
        return float(
            db.query(func.coalesce(func.sum(Order.total), 0.0))
            .filter(
                Order.session_id == session_id,
                Order.actor == actor,
                Order.status.in_(_SPEND_STATUSES),
            )
            .scalar()
            or 0.0
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# LangGraph wiring — a thin forward pass, no agent<->tool loops.
# ---------------------------------------------------------------------------

def _route_target(state):
    """After route: a fast path forwards straight to respond (direct reply) or
    tool (a dispatched safe cart action); everything else goes to the agent
    pipeline. Fast-path edges NEVER touch approval/guardrail/pay."""
    if not state.get("fast_path"):
        return "agent"
    action = state.get("next_action")
    if isinstance(action, tuple) or action not in (None, "", "idle"):
        return "tool"
    return "respond"


def _tool_target(state):
    """After tool: money nodes are unreachable from a fast path — a fast-path
    tool dispatch goes straight to respond; the agent pipeline continues to
    approval/guardrail/pay (which short-circuit unless an approval is due)."""
    if not state.get("fast_path"):
        return "approval"
    return "respond"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("route", route_node)
    graph.add_node("agent", agent_node)
    graph.add_node("discover", discover_node)
    graph.add_node("tool", tool_node)
    graph.add_node("approval", approval_node)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("pay", pay_node)
    graph.add_node("respond", respond_node)
    graph.add_node("audit", audit_node)

    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        _route_target,
        {"agent": "agent", "tool": "tool", "respond": "respond"},
    )
    graph.add_edge("agent", "discover")
    graph.add_edge("discover", "tool")
    graph.add_conditional_edges(
        "tool",
        _tool_target,
        {"approval": "approval", "respond": "respond"},
    )
    graph.add_edge("approval", "guardrail")
    graph.add_edge("guardrail", "pay")
    graph.add_edge("pay", "respond")
    graph.add_edge("respond", "audit")
    graph.add_edge("audit", END)
    return graph.compile()


graph = build_graph()


# ---------------------------------------------------------------------------
# Convenience adapter for /chat and tests.
# ---------------------------------------------------------------------------

def chat_reply(req: ChatRequest) -> ChatResponse:
    persisted = session_state.load_state(req.session_id, req.actor)
    state = run_turn(req.session_id, req.actor, req.message, persisted)
    return ChatResponse(
        session_id=req.session_id,
        reply=state.get("reply", ""),
        tool_calls_made=state.get("tools_called", []),
        blocked=state.get("policy_decision") == "BLOCK",
    )