"""
Runtime configuration for GrowthMate (Revision 3 — target architecture).

Every tunable knob from LLD §28 and the personality thresholds from the routing
rules live here, driven by environment variables with safe defaults. No secrets
in this module — secrets stay in `.env` and are read where they are used.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Routing confidence gates (LLD §5.3 / prompt routing_rules):
#   >= ROUTER_CONFIDENCE_FAST_PATH -> deterministic fast path, no LLM
#   [  ROUTER_CONFIDENCE_LLM_PATH, ROUTER_CONFIDENCE_FAST_PATH) -> one structured LLM call
#   <  ROUTER_CONFIDENCE_LLM_PATH  -> clarify or stronger reasoning
ROUTER_CONFIDENCE_FAST_PATH = float(os.getenv("ROUTER_CONFIDENCE_FAST_PATH", "0.90"))
ROUTER_CONFIDENCE_LLM_PATH = float(os.getenv("ROUTER_CONFIDENCE_LLM_PATH", "0.70"))

# Semantic external cache (LLD §8). Never a source of truth for merchant
# pricing: cached results are market intelligence only.
SEMANTIC_CACHE_TTL_SECONDS = float(os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "900"))
SEMANTIC_CACHE_SIMILARITY_THRESHOLD = float(
    os.getenv("SEMANTIC_CACHE_SIMILARITY_THRESHOLD", "0.62")
)

# Cascade router (Rev 3, Phase 1). A latency optimizer, never a money
# authority: fast paths are restricted to safe cart/affordance actions and
# payment-adjacent phrases always fail open into the full pipeline.
# CASCADE_ENABLED=false reproduces Rev-2 exactly (every turn falls through).
CASCADE_ENABLED = os.getenv("CASCADE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
CASCADE_TIER1_THRESHOLD = float(os.getenv("CASCADE_TIER1_THRESHOLD", "0.70"))
CASCADE_TIER2_THRESHOLD = float(os.getenv("CASCADE_TIER2_THRESHOLD", "0.80"))

# SerpAPI adapter (LLD §9): async, timeout materially shorter than the old
# blocking 15s path, provider errors normalized.
SERPAPI_TIMEOUT_SECONDS = float(os.getenv("SERPAPI_TIMEOUT_SECONDS", "8.0"))

# Next Best Offer Engine (LLD §12.3).
MAX_OFFERS_PER_TURN = int(os.getenv("MAX_OFFERS_PER_TURN", "1"))
MAX_ADDON_RATIO = float(os.getenv("MAX_ADDON_RATIO", "0.20"))
MIN_OFFER_CONFIDENCE = float(os.getenv("MIN_OFFER_CONFIDENCE", "0.55"))
SMOOTHED_PRIOR_ALPHA = float(os.getenv("OFFER_PRIOR_ALPHA", "1.0"))
SMOOTHED_PRIOR_BETA = float(os.getenv("OFFER_PRIOR_BETA", "1.0"))
DEFAULT_ACCEPT_PROBABILITY = float(os.getenv("OFFER_DEFAULT_ACCEPT_PROBABILITY", "0.35"))
MARGIN_PCT = float(os.getenv("MARGIN_PCT", "0.30"))
STOCK_CONFIDENCE_THRESHOLD = int(os.getenv("STOCK_CONFIDENCE_THRESHOLD", "10"))

# Growth-recovery agent (Rev 3, Phase 6): deterministic quantity-fit proposals
# only. Floor guards micro-transactions — any recovered total below it declines.
MIN_RECOVERY_AMOUNT = float(os.getenv("MIN_RECOVERY_AMOUNT", "100.0"))

# Quote service (LLD §14): immutable snapshot tied to cart version + hash.
QUOTE_TTL_SECONDS = int(os.getenv("QUOTE_TTL_SECONDS", "900"))

# Lightweight local embeddings (placeholder for pgvector later). Deterministic
# hashing-trick vectors; dimension is a config knob so it moves without code churn.
EMBEDDING_HASH_DIM = int(os.getenv("EMBEDDING_HASH_DIM", "512"))

# Catalog RAG index (Rev 3, Phase 2): deterministic on-disk k-NN index over
# merchant SKUs, rebuilt offline/post-seed. Path defaults next to the SQLite DB.
CATALOG_INDEX_PATH = os.getenv("CATALOG_INDEX_PATH", "growthmate.catalog_index.json")
CATALOG_INDEX_MIN_SCORE = float(os.getenv("CATALOG_INDEX_MIN_SCORE", "0.10"))

# Merchant capture scoring weights (LLD §11.1). Sums to ~1.0 for the defaults.
CAPTURE_W_SEMANTIC = float(os.getenv("CAPTURE_W_SEMANTIC", "0.30"))
CAPTURE_W_REQUIREMENT = float(os.getenv("CAPTURE_W_REQUIREMENT", "0.20"))
CAPTURE_W_PRICE = float(os.getenv("CAPTURE_W_PRICE", "0.15"))
CAPTURE_W_STOCK = float(os.getenv("CAPTURE_W_STOCK", "0.10"))
CAPTURE_W_MARGIN = float(os.getenv("CAPTURE_W_MARGIN", "0.10"))
CAPTURE_W_ATTACH = float(os.getenv("CAPTURE_W_ATTACH", "0.10"))
CAPTURE_W_PRIORITY = float(os.getenv("CAPTURE_W_PRIORITY", "0.05"))

# PII redaction (minimal email/phone/card/secret patterns, ARCHITECTURE §16).
PII_REDACT_TOKEN = os.getenv("PII_REDACT_TOKEN", "[REDACTED]")