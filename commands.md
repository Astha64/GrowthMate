# commands.md — GrowthMate run & agent commands

Everything needed to run the pipeline end-to-end and to talk to the GrowthMate
sales agent (chat UI) or an external buyer agent (REST API). Keep this file
open while demoing.

---

## 1. Run the pipeline end-to-end

```bash
# 1) Create + activate a virtualenv (do once)
python -m venv venv
source venv/bin/activate

# 2) Install deps (Razorpay pulls pkg_resources, so setuptools<81 is pinned)
pip install -r requirements.txt

# 3) Prepare environment (do once)
cp .env.example .env
#    Fill in .env: GEMINI_API_KEY, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
#    MANDATE_SECRET (required to boot). Everything else has a sane default.

# 4) Start the API (fresh DB starts with an EMPTY merchant catalog — products
#    are added at runtime from SERP discovery as EXT-xxx items; no seeding)
uvicorn app.main:app --reload

# 5) Verify it is up
curl http://127.0.0.1:8000/health
#    -> {"status":"ok",...}

# 6) Open the frontend chat (optional, needs a browser)
open frontend/index.html
```

### Testing

```bash
# Full suite (cascade on)
pytest

# Rev-2 parity mode (cascade disabled) — every turn falls through to the pipeline
CASCADE_ENABLED=false pytest

# Just the guardrail pure-function tests (no DB/network)
pytest tests/test_guardrail.py

# New suggestion-selection e2e tests
pytest tests/test_suggestions_e2e.py
```

### External buyer-agent demo (separate process, HTTP-only)

```bash
# After the API is running (no seeded DB required):
python buyer_agent.py
```

### Reset a schema change (no migrations exist)

```bash
rm -f growthmate.db growthmate.catalog_index.json   # index rebuilds at startup
```

---

## 2. Talking to the sales agent (chat UI)

`POST /chat` with `{session_id, actor, message}` — `actor` is `human` or
`buyer_agent`. The reply is plain text; every money action is an expected
HTTP 200 with `blocked: true` when refused.

### 2.1 Merchant-SKU cheat sheet (only if a catalog is inserted manually)

