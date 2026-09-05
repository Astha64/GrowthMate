"""
Commerce engine (Revision 3) — deterministic cart, quote snapshot, payment.

Centralizes ALL money arithmetic and cart mutations. Contract rules enforced
here (ARCHITECTURE §5.2 / §10):

  - Payable cart items are `merchant` rows (catalog SKUs from the products
    table) or `reference` rows (SERP/EXT-xxx listings with a name + price
    supplied by discovery). Both are quoted/paid identically (Rev 3).
  - Cart totals are computed here with Decimal — never from the LLM.
  - Every mutation bumps the session cart version and invalidates open quotes
    so a stale quote can never pay for an altered cart.
  - Order creation is idempotent per (session, cart_hash): a repeated payment
    for the same snapshot reuses the existing payment link instead of charging
    twice.
"""

import json
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func

from app import db as db_module
from app.models import CartEvent, CartItem, Order, OrderItem, Product
from app.quote import bump_cart_version, cart_total, compute_cart_hash, invalidate_quote
from app.razorpay_client import create_payment_link as rzp_create_payment_link

_ACTOR_KEYS = ("human", "buyer_agent")


def _d(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_product_by_sku(sku: str) -> Product | None:
    db = db_module.SessionLocal()
    try:
        return db.query(Product).filter(Product.sku == sku.upper()).first()
    finally:
        db.close()


def list_cart_items(session_id: str) -> list[dict]:
    db = db_module.SessionLocal()
    try:
        rows = (
            db.query(CartItem)
            .filter(CartItem.session_id == session_id)
            .order_by(CartItem.item_type, CartItem.ref_id)
            .all()
        )
        return [
            {
                "sku": r.ref_id,
                "item_type": r.item_type,
                "ref_id": r.ref_id,
                "name": r.name,
                "price": r.price,
                "quantity": r.quantity,
                "currency": "INR",
                "source": r.source,
            }
            for r in rows
        ]
    finally:
        db.close()


def cart_summary(session_id: str) -> dict:
    """Latest backend-computed cart totals (the single source of truth)."""
    items = list_cart_items(session_id)
    total = cart_total(items)
    return {
        "items": items,
        "count": sum(int(i["quantity"]) for i in items),
        "subtotal": str(_d(total)),
        "total": str(_d(total)),
        "currency": "INR",
    }


def _mutate(session_id: str, actor: str, action: str, sku: str, quantity: int) -> dict:
    if actor not in _ACTOR_KEYS:
        return {"error": f"unknown actor: {actor}"}
    sku = sku.upper()
    db = db_module.SessionLocal()
    try:
        row = (
            db.query(CartItem)
            .filter(
                CartItem.session_id == session_id,
                CartItem.ref_id == sku,
                CartItem.item_type.in_(("merchant", "reference")),
            )
            .first()
        )
        # Merchant stock checks only apply to merchant rows. Reference items
        # (SERP listings, denoted EXT-xxx) have no catalog stock ceiling.
        product = get_product_by_sku(sku) if not row or row.item_type == "merchant" else None

        if action == "add":
            if product is None:
                return {"error": f"unknown merchant SKU: {sku} — use an SKU from a search result (EXT-xxx) or reply with a number I listed"}
            existing = row.quantity if row else 0
            if existing + quantity > (product.stock or 0):
                return {
                    "error": f"only {product.stock} in stock for {product.sku}",
                    **cart_summary(session_id),
                }
            if row:
                row.quantity = existing + quantity
                row.name = product.name
                row.price = product.price
            else:
                db.add(
                    CartItem(
                        session_id=session_id,
                        item_type="merchant",
                        ref_id=product.sku,
                        name=product.name,
                        price=product.price,
                        quantity=quantity,
                        source="merchant",
                    )
                )
        elif action == "set_quantity":
            if quantity <= 0:
                if row:
                    db.delete(row)
            else:
                if product is not None and quantity > (product.stock or 0):
                    return {
                        "error": f"only {product.stock} in stock for {product.sku}",
                        **cart_summary(session_id),
                    }
                if row:
                    row.quantity = quantity
                    if product:
                        row.name = product.name
                        row.price = product.price
                else:
                    return {"error": f"{sku} is not in your cart"}
        elif action == "remove":
            if row:
                db.delete(row)
            else:
                return {"error": f"{sku} is not in your cart"}
        else:
            return {"error": f"unknown cart action: {action}"}

        db.commit()
        db.flush()
        _log_cart_event(db, session_id, actor, sku, f"cart_{action}")
    finally:
        db.close()

    try:
        invalidate_quote(session_id)
    finally:
        pass
    version = bump_cart_version(session_id)
    summary = cart_summary(session_id)
    return {"action": action, "result": "ok", "cart_version": version, **summary}


def add_to_cart(session_id: str, actor: str, sku: str, quantity: int = 1) -> dict:
    return _mutate(session_id, actor, "add", sku, quantity)


def add_reference_to_cart(session_id: str, actor: str, ref_id: str, name: str,
                          price, source: str = "market",
                          currency: str = "INR", quantity: int = 1) -> dict:
    """Add a SERP/market listing (EXT-xxx) to the cart as a payable reference
    item. Merchant stock ceilings don't apply — the listing itself is the price.
    Dedupes by ref_id; repeats just bump the quantity."""
    if actor not in _ACTOR_KEYS:
        return {"error": f"unknown actor: {actor}"}
    try:
        price = float(price)
    except (TypeError, ValueError):
        return {"error": "invalid price for reference item"}
    if price <= 0 or not name:
        return {"error": "reference item needs a name and a positive price"}
    ref_id = ref_id.upper()
    qty = max(1, int(quantity or 1))

    db = db_module.SessionLocal()
    try:
        row = (
            db.query(CartItem)
            .filter(
                CartItem.session_id == session_id,
                CartItem.item_type == "reference",
                CartItem.ref_id == ref_id,
            )
            .first()
        )
        if row:
            row.quantity = int(row.quantity or 1) + qty
            row.price = price
        else:
            db.add(
                CartItem(
                    session_id=session_id,
                    item_type="reference",
                    ref_id=ref_id,
                    name=str(name)[:200],
                    price=price,
                    quantity=qty,
                    source=str(source or "market")[:50],
                )
            )
        db.commit()
        db.flush()
        _log_cart_event(db, session_id, actor, ref_id, "cart_add")
    finally:
        db.close()

    try:
        invalidate_quote(session_id)
    finally:
        pass
    version = bump_cart_version(session_id)
    summary = cart_summary(session_id)
    return {"action": "add", "result": "ok", "cart_version": version, **summary}


def remove_from_cart(session_id: str, actor: str, sku: str) -> dict:
    return _mutate(session_id, actor, "remove", sku, 0)


def set_quantity(session_id: str, actor: str, sku: str, quantity: int) -> dict:
    return _mutate(session_id, actor, "set_quantity", sku, quantity)


def create_payment_order(session_id: str, actor: str, cart_hash: str,
                         quote_id: str | None = None,
                         mandate_signature: str | None = None) -> dict:
    """Snapshot the cart into Order + OrderItem rows (idempotent per hash).

    `mandate_signature` is the HMAC binding minted on the guardrail ALLOW path
    (Phase 4) and stored with the snapshot so the order itself is tamper-evident.
    `quote_id` names the exact approved quote that snapshot came from, so the
    order-level mandate covers all five bound fields (session|actor|cart_hash|
    amount|quote_id).
    """
    db = db_module.SessionLocal()
    try:
        existing = (
            db.query(Order)
            .filter(
                Order.session_id == session_id,
                Order.actor == actor,
                Order.cart_hash == cart_hash,
            )
            .first()
        )
        if existing is not None:
            if mandate_signature and not existing.mandate_signature:
                existing.mandate_signature = mandate_signature
                existing.quote_id = existing.quote_id or quote_id
                db.commit()
            return _order_payload(existing)

        rows = db.query(CartItem).filter(CartItem.session_id == session_id).all()
        if not rows:
            return {"error": "cart is empty — nothing to charge"}

        currency = "INR"
        subtotal = cart_total(
            [dict(quantity=r.quantity, price=r.price) for r in rows]
        )
        order = Order(
            actor=actor,
            session_id=session_id,
            subtotal=float(subtotal),
            total=float(subtotal),
            currency=currency,
            cart_hash=cart_hash,
            quote_id=quote_id,
            status="created",
            mandate_signature=mandate_signature,
        )
        db.add(order)
        db.flush()
        for i in rows:
            db.add(
                OrderItem(
                    order_id=order.id,
                    name=i.name,
                    price=i.price,
                    quantity=i.quantity,
                    source=i.source,
                    ref_id=i.ref_id,
                )
            )
        db.commit()
        db.refresh(order)

        _log_cart_event(db, session_id, actor, None, "purchased")
        return _order_payload(order)
    finally:
        db.close()


def execute_payment(session_id: str, actor: str, cart_hash: str,
                    quote_id: str | None = None,
                    mandate_signature: str | None = None) -> dict:
    """Create/fetch the order snapshot then call Razorpay. Guardrails must
    already have passed — this function never re-decides payment eligibility.
    The optional `mandate_signature` (minted on the ALLOW path) is snapshotted
    onto the Order row so it travels with the charge."""
    order = create_payment_order(session_id, actor, cart_hash,
                                 quote_id=quote_id,
                                 mandate_signature=mandate_signature)
    if "error" in order:
        return {"error": order["error"]}

    if order.get("razorpay_payment_link_id"):
        return {
            "short_url": order["short_url"],
            "order_id": order["id"],
            "amount": order["total"],
            "currency": "INR",
            "razorpay_payment_link_id": order["razorpay_payment_link_id"],
        }

    db = db_module.SessionLocal()
    try:
        row = db.query(Order).filter(Order.id == order["id"]).first()
        result = rzp_create_payment_link(row)
        if "error" in result:
            row.status = "failed"
            db.commit()
            return {"error": result["error"], "order_id": row.id}
        row.razorpay_payment_link_id = result.get("id")
        row.razorpay_order_id = result.get("order_id")
        row.payment_short_url = result.get("short_url")
        db.commit()
        return {
            "short_url": result.get("short_url"),
            "order_id": row.id,
            "amount": str(_d(row.total)),
            "currency": "INR",
            "razorpay_payment_link_id": result.get("id"),
            "cart_hash": row.cart_hash,
        }
    finally:
        db.close()


def get_order_status(order_id: int) -> dict:
    db = db_module.SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return {"error": f"order {order_id} not found"}
        items = [
            {
                "sku": oi.ref_id,
                "name": oi.name,
                "price": oi.price,
                "quantity": oi.quantity,
            }
            for oi in order.items
        ]
        return {
            "order_id": order.id,
            "status": order.status,
            "subtotal": str(_d(order.subtotal)),
            "total": str(_d(order.total)),
            "currency": order.currency,
            "cart_hash": order.cart_hash,
            "items": items,
            "razorpay_payment_link_id": order.razorpay_payment_link_id,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
    finally:
        db.close()


def _order_payload(order) -> dict:
    return {
        "id": order.id,
        "status": order.status,
        "subtotal": str(_d(order.subtotal)),
        "total": str(_d(order.total)),
        "currency": order.currency,
        "cart_hash": order.cart_hash,
        "quote_id": order.quote_id,
        "session_id": order.session_id,
        "actor": order.actor,
        "mandate_signature": order.mandate_signature,
        "razorpay_payment_link_id": order.razorpay_payment_link_id,
        "short_url": order.payment_short_url,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def _log_cart_event(db, session_id: str, actor: str, ref_id: str | None, event_type: str) -> None:
    try:
        if not session_id or not actor:
            return
        db.add(CartEvent(session_id=session_id, actor=actor, ref_id=ref_id, event_type=event_type))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()