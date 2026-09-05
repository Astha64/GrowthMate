"""
Pure-function tests for the guardrail layer. Must NOT touch the DB or network
(AGENTS.md gotcha). Table-driven, no mocking (LLD §11.8).

Covers BOTH independent checks (LLD §6):
  - validate_approval (§6.1): approved / not-approved / ambiguous -> blocked
  - check_transaction (§6.2): spend limits
"""

from app.guardrail import (
    ALLOWED_ACTORS,
    MAX_PER_SESSION,
    MAX_PER_TRANSACTION,
    check_transaction,
    validate_approval,
)

HUMAN_TX = MAX_PER_TRANSACTION["human"]  # 5000
BA_TX = MAX_PER_TRANSACTION["buyer_agent"]  # 3000
HUMAN_SESSION = MAX_PER_SESSION["human"]  # 15000
BA_SESSION = MAX_PER_SESSION["buyer_agent"]  # 5000


def test_unknown_actor_blocked():
    d = check_transaction("evil", 10.0, 0.0)
    assert d.allowed is False
    assert d.reason == "unknown actor"


def test_all_actors_allowed():
    assert ALLOWED_ACTORS == {"human", "buyer_agent"}


def test_within_limits_human():
    d = check_transaction("human", 1500.0, 0.0)
    assert d.allowed is True
    assert d.reason == "within limits"


def test_within_limits_buyer_agent():
    d = check_transaction("buyer_agent", 2999.0, 0.0)
    assert d.allowed is True


def test_exact_transaction_limit_allowed():
    d = check_transaction("human", HUMAN_TX, 0.0)
    assert d.allowed is True


def test_over_transaction_limit_human():
    d = check_transaction("human", HUMAN_TX + 1.0, 0.0)
    assert d.allowed is False
    assert f"per-transaction limit of ₹{int(HUMAN_TX)}" in d.reason


def test_over_transaction_limit_buyer_agent():
    # Engineered failure (§11.5): APP-001 x10 = 4990 > 3000
    d = check_transaction("buyer_agent", 4990.0, 0.0)
    assert d.allowed is False
    assert "per-transaction limit of ₹3000" in d.reason


def test_at_transaction_limit_buyer_agent():
    d = check_transaction("buyer_agent", BA_TX, 0.0)
    assert d.allowed is True


def test_over_session_limit_human():
    d = check_transaction("human", 1000.0, HUMAN_SESSION - 500.0)
    assert d.allowed is False
    assert f"per-session limit of ₹{int(HUMAN_SESSION)}" in d.reason


def test_over_session_limit_buyer_agent():
    d = check_transaction("buyer_agent", 1000.0, BA_SESSION - 500.0)
    assert d.allowed is False


def test_exact_session_limit_allowed():
    # spend_so_far such that spend + amount == limit exactly -> allowed (not >)
    d = check_transaction("human", 1000.0, HUMAN_SESSION - 1000.0)
    assert d.allowed is True


def test_negative_or_zero_amount_within_limits():
    d = check_transaction("human", 0.0, 0.0)
    assert d.allowed is True


def test_pure_function_deterministic():
    # Same inputs -> same output, always (stateless)
    a = check_transaction("buyer_agent", 2500.0, 0.0)
    b = check_transaction("buyer_agent", 2500.0, 0.0)
    assert a.allowed == b.allowed and a.reason == b.reason


# ---------------------------------------------------------------------------
# validate_approval (§6.1) — fail-closed approval classification.
# ---------------------------------------------------------------------------

def _state(preview_shown: bool, last_user_msg: str) -> dict:
    return {
        "checkout_preview": {"total": 3448.0} if preview_shown else None,
        "messages": [{"role": "user", "content": last_user_msg}],
    }


def test_approval_explicit_yes():
    assert validate_approval(_state(True, "yes")) is True


def test_approval_go_ahead():
    assert validate_approval(_state(True, "go ahead")) is True


def test_approval_looks_good_with_extra_affirmation():
    assert validate_approval(_state(True, "yes, go ahead")) is True


def test_approval_phrased_confirmation():
    assert validate_approval(_state(True, "looks good, please proceed")) is True


def test_approval_missing_preview_is_not_approved():
    # Fail-closed: no checkout preview shown -> never approved.
    assert validate_approval(_state(False, "yes")) is False


def test_approval_missing_user_message_is_not_approved():
    state = {"checkout_preview": {"total": 3448.0}, "messages": []}
    assert validate_approval(state) is False


def test_approval_blank_user_message_is_not_approved():
    assert validate_approval(_state(True, "")) is False


def test_approval_negative_is_not_approved():
    assert validate_approval(_state(True, "no")) is False
    assert validate_approval(_state(True, "no thanks")) is False


def test_approval_ambiguous_defaults_to_blocked():
    # "not sure"/"maybe" contain no unambiguous approval -> fail-closed False.
    assert validate_approval(_state(True, "not sure")) is False
    assert validate_approval(_state(True, "maybe")) is False
    assert validate_approval(_state(True, "wait a moment")) is False
    assert validate_approval(_state(True, "hold on")) is False


def test_approval_is_deterministic():
    a = validate_approval(_state(True, "yes"))
    b = validate_approval(_state(True, "yes"))
    assert a is True and b is True
