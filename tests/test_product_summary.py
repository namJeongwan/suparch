from pathlib import Path

from suparch.catalog import load_json_catalog
from suparch.models import ProductSummary

SAMPLE_CATALOG = (
    Path(__file__).parents[1] / "src" / "suparch" / "data" / "sample_catalog.json"
)


def test_product_summary_is_compact() -> None:
    product = load_json_catalog(SAMPLE_CATALOG)[0]

    summary = ProductSummary.from_product(product)
    payload = summary.model_dump()

    assert "active_ingredients" not in payload
    assert summary.ingredient_count == len(summary.ingredient_names)
    assert summary.ingredient_names_truncated is False
    assert "magnesium" in summary.ingredient_names


def test_product_summary_caps_ingredient_names() -> None:
    product = load_json_catalog(SAMPLE_CATALOG)[0]
    ingredient = product.active_ingredients[0]
    product.active_ingredients = [
        ingredient.model_copy(
            update={
                "canonical_name": f"ingredient-{index}",
                "label_name": f"Ingredient {index}",
            }
        )
        for index in range(30)
    ]

    summary = ProductSummary.from_product(product)

    assert summary.ingredient_count == 30
    assert len(summary.ingredient_names) == 20
    assert summary.ingredient_names_truncated is True
