from decimal import Decimal
from pathlib import Path

from suparch.models import StackSelection
from suparch.repositories import JsonCatalogRepository
from suparch.services import CatalogService

CATALOG_PATH = (
    Path(__file__).parents[1] / "src" / "suparch" / "data" / "sample_catalog.json"
)


def service() -> CatalogService:
    return CatalogService(JsonCatalogRepository(CATALOG_PATH))


def test_compares_common_ingredients() -> None:
    result = service().compare_products(
        [
            "example:magnesium-glycinate",
            "example:calcium-magnesium",
        ]
    )

    assert result.common_ingredients == ["magnesium"]


def test_calculates_stack_and_marks_duplicates() -> None:
    result = service().calculate_stack(
        [
            StackSelection(
                product_id="example:magnesium-glycinate",
                servings_per_day="1",
            ),
            StackSelection(
                product_id="example:calcium-magnesium",
                servings_per_day="2",
            ),
        ]
    )

    magnesium = next(
        total for total in result.totals if total.canonical_name == "magnesium"
    )
    assert magnesium.total_amount == Decimal("700000")
    assert magnesium.unit == "mcg"
    assert result.duplicate_ingredients == ["magnesium"]

