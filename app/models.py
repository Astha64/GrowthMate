"""
SQLAlchemy models for GrowthMate — Revision 3 (target architecture).

Revision 3 additions vs Rev-2:
  - Product: semantic_text / embedding (JSON), merchant_priority, unit_cost, margin_pct
  - ExternalProductListing: query_normalized, embedding, retrieved_at, expires_at
  - SearchCache: semantic external cache (LLD §8.1)
  - Quote: immutable quote tied to cart_version + cart_hash (LLD §14)
  - OfferEvent: next-best-offer feedback loop (LLD §12.4)
  - SessionState: compact persisted session state (LLD §4 / HLD §3.5)
  - AuditLog: trace_id / stage / latency_ms / source / cache_hit / confidence
  - Order: cart_hash for idempotent payment linkage

Money amounts are computed as Decimal in Python; DB columns stay Float/String
for SQLite simplicity (no migrations — see AGENTS.md).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# §2.1  Product — merchant catalog. Merchant-fulfillable inventory ONLY.
# ---------------------------------------------------------------------------

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    stock = Column(Integer, nullable=False, default=0)
    category = Column(String(100), nullable=True)
    semantic_text = Column(Text, nullable=True)          # name+category+desc+brand/use-cases
    embedding = Column(Text, nullable=True)              # JSON list of floats
    merchant_priority = Column(Float, nullable=False, default=0.0)
    unit_cost = Column(Float, nullable=True)             # optional economics
    margin_pct = Column(Float, nullable=True)            # optional economics
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §2.2  ExternalProductListing — SERP market listings. Since Rev 3 these are
# addable/payable (they enter the cart as EXT-xxx reference CartItems); the
# table retains the raw listing metadata for context/audit.
# ---------------------------------------------------------------------------

class ExternalProductListing(Base):
    __tablename__ = "external_product_listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False, index=True)
    source = Column(String(50), nullable=False)
    source_url = Column(Text, nullable=True)
    name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    brand = Column(String(100), nullable=True)
    features_json = Column(Text, nullable=True)
    rating = Column(Float, nullable=True)
    availability = Column(String(30), nullable=True)
    dedup_group_id = Column(Integer, nullable=True, index=True)
    query_normalized = Column(String(500), nullable=True, index=True)
    embedding = Column(Text, nullable=True)              # JSON list of floats
    retrieved_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=True)
    extracted_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §8.1  SearchCache — semantic external cache in front of SerpAPI.
# ---------------------------------------------------------------------------

class SearchCache(Base):
    __tablename__ = "search_cache"

    id = Column(Integer, primary_key=True)
    query_text = Column(Text, nullable=False)
    query_normalized = Column(String(500), nullable=False, index=True)
    embedding = Column(Text, nullable=True)              # JSON list of floats
    requirements_json = Column(Text, nullable=True)      # stored requirements
    results_json = Column(Text, nullable=False)          # normalized candidates
    source = Column(String(50), nullable=False, default="serpapi")
    quality_score = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=utcnow, index=True)
    expires_at = Column(DateTime, nullable=False)


# ---------------------------------------------------------------------------
# §2.3  CartItem — "merchant" rows (catalog SKUs) or "reference" rows (SERP
# EXT-xxx listings). Both are payable and flow through quote/checkout.
# ---------------------------------------------------------------------------

class CartItem(Base):
    __tablename__ = "cart_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False, index=True)
    item_type = Column(String(20), nullable=False)  # "merchant" | "reference"
    ref_id = Column(String(100), nullable=False)    # merchant SKU or EXT-xxx
    name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    source = Column(String(50), nullable=True)
    added_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §2.4  Order / OrderItem — multi-item, snapshotted from the paid quote cart.
# ---------------------------------------------------------------------------

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    razorpay_order_id = Column(String(100), nullable=True, index=True)
    razorpay_payment_link_id = Column(String(100), nullable=True, index=True)
    payment_short_url = Column(String(200), nullable=True)
    actor = Column(String(50), nullable=False)
    session_id = Column(String(100), nullable=False, index=True)
    subtotal = Column(Float, nullable=False)
    total = Column(Float, nullable=False)
    currency = Column(String(3), nullable=False, default="INR")
    cart_hash = Column(String(64), nullable=True, index=True)
    quote_id = Column(String(64), nullable=True, index=True)  # approved snapshot (Rev 3)
    status = Column(String(30), nullable=False, default="created")
    mandate_signature = Column(String(200), nullable=True)  # HMAC proof (Rev 3)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    source = Column(String(50), nullable=True)
    ref_id = Column(String(100), nullable=True)

    order = relationship("Order", back_populates="items")


# ---------------------------------------------------------------------------
# §14  Quote — immutable backend-owned snapshot bound to cart version + hash.
# ---------------------------------------------------------------------------

class Quote(Base):
    __tablename__ = "quotes"

    id = Column(Integer, primary_key=True)
    quote_id = Column(String(32), unique=True, nullable=False, index=True)
    session_id = Column(String(100), nullable=False, index=True)
    actor = Column(String(50), nullable=False)
    cart_version = Column(Integer, nullable=False, default=1)
    cart_hash = Column(String(64), nullable=False)
    amount = Column(String(32), nullable=False)       # exact Decimal, e.g. "4990.00"
    currency = Column(String(3), nullable=False, default="INR")
    status = Column(String(20), nullable=False, default="active")
    nonce = Column(String(32), nullable=False)        # approval token
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=False)


# ---------------------------------------------------------------------------
# §12.4  OfferEvent — Next Best Offer feedback loop.
# ---------------------------------------------------------------------------

class OfferEvent(Base):
    __tablename__ = "offer_events"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    base_sku = Column(String(50), nullable=False)
    offer_sku = Column(String(50), nullable=False)
    shown = Column(Boolean, nullable=False, default=True)
    accepted = Column(Boolean, nullable=False, default=False)
    accept_probability = Column(Float, nullable=True)
    expected_incremental_revenue = Column(Float, nullable=True)
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §4 / HLD §3.5  SessionState — compact durable state, not raw history.
# ---------------------------------------------------------------------------

class SessionState(Base):
    __tablename__ = "session_state"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), unique=True, nullable=False, index=True)
    actor = Column(String(50), nullable=False)
    state_json = Column(Text, nullable=False, default="{}")
    cart_version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# §2.5  CartEvent — growth analytics breadcrumbs.
# ---------------------------------------------------------------------------

class CartEvent(Base):
    __tablename__ = "cart_events"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    actor = Column(String(50), nullable=False)
    ref_id = Column(String(100), nullable=True)
    event_type = Column(String(30), nullable=False)
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# §2.5/§25  AuditLog — decision ledger with trace + timing envelope.
# ---------------------------------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    actor = Column(String(50), nullable=False)
    event_type = Column(String(50), nullable=False, index=True)
    tool_name = Column(String(50), nullable=True)
    parameters_json = Column(Text, nullable=True)
    agent_reasoning = Column(Text, nullable=True)
    decision = Column(String(20), nullable=True)
    reason = Column(Text, nullable=True)
    outcome = Column(String(20), nullable=False)
    error_detail = Column(Text, nullable=True)
    trace_id = Column(String(32), nullable=True, index=True)
    stage = Column(String(50), nullable=True)
    latency_ms = Column(Float, nullable=True)
    source = Column(String(50), nullable=True)
    cache_hit = Column(Boolean, nullable=True)
    confidence = Column(Float, nullable=True)
    tier_used = Column(Integer, nullable=True)              # cascade tier (Rev 3)
    mandate_signature = Column(String(200), nullable=True)  # HMAC proof (Rev 3)
    created_at = Column(DateTime, default=utcnow, index=True)