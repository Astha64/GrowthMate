# Phase Notes — Revision 3 implementation (deviations from the BUILD PROMPT)

This file records every deliberate deviation between the build prompt's
target-architecture description and what is actually implemented, so the diff
is reviewable at a glance. The safety invariant holds throughout: **payment is
only ever executed by deterministic Python on an ALLOW decision** —
`execute_payment` is reachable solely from the guardrail-approved path, and the
cascade fast paths are restricted to cart/affordance affordances.

Status: all six phases implemented; full suite green.
`CASCADE_ENABLED=false` reproduces Rev-2 exactly (every turn falls through).

## Phase 1 — Cascade router

- **As specified:** a latency-optimizer forward pass with fast tiers.
- **Deviation:** fast-path reply texts do NOT bit-match what the RM/prompt
  "should have said" once the specific conversation state is applied
  (approval-negation replies carry the exact approval text). The RM's parity
  fixtures therefore cover *behavioral* parity (deterministic action lines
  render identical), not byte-for-byte reply equality — a byte-equality harness
  would be testing the RM's phrasing, not the software.
- `payment guard → greeting/thanks/goodbye` ordering is placement-only:
  `payment_guard` returns "n/a" when there is nothing to approve, so the guard
  runs on every message with zero behavioral cost. Reordering moves no money logic.
- **Money-safety invariant:** `route()` emits only `SAFE_ACTIONS` and
  `SAFE_TOOL_KINDS`; payment-adjacent phrases always fail open into the full
  pipeline. Tested by `tests/test_cascade_never_fastpaths_payment.py`.

## Phase 2 — Catalog RAG index

- **As specified.** `growthmate.catalog_index.json` is a flat on-disk k-NN index
  (deterministic hashing embeddings), rebuilt at startup/post-seed, atomic
  writes, content-addressed rebuilds, fail-open reads. `backfill_embedding_columns`
  mirrors factory-correct embeddings into `products.embedding` for direct DB readers.

## Phase 3 — Semantic cache wrapper

- **As specified**, extended with `normalize_requirements`,
  `cached_lookup` (discovery-result shape with `cache_hit`/`source`/
  `similarity`/`count`/`recommendations`), `store` (refuses empty),
  `prune_expired`, and a `min_similarity` override. `discovery` and the cascade
  Tier-2 short-circuit both consume it.

## Phase 4 — HMAC mandates

- **As specified** — `MANDATE_SECRET` fail-fast, ALLOW-only minting on both
  money paths, `mandate_signature` on Order and audit rows, NULL on every BLOCK.
- **Deviation:** the order feed and `verify_order_mandate` require a
  `created_at`-stable `quote_id`; `Order.quote_id` was added so the 5-field
  binding (session|actor|cart_hash|amount|quote_id) verifies end-to-end. This
  requires a fresh DB (delete `growthmate.db`, re-run seed).
- `.env.example` (which AGENTS.md referenced but did not exist) was created and
  now lists `MANDATE_SECRET` and the `CASCADE_*`/catalog-index knobs.

## Phase 5 — Agent-readable feeds

- **As specified:** `/,well-known/agentic-catalog.json` served from the Phase-2
  index rows (last_built_at checkpointed from the sessions table) and
  `/,well-known/agentic-orders.json` (every read re-verifies each order's HMAC
  mandate; tampered rows surface as `mandate_verified:false`, never a 5xx).
- **Deviation:** feed entries carry only fields that exist on catalog rows —
  `brand`/`price_basis`/`stock_units` from the prompt do NOT exist on the
  merchant `Product` model, and the feed must not invent data.

## Phase 6 — Growth-recovery agent

- **Deviation from the prompt's "discount" framing:** recovery here is a
  deterministic *quantity-fit* proposal (reduce the dominant SKU to the
  `floor(limit/price)` ceiling) — never a price discount, because a discount
  would silently diverge from the immutable quote's product price. The recovery
  NEVER mutates the cart, NEVER executes payment, and is bounded below by
  `MIN_RECOVERY_AMOUNT`. Audited under actor `system_growth_agent` with
  event_type `recovery_offer`.

## Rev-3 deviation note — external (SERP) products are payable

The original design treated merchant-catalog SKUs as the *only* payable items
and external/SERP listings as reference-only comparison. **Rev-3 removes that
restriction:** every product returned by external discovery can now be added to
the cart and paid.

- Chat path: a no-merchant-match query surfaces the top market listings as
  selectable `EXT-001`/`EXT-002`/`EXT-003` suggestions (built by
  `orchestration._reference_suggestions`), which numbered picks and
  `add EXT-xxx` resolve via `commerce.add_reference_to_cart`. Cross-sell market
  complements are addable the same way.
- REST agent path: `/agent/discover` marks external candidates `payable=true`
  with an `EXT-xxx sku`; `/agent/quote` accepts an external item when the cart
  entry carries the discovery-issued `name`+`price`.
- The catalog remains fully payable as before. **Nothing weakened:** every
  external add still flows through the same approval (show-preview →
  `validate_approval`) and deterministic guardrail spend limits, and is
  audited. `add_reference_to_cart` is a plain deterministic cart mutation with
  no merchant stock ceiling (a SERP listing defines its own price/stock).
- Money-safety invariant unchanged: `execute_payment` remains reachable only
  from the guardrail-approved branch; fast paths cannot add reference items
  (they only surface them for selection).

## seed_data.py removed — SERP is the default product path

- `seed_data.py` (the hardcoded 10-SKU merchant catalog) was **deleted**.
  A fresh DB boots with an empty `products` table; every product arrives at
  runtime from the SERP/offline-mock discovery and is added to the cart as an
  addable `EXT-xxx` reference item. No seed step in run instructions.
- The merchant-catalog machinery (`merchant_capture`, cross-sell adjacency,
  catalog RAG index, catalog feed, offer engine) is **kept but dormant** —
  it lights up only if a catalog is inserted into `products` manually
  (tests seed their own rows via fixtures).
- `buyer_agent.py` was reworked to demo purely off SERP candidates: Journey A
  quotes the top payable `EXT-xxx` market listing by name/price; Journey B
  quotes the same listing at `qty = floor(3000/price)+1` so the total still
  exceeds the ₹3000 per-transaction limit and blocks with HTTP 200.

## Config additions (all env-driven, `.env.example`)

`CASCADE_ENABLED`, `CASCADE_TIER1_THRESHOLD`, `CASCADE_TIER2_THRESHOLD`,
`CATALOG_INDEX_PATH`, `CATALOG_INDEX_MIN_SCORE`, `MIN_RECOVERY_AMOUNT`,
`MANDATE_SECRET`.

## Test footprint

- New suites: `test_cascade_router`, `test_cascade_never_fastpaths_payment`,
  `test_catalog_index`, `test_semantic_cache` (extended), `test_mandate`,
  `test_agent_feed`, `test_growth_agent`.
- Enabled: 225 passed. Disabled parity: 218 passed + 7 skipped via the
  `requires_cascade` marker. No network or real keys required.