from __future__ import annotations

from aviapars.adapters._tutu_common import select_price_variant
from aviapars.adapters.base import Tier


def _variant(price_type: str, amount: object) -> dict[str, object]:
    return {"price": {"type": price_type, "value": {"amount": amount, "currencyCode": "RUB", "fraction": 100}}}


def test_missing_value_key_is_excluded_not_a_crash() -> None:
    # regression: a variant with type "exact" but no "value" key at all used to KeyError
    # inside min()'s key function and take down the whole search() call.
    malformed = {"price": {"type": "exact"}}
    variants = [malformed, _variant("exact", 500000)]

    result = select_price_variant(variants)

    assert result is not None
    chosen, confidence, tier = result
    assert chosen is variants[1]
    assert confidence == 1.0
    assert tier == Tier.CONFIRMATION


def test_non_numeric_amount_is_excluded() -> None:
    variants = [_variant("exact", None), _variant("exact", "not-a-number"), _variant("exact", 300000)]

    result = select_price_variant(variants)

    assert result is not None
    chosen, _, _ = result
    assert chosen["price"]["value"]["amount"] == 300000  # type: ignore[index]


def test_all_variants_malformed_returns_none() -> None:
    variants = [{"price": {"type": "exact"}}, _variant("exact", None), _variant("approximate", "bad")]

    assert select_price_variant(variants) is None


def test_picks_cheapest_among_usable_exact_variants() -> None:
    variants = [_variant("exact", 900000), _variant("exact", 500000), _variant("absent", 0)]

    result = select_price_variant(variants)

    assert result is not None
    chosen, confidence, tier = result
    assert chosen["price"]["value"]["amount"] == 500000  # type: ignore[index]
    assert confidence == 1.0 and tier == Tier.CONFIRMATION


def test_falls_back_to_approximate_when_no_usable_exact() -> None:
    variants = [_variant("exact", "garbage"), _variant("approximate", 700000)]

    result = select_price_variant(variants)

    assert result is not None
    _, confidence, tier = result
    assert confidence == 0.5 and tier == Tier.DISCOVERY
