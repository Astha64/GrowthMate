"""
Pydantic request/response models for GrowthMate's REST API (Revision 3).

Matches LOW_LEVEL_DESIGN.md §3 (REST API Contract) plus the agent-commerce
APIs (LLD §22-§25): discovery, quote, checkout, order status, and metrics.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str


class ProductOut(BaseModel):
    sku: str
    name: str
    description: Optional[str] = None
    price: float
    stock: int
    category: Optional[str] = None


class CatalogResponse(BaseModel):
    currency: str
    products: List[ProductOut]


class ChatRequest(BaseModel):
    session_id: str
    actor: str = Field(pattern="^(human|buyer_agent)$")
    message: str
    # Optional prior conversation turns, as [{"role": ..., "content": ...}],
    # used to reconstruct multi-turn agent state across /chat invocations
    # (LLD §3 — "optional history"). Defaults to empty.
    history: Optional[List[dict]] = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls_made: List[str] = []
    blocked: bool = False
    trace_id: Optional[str] = None


class AuditLogOut(BaseModel):
    id: int
    session_id: str
    actor: str
    event_type: str
    tool_name: Optional[str] = None
    parameters_json: Optional[str] = None
    agent_reasoning: Optional[str] = None
    decision: Optional[str] = None
    reason: Optional[str] = None
    outcome: str
    error_detail: Optional[str] = None
    created_at: str


class WebhookResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Agent-commerce APIs (LLD §22-§25).
# ---------------------------------------------------------------------------

class AgentCommerceManifest(BaseModel):
    schema_version: str = "1.0"
    name: str
    description: str
    currencies: List[str]
    operations: List[dict]
    capabilities: dict


class DiscoverRequest(BaseModel):
    session_id: str
    actor: str = Field(pattern="^(human|buyer_agent)$")
    query: str
    budget: Optional[float] = None
    features: Optional[List[str]] = None


class DiscoverCandidate(BaseModel):
    name: str
    price: float
    currency: str
    source: str
    payable: bool  # True for both merchant and external (EXT-xxx) candidates
    sku: Optional[str] = None  # EXT-xxx for external listings, merchant SKU for captures
    merchant_sku: Optional[str] = None
    why: Optional[str] = None


class DiscoverResponse(BaseModel):
    query: str
    count: int
    cache_hit: bool = False
    candidates: List[DiscoverCandidate]
    merchant_match: Optional[dict] = None
    merchant_candidates: List[dict] = []  # ranked, payable shortlist for automated buyers
    trace_id: Optional[str] = None


class AgentCartItem(BaseModel):
    sku: str
    quantity: int = Field(ge=1, le=100)
    name: Optional[str] = None   # required to add an external (EXT-xxx) reference item
    price: Optional[float] = None  # required for an external (EXT-xxx) reference item
    source: Optional[str] = None


class QuoteRequest(BaseModel):
    session_id: str
    actor: str = Field(pattern="^(human|buyer_agent)$")
    cart: List[AgentCartItem]


class QuoteResponse(BaseModel):
    quote_id: str
    session_id: str
    actor: str
    amount: str
    currency: str
    cart_hash: str
    cart_version: int
    nonce: str
    status: str
    expires_at: str
    items: List[dict]
    trace_id: Optional[str] = None


class CheckoutRequest(BaseModel):
    session_id: str
    actor: str = Field(pattern="^(human|buyer_agent)$")
    quote_id: str
    nonce: str


class CheckoutResponse(BaseModel):
    blocked: bool = False
    reason: Optional[str] = None
    order_id: Optional[int] = None
    payment_link: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    trace_id: Optional[str] = None


class RecoveryRequest(BaseModel):
    session_id: str
    actor: str = Field(pattern="^(human|buyer_agent)$")  # who gets recovered


class RecoveryResponse(BaseModel):
    eligible: bool = False
    reason: str = "not eligible"
    sku: Optional[str] = None
    name: Optional[str] = None
    from_quantity: int = 0
    to_quantity: int = 0
    old_total: str = "0.00"
    new_total: str = "0.00"
    limit: float = 0.0
    trace_id: Optional[str] = None


class OrderOut(BaseModel):
    order_id: int
    status: str
    subtotal: str
    total: str
    currency: str
    cart_hash: Optional[str] = None
    items: List[dict] = []
    razorpay_payment_link_id: Optional[str] = None
    created_at: Optional[str] = None


class MetricsOut(BaseModel):
    entries: dict
    total_requests: int


class LatencyEntry(BaseModel):
    stage: str
    count: int
    p50: float
    p95: float
    avg: float