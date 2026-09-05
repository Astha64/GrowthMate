"""
GrowthMate FastAPI routes — routing only, no business logic (LLD §10 / §22).

Endpoints:
  GET  /.well-known/agent-commerce.json   agent-commerce capability manifest
  GET  /health                            liveness
  GET  /catalog                           merchant catalog (LLD §3)
  POST /chat                              agent conversation (human | buyer_agent)
  GET  /audit                             audit trail (LLD §3)
  POST /agent/discover                    agent discovery (LLD §24)
  POST /agent/quote                       immutable backend quote (LLD §14/§23)
  POST /agent/checkout                    guarded payment execution (LLD §16/§24)
  GET  /agent/order/{order_id}            order status (LLD §24)
  GET  /metrics/latency                   stage P50/P95 latencies (LLD §26)
  POST /webhook/razorpay                  Razorpay payment-status webhook
"""

import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app import agent_feed
from app import db as db_module
from app import discovery as discovery_wire
from app import growth_agent
from app import merchant_capture
from app import observability
from app import orchestration
from app import quote as quote_module
from app import router as router_module
from app.commerce import (
    add_reference_to_cart,
    add_to_cart,
    cart_summary,
    execute_payment,
    get_order_status,
    get_product_by_sku,
    remove_from_cart,
)
from app.db import get_db, init_db
from app.guardrail import (
    check_transaction,
    validate_quote_against_cart,
)
from app.models import AuditLog, Order, Product
from app.observability import new_trace_id
from app.razorpay_client import verify_webhook_signature
from app.schemas import (
    AgentCommerceManifest,
    AuditLogOut,
    CatalogResponse,
    CheckoutRequest,
    CheckoutResponse,
    ChatRequest,
    ChatResponse,
    DiscoverCandidate,
    DiscoverRequest,
    DiscoverResponse,
    HealthResponse,
    MetricsOut,
    OrderOut,
    ProductOut,
    QuoteRequest,
    QuoteResponse,
    RecoveryRequest,
    RecoveryResponse,
    WebhookResponse,
)

app = FastAPI(title="GrowthMate API", version="0.2.0")


@app.on_event("startup")
def _startup() -> None:
    """Boot checks (Rev 3, Phase 4):

    - MANDATE_SECRET must be configured: the guardrail ALLOW path mints an
      HMAC mandate with it (app/mandate.py). Starting without it would mint
      signatures over an empty secret — fail fast instead.
    - Warm the offline catalog RAG index (Phase 2) — fail-open, the feed and
      retrieval degrade gracefully when the index cannot be built.
    """
    from app import mandate as mandate_module

    if not mandate_module.mandate_configured():
        raise RuntimeError(
            "MANDATE_SECRET is not set. The guardrail ALLOW path signs a "
            "deterministic HMAC mandate with it; refusing to boot without it. "
            "Set MANDATE_SECRET in .env (see .env.example)."
        )

    try:
        from app import catalog_index
        from app.config import CATALOG_INDEX_PATH

        if not catalog_index.is_current():
            catalog_index.build(index_path=CATALOG_INDEX_PATH)
        catalog_index.backfill_embedding_columns()
    except Exception:  # noqa: BLE001 — index is an optimization, not a gate
        pass


init_db()

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/", include_in_schema=False)
def root():
    return {"message": "GrowthMate API is running. See /docs for API docs."}


@app.get("/health", response_model=HealthResponse)
def health_check():
    return HealthResponse(status="ok", service="growthmate-backend")


