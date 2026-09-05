"""
Cross-sell (Revision 1) — deterministic complementary suggestions.

Two tiers, both derived without an LLM:
  1. `catalog`  — related merchant SKUs the merchant stocks (payable). Defined
                  by an explicit SKU adjacency map so the merchant controls
                  what is pushed (earbuds -> charger, chinos -> t-shirt).
  2. `external` — market/SERP items the merchant does NOT stock (e.g. "shoe
                  polish" for footwear). Addable/payable via an EXT-xxx SKU
                  minted by the chat layer (Rev 3: any SERP match can be
                  added to the cart and paid).

Failures degrade gracefully to fewer/no suggestions; a cross-sell hint must
never crash a reply.
"""

from app import commerce, discovery


_CATALOG_COMPLEMENTS: dict[str, list[str]] = {
    "ELEC-001": ["ELEC-002"],  # Wireless Earbuds -> USB-C Fast Charger
    "ELEC-002": ["ELEC-001"],  # charger -> earbuds
    "APP-001": ["APP-002"],    # t-shirt -> chinos
    "APP-002": ["APP-001"],    # chinos -> t-shirt
    "HOME-001": ["HOME-002"],  # coffee mug -> desk lamp
    "HOME-002": ["HOME-001"],  # desk lamp -> coffee mug
    "SHOE-001": [],
    "SHOE-002": [],
    "SHOE-003": [],
    "ACC-001": [],
}

# Category -> external market complements (merchant does not stock these).
# The first term is the one actually searched; later terms are fallbacks when
# the earlier search returns nothing. "Shoe polish" is the canonical example.
_EXTERNAL_COMPLEMENTS: dict[str, list[str]] = {
    "footwear": ["shoe polish", "shoe care kit", "shoe insoles"],
    "apparel": ["socks", "undershirt"],
    "electronics": ["earbud charging case", "usb-c cable"],
    "home": ["drink coaster", "desk organizer"],
    "accessories": ["card holder", "belt"],
}


def catalog_complements(sku: str, session_id: str | None = None) -> list[dict]:
    """Deterministic, payable-only catalog complements for a merchant SKU.
    No external discovery — safe to call on the hot path for suggestion lists."""
    complement: list[dict] = []
    for comp_sku in _CATALOG_COMPLEMENTS.get(sku, []):
        product = commerce.get_product_by_sku(comp_sku)
        if product is None:
            continue
        complement.append(
            {
                "sku": product.sku,
                "name": product.name,
                "price": float(product.price or 0.0),
                "currency": product.currency or "INR",
                "stock": int(product.stock or 0),
                "payable": True,
                "why": "pairs well with your selected item",
            }
        )
    return complement


def complements_for(sku: str, session_id: str) -> dict:
    """Return {catalog: [...], external: [...]} complementary suggestions for a
    merchant SKU. External discovery is one bounded SerpAPI call max; failures
    just drop the external tier."""
    catalog: list[dict] = []
    for comp in catalog_complements(sku, session_id):
        catalog.append({k: comp[k] for k in comp if k != "stock"})

    external: list[dict] = []
    category = _product_category(sku)
    for term in _EXTERNAL_COMPLEMENTS.get(category, []):
        try:
            result = discovery.discover(
                {
                    "category": None,
                    "product_type": term,
                    "budget": None,
                    "keywords": [term],
                    "required_features": [],
                    "explicit_item": [],
                },
                session_id,
            )
            recs = result.get("recommendations") or []
            if recs:
                rec = recs[0]
                external.append(
                    {
                        "name": rec.get("name", ""),
                        "price": float(rec.get("price") or 0.0),
                        "currency": rec.get("currency", "INR"),
                        "source": rec.get("source", ""),
                        "payable": True,  # SERP complements are addable (Rev 3)
                        "why": f"market complement ({term})",
                    }
                )
                break  # one bounded external search per turn
        except Exception:  # noqa: BLE001 — external tier is optional
            continue
    return {"catalog": catalog, "external": external}


def _product_category(sku: str) -> str:
    product = commerce.get_product_by_sku(sku)
    return (product.category if product is not None else "") or ""