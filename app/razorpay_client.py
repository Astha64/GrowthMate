"""
Thin Razorpay SDK wrapper. Matches LOW_LEVEL_DESIGN.md §7 / §10.

  - create_payment_link(order): calls the Payment Links API, catches SDK
    exceptions and returns {"error": ...} — never raises up to tool_node.
    Does NOT touch Order rows itself; tools.py owns persistence.
    Revision 2: reads order.total (multi-item cart) instead of a single
    product's price*quantity.
  - verify_webhook_signature(payload_body, signature_header): constant-time
    HMAC-SHA256 check using RAZORPAY_KEY_SECRET.
"""

import hashlib
import hmac
import os
import uuid

from dotenv import load_dotenv
import razorpay

load_dotenv()

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")


def _client() -> razorpay.Client:
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def create_payment_link(order) -> dict:
    """
    Calls Razorpay Payment Links API with order.total / order.currency and a
    reference id = order.id. Returns {"short_url": ..., "id": ...} on success,
    or {"error": str(e)} on SDK exception — never raises.

    reference_id must be unique per Razorpay account; a checkout may be retried
    more than once, so an attempt-scoped suffix (not just the order id) avoids
    the "reference_id already exists" conflict.
    """
    try:
        client = _client()
        data = {
            "amount": int(round(order.total * 100)),  # paise; multi-item total
            "currency": order.currency,
            "description": f"GrowthMate order #{order.id}",
            "reference_id": f"gm-order-{order.id}-{uuid.uuid4().hex[:8]}",
            "notes": {"order_id": order.id},
        }
        response = client.payment_link.create(data)
        return {
            "id": response.get("id"),
            "short_url": response.get("short_url"),
            "order_id": response.get("order_id"),
        }
    except Exception as e:  # noqa: BLE001 - degrade to dict, never raise
        return {"error": str(e)}


def verify_webhook_signature(payload_body: bytes, signature_header: str) -> bool:
    """
    HMAC-SHA256 of payload_body using RAZORPAY_KEY_SECRET, compared to
    signature_header via hmac.compare_digest (constant-time).
    """
    secret = RAZORPAY_KEY_SECRET.encode("utf-8")
    expected = hmac.new(secret, payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
