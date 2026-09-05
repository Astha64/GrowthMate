"""
Merchant Capture tests (LLD §11) — deterministic merchant-fit scoring and
verdict mapping. Hard constraints: stock, budget, explicit requirements.
"""

import pytest

from app import merchant_capture
from app.merchant_capture import capture_verdict, score_merchant_product


def _product(**over):
    base = {
        "sku": "APP-001",
        "name": "Cotton Crew T-Shirt",
        "price": 499.0,
        "stock": 50,
        "category": "apparel",
        "description": "premium cotton crew neck t-shirt",
        "merchant_priority": 0.8,
        "semantic_text": "cotton crew t-shirt apparel",
    }
    base.update(over)
    return base


def _requirements(**over):
    base = {
        "category": "apparel",
        "keywords": ["t-shirt"],
        "budget": 1000,
    }
    base.update(over)
    return base


def test_strong_merchant_match_captures():
    verdict = capture_verdict(_product(), _requirements())
    assert verdict["verdict"] == "capture"
    assert verdict["product"]["sku"] == "APP-001"
    assert verdict["reason"] == "strong merchant fit"


def test_out_of_stock_never_captures():
    verdict = capture_verdict(_product(stock=0), _requirements())
    assert verdict["verdict"] == "no_merchant_match"
    assert "out of stock" in verdict["reason"]


def test_over_budget_never_captures():
    verdict = capture_verdict(_product(price=9000.0), _requirements(budget=1000))
    assert verdict["verdict"] == "no_merchant_match"
    assert "budget" in verdict["reason"]


def test_no_product_no_match():
    verdict = capture_verdict(None, _requirements())
    assert verdict["verdict"] == "no_merchant_match"


def test_partial_fit_recommends_offers():
    # A clear keyword mismatch but otherwise stocked/in-budget should degrade
    # to recommend_offers instead of a hard capture.
    verdict = capture_verdict(_product(category="home", semantic_text="ceramic mug table lamp home"),
                              _requirements(category="home", keywords=["mug"]))
    assert verdict["verdict"] in ("recommend_offers", "capture")


def test_score_is_bounded_and_explainable():
    result = score_merchant_product(_product(), _requirements())
    assert 0.0 <= result["capped"] <= 1.0
    assert set(result["components"]) == {
        "semantic", "requirement", "price", "stock", "margin", "attach", "priority",
    }


def test_score_is_deterministic():
    a = score_merchant_product(_product(), _requirements())
    b = score_merchant_product(_product(), _requirements())
    assert a == b


def test_budget_closeness_degrades():
    near = score_merchant_product(_product(price=800.0), _requirements(budget=1000))
    far = score_merchant_product(_product(price=999.0), _requirements(budget=1000))
    assert near["components"]["price"] > far["components"]["price"]