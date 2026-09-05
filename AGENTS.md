# AGENTS.md — GrowthMate

## What this project is
AI Growth & Agentic Commerce hackathon submission. A merchant agent on Razorpay
test-mode APIs, transactable by both a human (chat UI) and an external AI buyer
agent (`buyer_agent.py`), with every money-moving action gated by deterministic
guardrail checks and every tool call logged to an audit trail. Deadline-driven —
prefer working over elegant.

## Docs (read, but they are out of sync with the code)
The docs capture original design intent but predate the current implementation
and are NOT a reliable spec. Concretely, LLD §19–§21 describe `/agent/*`
endpoints and a router/classifier LangGraph that don't exist — the code is a
single deterministic forward-pass LangGraph (`app/orchestration.py`) with a
different tool set, and code comments cite "LLD §…" numbers that don't map to
the checked-in LLD's sections. Read the docs for intent; treat
`orchestration.py`, `guardrail.py`, `quote.py`, `commerce.py`, `models.py`, and
`tests/` as the source of truth.

Do not rename established names independently: endpoint paths, tool names, DB
columns, LangGraph node names, and the audit `event_type` taxonomy are pinned
by the code, tests, and frontend — renaming breaks the suite and the viewer.

Note: README used to link `docs/BUILD_PROMPT.md`, which does not exist.

## Non-negotiable rules
- Money decisions are plain deterministic Python in `app/guardrail.py`. Three
  independent checks gate payment: `validate_approval` (explicit confirmation
  of a *shown* checkout preview/quote, fail-closed), `validate_quote_against_cart`
  (quote hash+version integrity, unexpired, fail-closed), and `check_transaction`
  (spend limits). Never let the LLM decide whether a payment executes;
  `validate_approval` is **not** an LLM-callable tool.
- Every money/cart action lands in `AuditLog` with a trace_id. `audit_node`
  writes the per-turn `chat_turn` row (decision ALLOW/BLOCK); `approval_node`
  and `guardrail_node` write nothing themselves; agent endpoints write their own
  `discovery`/`quote_created`/`payment`/`guardrail_decision` rows. Cart
  mutations log `cart_*` rows via `_log_cart_event`.
- Orchestration is a LangGraph `StateGraph` in `app/orchestration.py` with
  nodes `agent → discover → tool → approval → guardrail → pay → respond → audit`,
  a single forward pass (no agent↔tool loop). Do not flatten this into one
  function or drop `approval`/`guardrail` into the pay node.
- `buyer_agent.py` is a standalone script that speaks HTTP only (`requests`) —
  it must never import from `app/`.
- No secrets in source. Only `.env` (see `.env.example`), never committed.
- Every external call (Gemini, SerpAPI/discovery, Razorpay) is wrapped so
  failures become a normal conversational reply — no unhandled exception may
  reach the client.

## Stack
Python 3.11 (local venv; `runtime.txt` pins 3.13.7 for Render), FastAPI,
SQLAlchemy + SQLite, **Google Gemini** optional single structured
requirement-extraction call (`langchain-google-genai`, model
`gemini-3.5-flash-lite`, env `GEMINI_API_KEY`, called in `router.py` behind the
confidence gate — deterministic rules run first), LangGraph (thin forward-pass
StateGraph in `orchestration.py`), Razorpay SDK (test mode), vanilla
HTML/CSS/JS frontend, pytest + httpx.

## Commands
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill GEMINI_API_KEY + Razorpay test keys
uvicorn app.main:app --reload   # health: http://127.0.0.1:8000/health
pytest                    # no real keys or network required
python buyer_agent.py     # demo: needs running API (no seeded DB required)
```

Testing quirks: `tests/test_guardrail.py` is pure-function (no DB/network) —
run it alone with `pytest tests/test_guardrail.py`.
`tests/conftest.py` replaces `app.db.SessionLocal` with an in-memory SQLite DB
using `StaticPool` (required so TestClient threads share one DB). DB-touching
tests pass `db_session_factory` AND monkeypatch
`app.discovery.search_external_sources` and `app.commerce.rzp_create_payment_link`
(aliased as `rzp_create_payment_link` in commerce). Guardrail limits to
remember: `human` ₹5000/transaction · ₹15000/session; `buyer_agent`
₹3000/transaction · ₹5000/session (`app/guardrail.py`).

## Structure
```
app/            FastAPI routes, models, guardrail, quote, commerce, orchestration
  main.py           routing only — no business logic (chat builds state, invokes graph)
  orchestration.py  LangGraph nodes + build_graph (agent→discover→tool→approval→
                    guardrail→pay→respond→audit), run_turn/chat_reply
  guardrail.py      deterministic policy: validate_approval + validate_quote_against_cart
                    + check_transaction (fail-closed, no LLM)
  quote.py          canonical cart hash, immutable quotes, cart version
  commerce.py       cart/order/payment logic, Decimal math
  router.py         fast intent/domain/risk rule-based classification (+ optional LLM)
  discovery.py      external product discovery (async SerpAPI / offline mock)
  merchant_capture.py  deterministic merchant-fit scoring + capture verdicts
  offer_engine.py   next-best-offer expected-revenue selection
  semantic_cache.py semantic search-cache (embedding + TTL)
  growth.py         offer accept rates (Beta-smoothed) + insights
  observability.py  per-stage latency + audit writes + stage context managers
  razorpay_client.py  Razorpay SDK wrapper + webhook HMAC verify
frontend/       chat + audit viewer, static HTML/JS
tests/          pytest suite
buyer_agent.py  standalone external-agent simulation (repo root, not in app/)
docs/           architecture / HLD / LLD — original design intent, out of sync
```

## Gotchas
- `app/main.py` is routing only. If you're writing DB queries or LLM calls there
  that don't already exist, they belong in another module.
- There are no migrations. After any schema change in `app/models.py`, delete
  `growthmate.db` (the `.catalog_index.json` rebuilds itself at startup). A
  fresh DB starts with an EMPTY merchant catalog — products are added at
  runtime from SERP discovery (`EXT-xxx` reference items) and `seed_data.py`
  no longer exists (Rev 3).
- The "engineered failure" demo (buyer cart whose total exceeds its per-tx
  limit) must return HTTP 200 with `"blocked": true`, not a 4xx/5xx — a block
  is
  expected control flow, not an error; it must also create no Order row.
- `razorpay==1.4.2` imports `pkg_resources` at runtime, hence the `setuptools<81`
  pin in `requirements.txt` — keep it.
- `frontend/audit.js` reads Rev-2 field names `decision` / `reason` (the API no
  longer returns `guardrail_decision`/`guardrail_reason`). Don't use frontend JS
  as the field-name source of truth. `chat.js` hardcodes `actor: "human"` and
  carries `history` across turns (harmless — the server stores durable state).
- `orchestration._find_sku` must resolve full `XXX-###` SKUs against the catalog
  — a bare `\d{3}` regex mis-maps e.g. `SHOE-001` → `APP-001`. Keep `_SKU_RE`
  as `\b([A-Z]{2,6}-\d{2,6})\b`.
- `discover_node` short-circuits for the `gather` action (clarifying question) —
  it must not run discovery or let `tool_node` overwrite the reply with a cart
  summary.
- `.env` vars: `GEMINI_API_KEY`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
  `DATABASE_URL` (default `sqlite:///./growthmate.db`),
  `SERPAPI_KEY` (optional — live discovery; without it `discovery.py` falls
  back to the offline mock catalog), `BUYER_BASE_URL`/`BUYER_SESSION_ID`
  (buyer_agent only). All are listed in `.env.example`.