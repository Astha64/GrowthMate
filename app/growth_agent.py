"""
Growth-recovery agent (Rev 3, Phase 6) — deterministic, money-safe recovery.

Actors: `system_growth_agent` (the proposer) wins back a checkout that the
deterministic guardrails declined *spending-level* (e.g. a cart whose total
blows the per-transaction cap). It proposes a strict *quantity-fit* recovery:

  - only ever REDUCES the dominant SKU's quantity, never touches unit price and
    never invents discounts (a discount would silently change the price a
    merchant quote was built on — out of scope);
  - the proposed quantity is the exact ceiling `floor(per_transaction_limit /
    unit_price)`, i.e. the largest cart total that still passes
    `guardrail.check_transaction`;
  - a floor `MIN_RECOVERY_AMOUNT` guards micro-transactions (an offer below it
    is not worth a charge and is declined deterministically).

Money-safety contract (same as guardrails): the recovery agent NEVER executes
payment and NEVER mutates the cart. It only emits a proposal (plus its audit
row); the buyer still drives a normal quote -> approval -> guardrail -> pay
pipeline for the adjusted cart so every money move keeps its HMAC mandate.

Deterministic: same cart + same limits => same proposal, and ties on dominant
SKU break by SKU. Fail-open: never raises; a non-eligible cart returns
`eligible: false` with a plain reason.
"""

from app import db as db_module
from app.config import MIN_RECOVERY_AMOUNT
from app.guardrail import MAX_PER_TRANSACTION
from app.models import CartItem

RECOVERY_ACTOR = "system_growth_agent"


def _dominant_item(items: list[dict]) -> dict | None:
    """Highest price*quantity item; deterministic tie-break by SKU."""
    scored = []
    for item in items:
        price = float(item.get("price") or 0.0)
        qty = int(item.get("quantity") or 0)
        if price <= 0 or qty <= 0:
            continue
        scored.append((price * qty, str(item.get("sku") or ""), item))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][2]


def recover_cart(session_id: str, actor: str) -> dict:
    """Propose (never execute) the deterministic quantity-fit recovery."""
    limit = MAX_PER_TRANSACTION.get(actor)
    if limit is None or limit <= 0:
        return {"eligible": False, "reason": "no recoverable per-transaction limit for actor",
                "sku": None, "from_quantity": 0, "to_quantity": 0,
                "old_total": "0.00", "new_total": "0.00", "limit": 0.0}

    db = db_module.SessionLocal()
    try:
        items = [
            {
                "sku": row.ref_id,
                "name": row.name,
                "price": float(row.price),
                "quantity": int(row.quantity),
            }
            for row in db.query(CartItem)
            .filter(CartItem.session_id == session_id)
            .all()
        ]
    finally:
        db.close()

    old_total = round(sum(float(i["price"]) * int(i["quantity"]) for i in items), 2)
    if old_total <= 0:
        return {"eligible": False, "reason": "cart is empty — nothing to recover",
                "sku": None, "from_quantity": 0, "to_quantity": 0,
                "old_total": "0.00", "new_total": "0.00", "limit": limit}
    if old_total <= limit:
        return {"eligible": False, "reason": "cart total already within the per-transaction limit",
                "sku": None, "from_quantity": 0, "to_quantity": 0,
                "old_total": f"{old_total:.2f}", "new_total": f"{old_total:.2f}", "limit": limit}

    item = _dominant_item(items)
    price = float(item["price"])
    current_qty = int(item["quantity"])
    ceiling = int(limit // price)  # largest quantity that stays <= limit
    proposed_qty = min(current_qty, ceiling)
    new_total = round(price * proposed_qty, 2)

    if proposed_qty < 1:
        return {"eligible": False, "reason": "single unit alone exceeds the limit — cannot recover",
                "sku": item["sku"], "from_quantity": current_qty, "to_quantity": 0,
                "old_total": f"{old_total:.2f}", "new_total": "0.00", "limit": limit}
    if proposed_qty >= current_qty or new_total <= 0:
        return {"eligible": False, "reason": "quantity adjustment cannot bring the cart in line",
                "sku": item["sku"], "from_quantity": current_qty, "to_quantity": proposed_qty,
                "old_total": f"{old_total:.2f}", "new_total": f"{new_total:.2f}", "limit": limit}
    if new_total < MIN_RECOVERY_AMOUNT:
        return {"eligible": False, "reason": "recovered total would fall below the minimum transaction",
                "sku": item["sku"], "from_quantity": current_qty, "to_quantity": proposed_qty,
                "old_total": f"{old_total:.2f}", "new_total": f"{new_total:.2f}", "limit": limit}

    return {
        "eligible": True,
        "reason": "quantity-fit recovery",
        "sku": item["sku"],
        "name": item.get("name"),
        "from_quantity": current_qty,
        "to_quantity": proposed_qty,
        "old_total": f"{old_total:.2f}",
        "new_total": f"{new_total:.2f}",
        "limit": limit,
    }