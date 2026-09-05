"""
Router tests — fast intent/domain/risk classification (LLD §3.2, §5).
Deterministic rules first; the LLM rung is only exercised at low confidence and
is failure-tolerant (tests stub it out and prove degradation to clarify).
"""

from app import router
from app.config import ROUTER_CONFIDENCE_FAST_PATH, ROUTER_CONFIDENCE_LLM_PATH


def test_fast_path_confidence_high_for_add_sku():
    d = router.classify("add APP-001", {})
    assert d["intent"] == "cart"
    assert d["action"] == "cart_add"
    assert d["requires_llm"] is False
    assert d["confidence"] >= ROUTER_CONFIDENCE_FAST_PATH


def test_fast_path_remove_sku():
    d = router.classify("remove APP-005", {})
    assert d["action"] == "cart_remove"
    assert d["requires_llm"] is False


def test_fast_path_show_cart_and_total():
    assert router.classify("show my cart", {})["action"] == "cart_show"
    assert router.classify("what is my total", {})["action"] == "total"


def test_approval_requires_preview():
    # Without a shown preview, "yes" must NOT route to approve_checkout.
    d = router.classify("yes proceed", {"checkout_preview": None, "cart": []})
    assert d["action"] != "approve_checkout"

    d = router.classify("yes proceed", {"checkout_preview": {"total": 100}, "cart": [1]})
    assert d["action"] == "approve_checkout"
    assert d["risk"] == "high"


def test_discovery_with_category_and_budget():
    d = router.classify("I want running shoes under 2500", {})
    assert d["intent"] == "discovery"
    assert d["action"] == "recommend"
    # The router routes to discovery at fast-path confidence when it has both a
    # category synonym and a budget, and flags merchant retrieval.
    assert d["confidence"] >= ROUTER_CONFIDENCE_FAST_PATH
    assert d["requires_merchant_retrieval"] is True


def test_category_and_budget_extractors():
    # The deterministic extractors (used to build structured_requirements) work
    # separate from the routing decision shape.
    assert router.extract_category("running shoes") == "footwear"
    assert router.extract_category("sneakers") == "footwear"
    assert router.extract_budget("under ₹1,250") == 1250.0
    assert router.extract_budget("budget is 3000") == 3000.0
    assert router.extract_budget("running shoes") is None
    d = router.classify("hello", {})
    assert d["action"] == "clarify"
    assert d["confidence"] < ROUTER_CONFIDENCE_LLM_PATH
    assert d["requires_llm"] is False


def test_live_search_only_when_requested():
    d = router.classify("show me market alternatives for earbuds", {})
    assert d["requires_live_search"] is True
    d2 = router.classify("find earbuds", {})
    assert d2["requires_live_search"] is False


def test_extract_requirements_llm_failure_is_empty(monkeypatch):
    # LLM failure must degrade to {} -> caller clarifies (never a crash).
    # Patch the module the function imports from (it does a local
    # `from langchain_google_genai import ChatGoogleGenerativeAI`), so this is
    # simulated regardless of whether a real API key is configured.
    import langchain_google_genai as lgg

    class Boom:
        def __init__(self, *a, **k):
            pass

        def invoke(self, *a, **k):
            raise RuntimeError("simulated timeout")

    monkeypatch.setattr(lgg, "ChatGoogleGenerativeAI", Boom)
    out = router.extract_requirements_llm("I want headphones please")
    assert out == {}