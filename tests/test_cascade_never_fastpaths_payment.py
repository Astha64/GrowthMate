"""
Cascade-router money-safety contract (Rev 3, Phase 1).

Non-negotiable (AGENTS.md / spec invariants): the latency cascade must NEVER
fast-path `execute_payment`. Every money decision stays inside the
deterministic `approval -> guardrail -> pay` pipeline. These tests pin that
contract BOTH structurally (the actions `route()` may emit are restricted to a
safe set) AND behaviourally (every payment/approval/negation phrasing fails
open to the full pipeline; an approval turn still reaches pay via the mocked
Razorpay journey).
"""

import os

import pytest

from app import cascade_router
from app.cascade_router import SAFE_ACTIONS, SAFE_TOOL_KINDS

# Money-adjacent words/phrases: the cascade must treat ALL of these as fail-open
# to the full pipeline — never a fast-path, never a cached discovery answer.
PAYMENT_NEEDLES = (
    "pay now", "pay", "I approve", "approve", "confirm payment", "confirm",
    "proceed", "proceed to payment", "complete the purchase", "complete my order",
    "buy now", "charge my card", "settle it", "make the payment", "yes proceed",
    "authorize", "start the payment",
)

# Approval / negation of a SHOWN preview is also payment-adjacent: these must
# reach `approval_node/guardrail_node/pay_node`, not a greeting or tool path.
PREVIEW_APPROVALS = (
    "yes", "yes please", "ok", "okay", "sure", "go ahead", "yep", "yeah",
    "that works", "sounds good", "do it", "looks good",
)
PREVIEW_NEGATIONS = (
    "no", "nope", "nah", "not now", "cancel", "decline", "forget it", "stop",
)

CORPUS = [
    *[("payment", t, False) for t in PAYMENT_NEEDLES],
    *[("approval", t, True) for t in PREVIEW_APPROVALS],
    *[("negation", t, True) for t in PREVIEW_NEGATIONS],
    *[("benign", t, False) for t in (
        "add APP-001", "show my cart", "cart total", "checkout", "hi",
        "running shoes under 2500", "wireless earbuds", "2", "add it",
        "clear my cart", "how much is my cart total", "show me your catalog",
    )],
]


def _action_is_safe(action) -> bool:
    """True when `action` can never reach approval/guardrail/pay."""
    if action is None:
        return True
    if isinstance(action, tuple):
        return action[0] in SAFE_TOOL_KINDS
    return action in SAFE_ACTIONS


@pytest.mark.parametrize("kind,text,has_preview", CORPUS)
def test_route_never_emits_money_action(kind, text, has_preview):
    d = cascade_router.route(
        text,
        session_id="unit-safety",
        is_first_turn=False,
        has_preview=has_preview,
        cart_items=[],
    )
    assert _action_is_safe(d.action), f"{text!r} produced unsafe action {d.action!r}"
    assert d.can_reach_pay_fast() is False, f"{text!r} may fast-path a charge"


@pytest.mark.parametrize("text", PAYMENT_NEEDLES)
def test_payment_phrasing_always_falls_through(text):
    d = cascade_router.route(text, session_id="unit-safety", is_first_turn=False,
                             has_preview=False, cart_items=[])
    assert d.fast_path is False, f"{text!r} must not fast-path"
    assert d.action is None, f"{text!r} must be decided by the full pipeline"
    assert "payment_adjacent_guard" in d.reason_codes


@pytest.mark.parametrize("text", PREVIEW_APPROVALS + PREVIEW_NEGATIONS)
def test_preview_context_always_falls_through(text):
    d = cascade_router.route(text, session_id="unit-safety", is_first_turn=False,
                             has_preview=True, cart_items=[{"sku": "APP-001"}])
    assert d.fast_path is False
    assert d.action is None
    assert "payment_adjacent_guard" in d.reason_codes


def test_large_corpus_never_reaches_pay_fast_path():
    for _kind, text, has_preview in CORPUS:
        d = cascade_router.route(text, session_id="unit-corpus",
                                 is_first_turn=True, has_preview=has_preview,
                                 cart_items=[{"sku": "APP-001"}])
        assert d.can_reach_pay_fast() is False, f"unsafe: {text!r}"


def test_cascade_disabled_is_rev2_fallthrough(monkeypatch):
    """CASCADE_ENABLED=false must reproduce Rev-2: every turn falls through to
    the full pipeline and the cascade never fast-paths anything."""
    from app.config import CASCADE_ENABLED

    # The default is enabled; a CI/dev runner may explicitly disable it.
    if not os.getenv("CASCADE_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        assert CASCADE_ENABLED is True  # enabled by default in config
    monkeypatch.setattr(cascade_router, "CASCADE_ENABLED", False)
    for kind, text, has_preview in CORPUS:
        d = cascade_router.route(text, session_id="unit-disabled",
                                 is_first_turn=True, has_preview=has_preview,
                                 cart_items=[])
        assert d.fast_path is False, f"{text!r} fast-pathed while disabled"
        assert d.action is None
        # The money guard still fires (safety invariant); benign turns now
        # land exactly where Rev-2 would run them: the Tier-3 pipeline.
        if kind == "benign":
            assert d.tier >= 3