A fresh DB has an **empty merchant catalog** — all products come from SERP
discovery (`EXT-xxx`). The SKUs below are what the Rev-2 fixtures/tests seed;
they exist in the app only if a catalog is loaded into the `products` table
manually (e.g. via the tests' `_seed` helpers).

| SKU       | Name                | Price  | Category    |
|-----------|---------------------|--------|-------------|
| APP-001   | Cotton Crew T-Shirt | ₹499   | apparel     |
| APP-002   | Chinos              | ₹1499  | apparel     |
| ACC-001   | Leather Wallet      | ₹1299  | accessories |
| ELEC-001  | Wireless Earbuds    | ₹2499  | electronics |
| ELEC-002  | USB-C Fast Charger  | ₹1199  | electronics |
| HOME-001  | Ceramic Coffee Mug  | ₹199   | home        |
| HOME-002  | Desk Lamp           | ₹899   | home        |
| SHOE-001  | Nike Revolution 6   | ₹1899  | footwear    |
| SHOE-002  | Adidas Duramo SL    | ₹2499  | footwear    |
| SHOE-003  | Casual Sneakers     | ₹2199  | footwear    |

`GET /catalog` returns the live merchant list. These catalog SKUs are always
payable. On top of those, **any product returned by the SERP/market discovery
can also be added to the cart and paid** — those appear as `EXT-001`,
`EXT-002`, ... options.

### 2.2 Cheat sheet of agent commands

| You say | What happens |
|---|---|
| `help` | Agent lists what it can do |
| `show catalog` / `catalog` | Lists a taste of the catalog with SKUs |
| `wireless earbuds` / `search running shoes under 4000` | Product discovery — ranked 2-3 selectable suggestions (`1)` `2)` `3)`) with the merchant match first + related items |
| `1` / `2` / `first one` / `option 2` / `second` | Picks that suggestion and adds it to your cart |
| `add SHOE-001` | Adds one merchant SKU to the cart (repeat the command to bump quantity) |
| `add EXT-001` | Adds a market/SERP listing (EXT-xxx) to the cart — same checkout flow |
| `yes` / `add it` | Confirms a suggested/catalog item the agent offered to add |
| `remove SHOE-001` | Removes a SKU |
| `clear my cart` | Empties the cart |
| `show my cart` / `my cart` | Shows cart contents |
| `what's my total` / `cart total` | Shows the cart total |
| `checkout` | Shows the payable preview (itemized, quote id, expiry) |
| `Yes, proceed` / `approve` | Final approval — creates the Razorpay payment link. **Only after you've seen the preview.** |
| `no` / `adjust cart` | Backs out of the preview to edit the cart |

### 2.3 When there is NO direct merchant match

Say something the catalog does not stock, e.g. `wireless mouse`. The agent
runs external/SERP discovery and shows the top market listings as **selectable
`EXT-xxx` options** — every one of them can be added to the cart and paid
(Rev 3; external matches are no longer reference-only).

Example:

```
We don't stock a direct match, so here's what the market shows — you can add any of these to your cart:
  1) Logitech G Pro Wireless Gaming Mouse (EXT-001) — ₹2500.0 (INR)
  2) ...
Reply "1", "2" or "3" (or say "add EXT-001") to add it to your cart, then say "checkout" when ready.
```

Reply `1` / `2` / `3` (or `add EXT-001`) to add the market listing, then
`checkout` → `Yes, proceed` exactly like a catalog item. Market listings are
still fully guardrailed (approval + spend limits) and audited.

### 2.4 Full happy-path transcript (paste-able)

```
1. wireless earbuds
   -> ranked suggestions (1) ELEC-001 ... 2) ELEC-002 ...)
2. 1              (adds ELEC-001; related items are offered as suggestions too)
3. show my cart   (confirm cart contents)
4. checkout       (shows payable preview with quote id)
5. Yes, proceed   (payment link created — complete on Razorpay)
```

### 2.5 Sample agent-discovery prompt (for an AI sales agent)

> "I am looking for wireless earbuds within ₹2500. Search the catalog, show me
> the top fixes with merchant SKUs, add the best match to my cart, and prepare
> checkout."

Deterministic equivalent, one message at a time:

```
POST /chat {"session_id": "sess-demo", "actor": "human", "message": "wireless earbuds under 2500"}
POST /chat {"session_id": "sess-demo", "actor": "human", "message": "1"}
POST /chat {"session_id": "sess-demo", "actor": "human", "message": "checkout"}
POST /chat {"session_id": "sess-demo", "actor": "human", "message": "Yes, proceed"}
```

---

## 3. Buyer-agent REST API (external AI buyer)

Capability discovery and order feeds:

```bash
curl http://127.0.0.1:8000/.well-known/agent-commerce.json
curl http://127.0.0.1:8000/.well-known/agentic-catalog.json
curl http://127.0.0.1:8000/.well-known/agentic-orders.json?session_id=sess-buyer-demo
```

Discover → quote → checkout:

```bash
curl -X POST http://127.0.0.1:8000/agent/discover \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "sess-buyer-demo", "actor": "buyer_agent", "query": "wireless earbuds", "budget": 2500}'

curl -X POST http://127.0.0.1:8000/agent/quote \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "sess-buyer-demo", "actor": "buyer_agent", "cart": [{"sku": "ELEC-001", "quantity": 1}]}'
#   -> returns quote_id + nonce + cart_hash

# A market (EXT-xxx) listing from /agent/discover is quoted the same way —
# carry its name + price from the discover response:
curl -X POST http://127.0.0.1:8000/agent/quote \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "sess-buyer-demo", "actor": "buyer_agent", "cart": [{"sku": "EXT-001", "name": "Logitech G Pro Wireless Mouse", "price": 2500.0, "quantity": 1}]}'

curl -X POST http://127.0.0.1:8000/agent/checkout \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "sess-buyer-demo", "actor": "buyer_agent", "quote_id": "<from quote>", "nonce": "<from quote>"}'
#   -> blocked:false + payment_link when within limits

curl http://127.0.0.1:8000/agent/order/{order_id}
```

Over-limit remediation (quantity recovery):

```bash
curl -X POST http://127.0.0.1:8000/agent/recovery \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "sess-buyer-demo", "actor": "buyer_agent"}'
```

Operational endpoints:

```bash
curl "http://127.0.0.1:8000/audit?session_id=sess-demo"   # audit trail
curl http://127.0.0.1:8000/metrics/latency                 # stage latency
```

---

## 4. Spend limits (guardrail)

| actor       | per transaction | per session |
|-------------|-----------------|-------------|
| `human`     | ₹5,000          | ₹15,000     |
| `buyer_agent`| ₹3,000         | ₹5,000      |

An over-limit payment returns HTTP 200 with `blocked: true` (expected control
flow, not an error) and creates no Order row.

---

## 5. Webhook (payment confirmation, optional)

```bash
curl -X POST http://127.0.0.1:8000/webhook/razorpay \
  -H 'Content-Type: application/json' \
  -H 'X-Razorpay-Signature: <hmac>' \
  -d '{"event": "payment_link.paid", "payload": {...}}'
```

---

## 6. Quick reference (everything in 10 lines)

```bash
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill keys + MANDATE_SECRET
uvicorn app.main:app --reload
curl http://127.0.0.1:8000/health
pytest
python buyer_agent.py
```

Then drive the chat: search → pick (`1`/`2`/`3`) → `show my cart` →
`checkout` → `Yes, proceed`.