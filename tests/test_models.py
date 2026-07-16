from decimal import Decimal

from suparch.models import Ingredient


def test_ingredient_normalizes_canonical_name() -> None:
    ingredient = Ingredient(
        canonical_name="  Magnesium  ",
        label_name="Magnesium",
        amount="200",
        unit="mg",
    )

    assert ingredient.canonical_name == "magnesium"
    assert ingredient.amount == Decimal("200")

