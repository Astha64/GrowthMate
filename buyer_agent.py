"""
buyer_agent.py — standalone external AI-buyer simulation (agent-commerce API).

Speaks to the running GrowthMate API over HTTP exactly like a real third-party
agent would — uses ONLY `requests`, and must NEVER import from app/.

Drives Journey A and B through the first-class agent-commerce API:
  GET  /.well-known/agent-commerce.json   capability discovery (optional)
  POST /agent/discover                    market + merchant discovery
  POST /agent/quote                       immutable backend quote
  POST /agent/checkout                    guarded payment execution
  GET  /agent/order/{order_id}            order status

Journey A (success): discover -> pick the top payable candidate -> quote ->
    checkout within the buyer per-transaction limit (₹3000). With no seeded
    merchant catalog (Rev 3), the top candidate is a SERP market listing
    carrying its own EXT-xxx sku/name/price from /agent/discover.
Journey B (engineered failure): quotes the SAME market listing at a quantity
    whose total exceeds the ₹3000 per-transaction limit; the API returns HTTP
    200 with blocked=true and a structured refusal. A block is expected control
    flow, not an error.
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BUYER_BASE_URL", "http://127.0.0.1:8000")
SESSION_ID = os.getenv("BUYER_SESSION_ID", "sess-buyer-agent-api")
ACTOR = "buyer_agent"


def _post(path: str, payload: dict, timeout: int = 60) -> dict:
    resp = requests.post(f"{BASE_URL}{path}", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get(path: str, timeout: int = 30) -> dict:
    resp = requests.get(f"{BASE_URL}{path}", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def discover(query: str, budget: float | None = None, features: list[str] | None = None) -> dict:
    return _post("/agent/discover", {
        "session_id": SESSION_ID,
        "actor": ACTOR,
        "query": query,
        "budget": budget,
        "features": features or [],
    })


def quote(cart: list[dict]) -> dict:
    return _post("/agent/quote", {
        "session_id": SESSION_ID,
        "actor": ACTOR,
        "cart": cart,
    })


def checkout(quote_id: str, nonce: str) -> dict:
    return _post("/agent/checkout", {
        "session_id": SESSION_ID,
        "actor": ACTOR,
        "quote_id": quote_id,
        "nonce": nonce,
    })


def main() -> None:
    print(f"buyer_agent connecting to {BASE_URL} (session {SESSION_ID}, actor={ACTOR})")

    manifest = _get("/.well-known/agent-commerce.json")
    print(f"capabilities: {sorted(o['name'] for o in manifest['operations'])}")

    # --- Journey A: ranked discovery -> pick top payable -> quote -> checkout ---
    print("Journey A -> discover('running shoes under 2500')")
    d = discover("running shoes under 2500", budget=2500.0)
    shortlist = d.get("merchant_candidates") or []
    if shortlist:
        top = shortlist[0]
        print(f"Journey A -> top merchant candidate: {top['name']} ({top['sku']}) "
              f"₹{top['price']} confidence={top['confidence']}")
        if top.get("complementary"):
            print(f"  cross-sell: {[c['name'] for c in top['complementary']]}")
        sku, qty = top["sku"], 1
        q = quote([{"sku": sku, "quantity": qty}])
    else:
        # Rev 3: no seeded merchant catalog — the SERP market candidate (EXT-xxx)
        # is itself payable and quoted directly with its listing name/price.
        candidates = [c for c in (d.get("candidates") or [])
                      if c.get("payable") and (c.get("price") or 0) > 0]
        top = candidates[0] if candidates else None
        if top is None:
            print("Journey A: no payable market candidate returned; cannot proceed.")
            sys.exit(2)
        print(f"Journey A -> top SERP market candidate: {top['name']} "
              f"({top.get('sku')}) ₹{top['price']} payable=true")
        q = quote([{"sku": top["sku"], "name": top["name"], "price": top["price"],
                    "quantity": 1}])

    print(f"Journey A quote: {q['quote_id']} amount {q['amount']} {q['currency']} "
          f"hash {q['cart_hash'][:12]}…")
    co = checkout(q["quote_id"], q["nonce"])
    if co.get("blocked"):
        print(f"Journey A BLOCKED: {co.get('reason')}")
    else:
        print(f"Journey A OK: order={co.get('order_id')} link={co.get('payment_link')}")
        order = _get(f"/agent/order/{co['order_id']}")
        print(f"Journey A order status: {order.get('status')} total {order.get('total')}")

    # --- Journey B: engineered failure (cart total exceeds per-transaction limit)
    # ---
    # The quote-bound total must exceed the buyer_agent ₹3000 per-transaction
    # limit. Quantity is computed from the SAME market listing used in Journey A
    # (price // limit + 1 with a minimum of 1) so no seeded catalog is needed.
    price = float(top.get("price") or 0.0)
    over_qty = max(1, int(3000 // price) + 1) if price > 0 else 7
    print(f"Journey B -> quoting {over_qty}x {top.get('sku')} (₹{price} each = "
          f"₹{over_qty * price:.0f}, exceeds ₹3000 limit)")
    payload_b = {"sku": top["sku"], "quantity": over_qty}
    if str(top.get("sku", "")).startswith("EXT-"):
        payload_b["name"] = top["name"]
        payload_b["price"] = top["price"]
    q2 = quote([payload_b])
    print(f"Journey B quote: {q2['quote_id']} amount {q2['amount']}")
    co2 = checkout(q2["quote_id"], q2["nonce"])
    print(f"Journey B confirm -> blocked={co2.get('blocked')} reason={co2.get('reason')}")

    if co2.get("blocked") is True:
        print("SUCCESS: engineered failure handled gracefully (HTTP 200, blocked=true)")
    else:
        print("WARNING: Journey B did not return blocked=true — check guardrail limits.")
        sys.exit(2)


if __name__ == "__main__":
    main()