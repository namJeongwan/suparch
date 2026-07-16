from decimal import Decimal

from suparch.normalization import build_ingredient, canonicalize_ingredient


def test_canonicalizes_vitamin_alias_and_form() -> None:
    canonical, form = canonicalize_ingredient(
        "Vitamin D3 (as Cholecalciferol)"
    )

    assert canonical == "vitamin d"
    assert form == "cholecalciferol"


def test_normalizes_mass_to_micrograms() -> None:
    ingredient = build_ingredient(
        "Magnesium (as Magnesium Glycinate)",
        "200 mg",
        "48%",
    )

    assert ingredient.amount == Decimal("200")
    assert ingredient.unit == "mg"
    assert ingredient.normalized_amount == Decimal("200000")
    assert ingredient.normalized_unit == "mcg"


def test_maps_common_b_vitamin_alias() -> None:
    ingredient = build_ingredient("Vitamin B5", "10 mg")

    assert ingredient.canonical_name == "pantothenic acid"


def test_parses_cfu_magnitude() -> None:
    ingredient = build_ingredient("Probiotic Cultures", "50 Billion CFU")

    assert ingredient.amount == Decimal("50000000000")
    assert ingredient.normalized_amount == Decimal("50000000000")
    assert ingredient.normalized_unit == "CFU"


def test_parses_plural_cfu_with_footnote() -> None:
    ingredient = build_ingredient("Probiotic Cultures", "20 Billion CFUs¹")

    assert ingredient.amount == Decimal("20000000000")
    assert ingredient.unit == "CFU"
