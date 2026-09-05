# GrowthMate — AI Growth & Agentic Commerce Agent

A merchant-side agent on **Razorpay test-mode APIs**, transactable by both a
human (chat UI) and an autonomous external AI buyer agent (`buyer_agent.py`),
with every money-moving action gated by a **deterministic guardrail** and
logged to a queryable **audit trail**.

**The bar (problem statement):** every money action explainable, bounded, and
gated. Show the audit trail and one failure handled gracefully.

---

## Setup

```bash
python -m venv venv
source venv/bin/activate                 # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                     # then fill in real keys (below)
uvicorn app.main:app --reload
```

A fresh DB starts with an **empty merchant catalog** — the agent adds products
directly from SERP discovery at runtime (every market listing is payable as an
`EXT-xxx` item). No seed script; `seed_data.py` no longer exists.

Visit http://127.0.0.1:8000/health — should return:
```json
{"status": "ok", "service": "growthmate-backend"}
```

Interactive API docs: http://127.0.0.1:8000/docs

Chat UI: http://127.0.0.1:8000/static/index.html
Audit viewer: http://127.0.0.1:8000/static/audit.html

### Environment variables (`.env`)

| Key | Purpose |
|---|---|
| `GEMINI_API_KEY` | Optional single structured requirement-extraction call in the router (Google Gemini) |
| `RAZORPAY_KEY_ID` | Razorpay test-mode public key |
| `RAZORPAY_KEY_SECRET` | Razorpay test-mode secret (webhook HMAC too) |
| `DATABASE_URL` | SQLite URL (default `sqlite:///./growthmate.db`) |
| `SERPAPI_KEY` | Optional — live external discovery; without it `discovery.py` uses the offline mock catalog |
| `BUYER_BASE_URL` | base URL `buyer_agent.py` talks to (default `http://127.0.0.1:8000`) |
| `BUYER_SESSION_ID` | session id the demo buyer agent uses (default `sess-buyer-agent-api`) |

Never commit `.env`. Secrets live only in `.env` (see `.env.example`).

---

## Architecture

Read the design docs, in order — they capture original intent but are **out of
sync** with the code (the code is the source of truth):

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — integrations, agent orchestration shape
- [`docs/HIGH_LEVEL_DESIGN.md`](docs/HIGH_LEVEL_DESIGN.md) — module boundaries, data flow, NFRs
- [`docs/LOW_LEVEL_DESIGN.md`](docs/LOW_LEVEL_DESIGN.md) — schemas, API contracts, tool JSON schemas,
  guardrail rules, LangGraph node signatures, sequence diagrams (§11 addendum resolves ambiguities)

### How it works

Orchestration is a **single deterministic forward pass** through a five-step
LangGraph pipeline built in `app/orchestration.py` (nodes `agent → discover →
tool → approval → guardrail → pay → respond → audit`). No agent↔tool loop, no
LLM deciding which tool to call next.

```
Human (chat UI) ─┐
                 ├─► POST /chat ─► forward pass ─► reply
buyer_agent.py ──┘    POST /agent/discover|quote|checkout (same policy plane)

  agent      deterministic router + (optional, confidence-gated) requirements extraction
  discover   merchant capture + cached/live external discovery + one offer
  tool       deterministic cart/quote/checkout actions (no LLM)
  approval   explicit "yes" against the shown checkout preview (fail-closed)
  guardrail  quote↔cart hash/version integrity + actor spend limits (fail-closed)
  pay        Razorpay ONLY if every precondition passed
  respond    reply composition + compact durable session state
  audit      per-turn AuditLog row (trace_id + stage + latency)
```

- **Money decisions are plain deterministic Python** in `app/guardrail.py`:
  `validate_approval` (explicit confirmation of a *shown* quote), then
  `validate_quote_against_cart` (SHA-256 cart hash + version, unexpired),
  then `check_transaction` (per-transaction/per-session spend limits for
  `human` ₹5000/₹15000 and `buyer_agent` ₹3000/₹5000). Blocks are expected
  control flow: HTTP 200 with `"blocked": true`, no order row, no Razorpay call.
- **Quotes are immutable snapshots** (`app/quote.py`): backend-owned, hash-bound
  to the exact cart contents, invalidated on any later cart mutation.
- **Every** money/cart action lands in `AuditLog` with a trace_id — approvals,
  blocks, and successes each get their own row.
- `buyer_agent.py` is a standalone script using `requests` only; it never imports
  from `app/`.

---

## Demo journeys

### Journey A — normal human purchase (pure SERP)

With an empty merchant catalog (default), every product comes from **SERP
discovery**:

1. Open the chat UI (http://127.0.0.1:8000/static/index.html).
2. Ask: *"I need running shoes under 2000."* — the agent searches the market
   and lists the top results as selectable `EXT-xxx` options; reply `1`, `2` or
   `3` (or *"add EXT-001"*).
3. Say *"checkout"* to see the backend-owned quote preview, then *"yes"* to
   approve that exact amount.
4. You receive a real Razorpay **test** payment link.
5. Check the audit viewer — the row shows `ALLOW` / `success`.

(If a merchant catalog was inserted manually, catalog queries surface payable
merchant SKUs on top of the market listings.)

### Journey B — engineered failure (buyer agent exceeds its limit)

`buyer_agent.py` quotes the SERP market listing at a quantity whose total
exceeds the `buyer_agent` per-transaction limit of **₹3000** (no seeded catalog
needed).

```bash
python buyer_agent.py
```

Expected output includes `Journey B blocked: True` and a clean structured
refusal (HTTP 200 with `"blocked": true`) — **not** a 4xx/5xx or an exception.
The audit viewer shows the `BLOCK` row with reason
`exceeds per-transaction limit of ₹3000`, and **no Order row / no Razorpay call**
was made.

---

## Tests

```bash
pytest
```

Guardrail tests are pure-function (no DB/network). Everything else uses an
in-memory SQLite DB and monkeypatches the Razorpay wrapper and external
discovery — no real API keys or network required.

---

## Deployment (Render)

No `Procfile` — this project deploys on **Render** (LLD §11.10) using a
dashboard-configured start command:

```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Settings in the Render dashboard:
- **Runtime:** Python 3 (see `runtime.txt`)
- **Start command:** the line above
- **Environment:** set `GEMINI_API_KEY`, `RAZORPAY_KEY_ID`,
  `RAZORPAY_KEY_SECRET`, `DATABASE_URL` on the service
- A persistent disk is recommended so the SQLite file survives restarts
