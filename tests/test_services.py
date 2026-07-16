from pathlib import Path

from suparch.models import ProductSearchQuery
from suparch.repositories import JsonCatalogRepository
from suparch.services import CatalogService

CATALOG_PATH = (
    Path(__file__).parents[1] / "src" / "suparch" / "data" / "sample_catalog.json"
)


def service() -> CatalogService:
    return CatalogService(JsonCatalogRepository(CATALOG_PATH))


def test_searches_by_included_and_excluded_ingredients() -> None:
    result = service().search_products(
        ProductSearchQuery(
            include_ingredients=["magnesium"],
            exclude_ingredients=["calcium"],
        )
    )

    assert result.total == 1
    assert result.products[0].id == "example:magnesium-glycinate"


def test_searches_by_ingredient_form() -> None:
    result = service().search_products(
        ProductSearchQuery(
            include_ingredients=["magnesium"],
            forms=["glycinate"],
        )
    )

    assert [product.id for product in result.products] == [
        "example:magnesium-glycinate"
    ]


def test_applies_currency_and_price_filters() -> None:
    result = service().search_products(
        ProductSearchQuery(
            currency="USD",
            max_price="10",
        )
    )

    assert [product.id for product in result.products] == ["example:vitamin-d3"]


def test_returns_product_by_id() -> None:
    product = service().get_product("example:vitamin-d3")

    assert product is not None
    assert product.name == "Vitamin D3"