@app.get("/catalog", response_model=CatalogResponse)
def get_catalog(db: Session = Depends(get_db)):
    """Merchant catalog: explicit fields + currency, no formatting (LLD §3)."""
    products = db.query(Product).order_by(Product.sku).all()
    return CatalogResponse(
        currency="INR",
        products=[
            ProductOut(
                sku=p.sku,
                name=p.name,
                description=p.description,
                price=p.price,
                stock=p.stock,
                category=p.category,
            )
            for p in products
        ],
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """Runs the thin forward-pass coordinator for this message (human or
    buyer_agent). Money actions pass approval -> quote/cart integrity ->
    guardrail; blocks return HTTP 200 with blocked=true (expected control flow)."""
    response = orchestration.chat_reply(req)
    return ChatResponse(
        session_id=response.session_id,
        reply=response.reply,
        tool_calls_made=response.tool_calls_made,
        blocked=response.blocked,
        trace_id=response.trace_id,
    )


@app.get("/audit", response_model=list[AuditLogOut])
def get_audit(session_id: str | None = None, db: Session = Depends(get_db)):
    """Audit trail, most recent first. Optional session_id filter (LLD §3)."""
    q = db.query(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if session_id:
        q = q.filter(AuditLog.session_id == session_id)
    rows = q.limit(200).all()
    return [
        AuditLogOut(
            id=r.id,
            session_id=r.session_id,
            actor=r.actor,
            event_type=r.event_type,
            tool_name=r.tool_name,
            parameters_json=r.parameters_json,
            agent_reasoning=r.agent_reasoning,
            decision=r.decision,
            reason=r.reason,
            outcome=r.outcome,
            error_detail=r.error_detail,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Agent-commerce capability manifest (LLD §22).
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent-commerce.json", response_model=AgentCommerceManifest)
def agent_commerce_manifest():
    return AgentCommerceManifest(
        schema_version="1.0",
        name="GrowthMate Merchant Agent",
        description="Merchant-fulfillable product discovery, quotes, and guarded checkout for autonomous buyer agents.",
        currencies=["INR"],
        operations=[
            {"name": "discover", "method": "POST", "path": "/agent/discover"},
            {"name": "quote", "method": "POST", "path": "/agent/quote"},
            {"name": "checkout", "method": "POST", "path": "/agent/checkout"},
            {"name": "order_status", "method": "GET", "path": "/agent/order/{order_id}"},
        ],
        capabilities={
            "payable_skus_only": True,
            "external_payable": True,
            "quote_binding": "cart_hash",
            "approval": "explicit buyer confirmation",
            "guardrails": "deterministic spend limits",
        },
    )


# ---------------------------------------------------------------------------
# Phase 5 agent feeds: deterministic catalog + mandate-reverified order ledger.
# ---------------------------------------------------------------------------

@app.get("/.well-known/agentic-catalog.json")
def agentic_catalog_feed():
    """The deterministic merchant catalog (from the Phase-2 RAG index rows) plus
    endpoint metadata. Amounts are build-time snapshots — the authoritative price
    for any money move is the backend quote, never this feed."""
    return agent_feed.catalog_feed()


@app.get("/.well-known/agentic-orders.json")
def agentic_orders_feed(session_id: str | None = None):
    """Order ledger; every read re-verifies each order's HMAC mandate and marks
    `mandate_verified` per row so agents see tamper evidence, never a 5xx."""
    return agent_feed.orders_feed(session_id=session_id)


# ---------------------------------------------------------------------------
# Phase 6 growth-recovery agent: deterministic quantity-fit proposal. Never a
# money move — the buyer still goes through quote/approval/guardrail on the
# adjusted cart. Actor on the audit row is `system_growth_agent`.
# ---------------------------------------------------------------------------

@app.post("/agent/recovery", response_model=RecoveryResponse)
def agent_recovery(req: RecoveryRequest):
    """Propose (never execute) a quantity-fit recovery for a cart that the
    deterministic guardrails declined on spend limits."""
    trace_id = new_trace_id()
    proposal = growth_agent.recover_cart(req.session_id, req.actor)

    observability.write_audit(
        session_id=req.session_id,
        actor=growth_agent.RECOVERY_ACTOR,
        event_type="recovery_offer",
        tool_name="agent_recovery",
        parameters={
            "buyer_actor": req.actor,
            "sku": proposal.get("sku"),
            "from_quantity": proposal.get("from_quantity"),
            "to_quantity": proposal.get("to_quantity"),
            "old_total": proposal.get("old_total"),
            "new_total": proposal.get("new_total"),
        },
        decision="ALLOW" if proposal["eligible"] else "BLOCK",
        reason=proposal["reason"],
        outcome="success",
        trace_id=trace_id,
        stage="recovery",
    )
    return RecoveryResponse(
        eligible=proposal["eligible"],
        reason=proposal["reason"],
        sku=proposal.get("sku"),
        name=proposal.get("name"),
        from_quantity=proposal.get("from_quantity", 0),
        to_quantity=proposal.get("to_quantity", 0),
        old_total=proposal.get("old_total", "0.00"),
        new_total=proposal.get("new_total", "0.00"),
        limit=float(proposal.get("limit", 0.0)),
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# Agent-commerce APIs (LLD §24). Same policy plane as the human chat UI.
# ---------------------------------------------------------------------------
# Agent-commerce APIs (LLD §24). Same policy plane as the human chat UI.
# ---------------------------------------------------------------------------

@app.post("/agent/discover", response_model=DiscoverResponse)
def agent_discover(req: DiscoverRequest):
    """External market + merchant-match discovery. External candidates are
    addable/payable via their EXT-xxx SKU (Rev 3 — any SERP listing can go to
    cart -> quote -> checkout); merchant captures surface the merchant SKU too."""
    trace_id = new_trace_id()
    with observability.stage("agent_discover"):
        explicit = orchestration._explicit_item_terms(req.query or "")
        requirements = {
            "category": router_module.extract_category(req.query),
            "product_type": router_module.extract_category(req.query),
            "budget": req.budget,
            "keywords": list(req.features or []) + router_module.extract_features(req.query) + explicit,
            "required_features": list(req.features or []) + router_module.extract_features(req.query),
            "explicit_item": explicit,
        }
        result = discovery_wire.discover(requirements, req.session_id)
        cached = result.get("cache_hit", False)

        recs = result.get("recommendations", [])[:5]
        candidates = [
            DiscoverCandidate(
                name=c.get("name", ""),
                price=float(c.get("price", 0.0)),
                currency=c.get("currency", "INR"),
                source=c.get("source", ""),
                sku=f"EXT-{i + 1:03d}",
                payable=True,
                why=c.get("why"),
            )
            for i, c in enumerate(recs)
        ]

        best, cap = _best_merchant_capture(requirements, merchant_capture)
        # Same force-fit guard as the chat path: a specifically-named item the
        # merchant catalog does not stock must not be matched to a
        # same-category neighbor (e.g. "wireless mouse" -> earbuds).
        explicit = orchestration._explicit_item_terms(req.query)
        if explicit and best:
            hay = " ".join(
                [
                    str(best.get("name", "")),
                    str(best.get("category", "")) or "",
                    str(best.get("description", "") or ""),
                ]
            ).lower()
            if not any(n in hay for n in explicit):
                cap = {
                    **cap,
                    "verdict": "no_merchant_match",
                    "reason": f"merchant catalog does not stock the named item ({', '.join(explicit)})",
                }
                best = None
        if cap.get("verdict") in ("capture", "recommend_offers") and cap.get("product"):
            merchant_match = cap
        else:
            merchant_match = None

        # Ranked top-N payable shortlist for automated buyers.
        merchant_candidates = _merchant_candidate_shortlist(
            requirements, orchestration, req.session_id
        )

        observability.write_audit(
            session_id=req.session_id,
            actor=req.actor,
            event_type="discovery",
            tool_name="agent_discover",
            parameters={"query": req.query, "budget": req.budget},
            outcome="success",
            trace_id=trace_id,
            stage="agent_discover",
            source="cache" if cached else "serpapi",
            cache_hit=cached,
            confidence=cap.get("score"),
        )
        return DiscoverResponse(
            query=req.query,
            count=len(candidates),
            cache_hit=cached,
            candidates=candidates,
            merchant_match=merchant_match,
            merchant_candidates=merchant_candidates,
            trace_id=trace_id,
        )


def _merchant_candidate_shortlist(requirements, orchestration, session_id: str) -> list[dict]:
    """Ranked, payable merchant shortlist (top-N by deterministic fit) for the
    automated-buyer discover flow. Each candidate carries its confidence plus a
    complementary cross-sell summary so the agent can upsell deterministically.
    """
    if not requirements.get("product_type") and not requirements.get("category"):
        return []
    products = orchestration._merchant_products()
    matches = merchant_capture.top_merchant_matches(products, requirements)
    out = []
    for rank, cap in enumerate(matches, 1):
        card = cap.get("product") or {}
        sku = card.get("sku")
        if not sku:
            continue
        out.append(
            {
                "rank": rank,
                "sku": sku,
                "name": card.get("name"),
                "price": float(card.get("price") or 0.0),
                "currency": card.get("currency", "INR"),
                "payable": True,
                "confidence": round(float(cap.get("capped") or 0.0), 3),
                "why": cap.get("reason"),
                "complementary": _cross_sell_names(sku, session_id),
            }
        )
    return out


def _cross_sell_names(sku: str, session_id: str) -> list[dict]:
    try:
        from app.cross_sell import complements_for

        comps = complements_for(sku, session_id)
    except Exception:  # noqa: BLE001 — cross-sell is optional sugar
        return []
    return [
        {"name": c["name"], "sku": c.get("sku"), "payable": c.get("payable", False)}
        for c in (comps.get("catalog") or [])[:3]
    ] + [
        {"name": c["name"], "payable": c.get("payable", False),
         "price": c.get("price"), "currency": c.get("currency")}
        for c in (comps.get("external") or [])[:2]
    ]


def _best_merchant_capture(requirements, merchant_capture):
    from app.orchestration import _merchant_products

    products = _merchant_products()
    best = None
    best_score = -1.0
    for p in products:
        score = merchant_capture.score_merchant_product(p, requirements)["capped"]
        if score > best_score:
            best_score = score
            best = p
    return best, merchant_capture.capture_verdict(best, requirements)


@app.post("/agent/quote", response_model=QuoteResponse)
def agent_quote(req: QuoteRequest):
    """Create an immutable, backend-owned quote for the given merchant cart.
    Invalidates prior quotes; cart ops are the same deterministic layer the
    human chat uses."""
    trace_id = new_trace_id()
    with observability.stage("quote"):
        # Rebuild the cart to exactly the requested merchant items.
        summary = cart_summary(req.session_id)
        for item in summary["items"]:
            remove_from_cart(req.session_id, req.actor, item["sku"])
        for item in req.cart:
            product = get_product_by_sku(item.sku)
            if product is not None:
                add_to_cart(req.session_id, req.actor, item.sku, item.quantity)
            elif item.sku.upper().startswith("EXT-") and item.name and item.price:
                # External (SERP) listing payload the buyer got from /agent/discover.
                add_reference_to_cart(
                    req.session_id, req.actor, item.sku,
                    item.name, item.price, source=item.source or "market",
                    quantity=item.quantity,
                )
            else:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"unknown SKU {item.sku}: pass a merchant SKU or an external "
                        f"EXT-xxx listing with name/price (from /agent/discover)"
                    ),
                )

        items = cart_summary(req.session_id)["items"]
        version = quote_module.load_session_version(req.session_id, req.actor)
        quote = quote_module.create_quote(req.session_id, req.actor, items, version)

    observability.write_audit(
        session_id=req.session_id,
        actor=req.actor,
        event_type="quote_created",
        tool_name="agent_quote",
        parameters={"cart": [i.sku for i in req.cart]},
        outcome="success",
        trace_id=trace_id,
        stage="quote",
        confidence=float(quote["cart_version"]),
    )
    return QuoteResponse(
        quote_id=quote["quote_id"],
        session_id=quote["session_id"],
        actor=quote["actor"],
        amount=quote["amount"],
        currency=quote["currency"],
        cart_hash=quote["cart_hash"],
        cart_version=quote["cart_version"],
        nonce=quote["nonce"],
        status=quote["status"],
        expires_at=quote["expires_at"],
        items=items,
        trace_id=trace_id,
    )


@app.post("/agent/checkout", response_model=CheckoutResponse)
def agent_checkout(req: CheckoutRequest):
    """Guardrailed checkout: quote must be active and match the current cart
    hash, the nonce must be the approval token, and spend limits must pass.
    A block is HTTP 200 control flow with blocked=true — never an error, and
    never a Razorpay call."""
    trace_id = new_trace_id()
    quote = quote_module.get_quote(req.quote_id, req.session_id)

    if quote is None:
        return _checkout_blocked(trace_id, req, "quote not found or superseded")
    if quote["nonce"] != req.nonce:
        return _checkout_blocked(trace_id, req, "approval token mismatch")

    items = cart_summary(req.session_id)["items"]
    version = quote_module.load_session_version(req.session_id, req.actor)
    ok, reason = validate_quote_against_cart(quote, items, version)
    if not ok:
        return _checkout_blocked(trace_id, req, reason)

    import decimal

    decision = check_transaction(req.actor, decimal.Decimal(quote["amount"]), _session_spend(req))
    if not decision.allowed:
        return _checkout_blocked(trace_id, req, decision.reason)

    # Guardrail ALLOW: bind the exact approved snapshot with a deterministic
    # HMAC mandate (Phase 4). It travels onto the Order row and the audit trail.
    from app import mandate as mandate_module

    signature = mandate_module.sign_mandate(
        session_id=req.session_id,
        actor=req.actor,
        cart_hash=quote["cart_hash"],
        amount=decimal.Decimal(quote["amount"]),
        quote_id=req.quote_id,
    )
    with observability.stage("razorpay"):
        result = execute_payment(req.session_id, req.actor, quote["cart_hash"],
                                 quote_id=req.quote_id,
                                 mandate_signature=signature)
    if "error" in result:
        observability.write_audit(
            session_id=req.session_id,
            actor=req.actor,
            event_type="payment",
            tool_name="agent_checkout",
            parameters={"quote_id": req.quote_id, "nonce": req.nonce},
            decision="BLOCK",
            reason=result["error"],
            outcome="failed",
            trace_id=trace_id,
            stage="razorpay",
            mandate_signature=None,
        )
        return CheckoutResponse(blocked=True, reason=result["error"], trace_id=trace_id)

    quote_module.invalidate_quote(req.session_id)
    observability.write_audit(
        session_id=req.session_id,
        actor=req.actor,
        event_type="payment",
        tool_name="agent_checkout",
        parameters={"quote_id": req.quote_id, "nonce": req.nonce,
                    "mandate_signature": signature},
        decision="ALLOW",
        reason="approval + quote integrity + spend limits satisfied",
        outcome="success",
        trace_id=trace_id,
        stage="razorpay",
        mandate_signature=signature,
    )
    return CheckoutResponse(
        blocked=False,
        order_id=result.get("order_id"),
        payment_link=result.get("short_url"),
        amount=result.get("amount"),
        currency="INR",
        trace_id=trace_id,
    )


def _checkout_blocked(trace_id: str, req: CheckoutRequest, reason: str) -> CheckoutResponse:
    """Deterministic block: HTTP 200 control flow, audited, Razorpay untouched."""
    observability.write_audit(
        session_id=req.session_id,
        actor=req.actor,
        event_type="guardrail_decision",
        tool_name="agent_checkout",
        parameters={"quote_id": req.quote_id, "nonce": req.nonce},
        decision="BLOCK",
        reason=reason,
        outcome="blocked",
        trace_id=trace_id,
        stage="guardrail",
    )
    return CheckoutResponse(blocked=True, reason=reason, trace_id=trace_id)


def _session_spend(req) -> float:
    from sqlalchemy import func as _func

    db = db_module.SessionLocal()
    try:
        return float(
            db.query(_func.coalesce(_func.sum(Order.total), 0.0))
            .filter(
                Order.session_id == req.session_id,
                Order.actor == req.actor,
                Order.status.in_(["created", "paid"]),
            )
            .scalar()
            or 0.0
        )
    finally:
        db.close()


@app.get("/agent/order/{order_id}", response_model=OrderOut)
def agent_order(order_id: int):
    result = get_order_status(order_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return OrderOut(
        order_id=result["order_id"],
        status=result["status"],
        subtotal=result["subtotal"],
        total=result["total"],
        currency=result["currency"],
        cart_hash=result.get("cart_hash"),
        items=result.get("items", []),
        razorpay_payment_link_id=result.get("razorpay_payment_link_id"),
        created_at=result.get("created_at"),
    )


@app.get("/metrics/latency", response_model=MetricsOut)
def metrics_latency():
    """Stage-level P50/P95 latency (LLD §26). Demo-friendly before/after view."""
    entries = observability.latency_summary()
    total = sum(e["count"] for e in entries.values())
    return MetricsOut(entries=entries, total_requests=total)


@app.post("/webhook/razorpay", response_model=WebhookResponse)
async def razorpay_webhook(request: Request):
    """Razorpay payment-status webhook. Verifies HMAC on the raw body; valid
    events are applied idempotently (LLD §11.6 / build-prompt failure rules)."""
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="invalid signature")

    payload = json.loads(body)
    event = payload.get("event", "")
    entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    payment_link_id = entity.get("id")

    db = db_module.SessionLocal()
    try:
        if payment_link_id:
            orders = (
                db.query(Order)
                .filter(Order.razorpay_payment_link_id == payment_link_id)
                .all()
            )
            for order in orders:
                if "paid" in event:
                    order.status = "paid"
                elif "expired" in event:
                    order.status = "failed"
                else:
                    continue
            db.commit()
        return WebhookResponse(status="processed")
    finally:
        db.close()