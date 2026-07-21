from decimal import Decimal
from pathlib import Path

import pytest

from suparch.models import (
    Ingredient,
    IngredientTarget,
    ProductMatchQuery,
    ProductSearchQuery,
)
from suparch.repositories import InMemoryCatalogRepository, JsonCatalogRepository
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


def test_ranks_products_by_required_and_preferred_targets() -> None:
    result = service().match_products(
        ProductMatchQuery(
            required_ingredients=[
                IngredientTarget(
                    name="magnesium",
                    minimum_amount="200",
                    maximum_amount="300",
                    unit="mg",
                )
            ],
            preferred_ingredients=[
                IngredientTarget(
                    name="calcium",
                    minimum_amount="400",
                    maximum_amount="600",
                    unit="mg",
                )
            ],
        )
    )

    assert [match.product.id for match in result.matches] == [
        "example:calcium-magnesium",
        "example:magnesium-glycinate",
    ]
    assert result.matches[0].score == 100
    assert result.matches[1].score == Decimal("66.67")
    assert result.matches[1].missing_ingredients == ["calcium"]
    assert result.candidate_total == 2
    assert result.evaluated_count == 2
    assert result.truncated is False


def test_flags_amounts_above_caller_supplied_maximum() -> None:
    result = service().match_products(
        ProductMatchQuery(
            required_ingredients=[
                IngredientTarget(
                    name="magnesium",
                    maximum_amount="225",
                    unit="mg",
                )
            ]
        )
    )

    assert result.matches[0].product.id == "example:magnesium-glycinate"
    flagged = next(
        match
        for match in result.matches
        if match.product.id == "example:calcium-magnesium"
    )
    assert flagged.score == 0
    assert flagged.above_maximum == ["magnesium"]


def test_rejects_overlapping_required_and_preferred_targets() -> None:
    request = ProductMatchQuery(
        required_ingredients=[IngredientTarget(name="vitamin d")],
        preferred_ingredients=[IngredientTarget(name="vitamin d3")],
    )

    try:
        service().match_products(request)
    except ValueError as error:
        assert str(error) == "Ingredients cannot be both required and preferred: vitamin d"
    else:
        raise AssertionError("Expected overlapping targets to be rejected")


def test_rejects_unsupported_target_unit_before_searching() -> None:
    request = ProductMatchQuery(
        required_ingredients=[
            IngredientTarget(name="not-in-catalog", minimum_amount="1", unit="mL")
        ]
    )

    try:
        service().match_products(request)
    except ValueError as error:
        assert str(error) == "Unsupported ingredient target unit: mL"
    else:
        raise AssertionError("Expected unsupported units to be rejected")


def test_aggregates_compatible_duplicate_ingredient_rows() -> None:
    base = service().get_product("example:magnesium-glycinate")
    assert base is not None
    ingredients = [
        Ingredient(
            canonical_name="magnesium",
            label_name="Magnesium source one",
            amount="50",
            unit="mg",
        ),
        Ingredient(
            canonical_name="magnesium",
            label_name="Magnesium source two",
            amount="500",
            unit="mg",
        ),
    ]
    matching_service = CatalogService(
        InMemoryCatalogRepository(
            [base.model_copy(update={"active_ingredients": ingredients})]
        )
    )

    result = matching_service.match_products(
        ProductMatchQuery(
            required_ingredients=[
                IngredientTarget(
                    name="magnesium",
                    maximum_amount="300",
                    unit="mg",
                )
            ]
        )
    )

    assert result.matches[0].score == 0
    assert result.matches[0].above_maximum == ["magnesium"]


def test_does_not_compare_dfe_as_plain_micrograms_from_stale_catalog() -> None:
    base = service().get_product("example:magnesium-glycinate")
    assert base is not None
    folate = Ingredient(
        canonical_name="folate",
        label_name="Folate",
        amount="833",
        unit="mcg DFE",
        normalized_amount="833",
        normalized_unit="mcg",
    )
    matching_service = CatalogService(
        InMemoryCatalogRepository(
            [base.model_copy(update={"active_ingredients": [folate]})]
        )
    )

    result = matching_service.match_products(
        ProductMatchQuery(
            required_ingredients=[
                IngredientTarget(
                    name="folate",
                    minimum_amount="400",
                    maximum_amount="800",
                    unit="mcg",
                )
            ]
        )
    )

    assert result.matches[0].score == 0
    assert result.matches[0].unit_mismatches == ["folate"]


def test_prefers_nested_equivalent_quantity_over_parent_total() -> None:
    base = service().get_product("example:magnesium-glycinate")
    assert base is not None
    folate_rows = [
        Ingredient(
            canonical_name="folate",
            label_name="Folate",
            amount="1330",
            unit="mcg",
        ),
        Ingredient(
            canonical_name="folate",
            label_name="Folic Acid",
            amount="800",
            unit="mcg",
            parent_ingredient="folate",
        ),
    ]
    matching_service = CatalogService(
        InMemoryCatalogRepository(
            [base.model_copy(update={"active_ingredients": folate_rows})]
        )
    )

    result = matching_service.match_products(
        ProductMatchQuery(
            required_ingredients=[
                IngredientTarget(
                    name="folate",
                    minimum_amount="400",
                    maximum_amount="800",
                    unit="mcg",
                )
            ]
        )
    )

    assert result.matches[0].score == 100


@pytest.mark.parametrize(
    "extra_row",
    [
        Ingredient(
            canonical_name="magnesium",
            label_name="Magnesium upper bound",
            amount="50",
            unit="mg",
            amount_operator="<",
        ),
        Ingredient(canonical_name="magnesium", label_name="Magnesium unknown"),
        Ingredient(
            canonical_name="magnesium",
            label_name="Magnesium alternate unit",
            amount="50",
            unit="IU",
        ),
    ],
)
def test_treats_mixed_exact_and_uncertain_rows_as_not_comparable(
    extra_row: Ingredient,
) -> None:
    base = service().get_product("example:magnesium-glycinate")
    assert base is not None
    exact = Ingredient(
        canonical_name="magnesium",
        label_name="Magnesium exact",
        amount="100",
        unit="mg",
    )
    matching_service = CatalogService(
        InMemoryCatalogRepository(
            [base.model_copy(update={"active_ingredients": [exact, extra_row]})]
        )
    )

    result = matching_service.match_products(
        ProductMatchQuery(
            required_ingredients=[
                IngredientTarget(
                    name="magnesium",
                    maximum_amount="100",
                    unit="mg",
                )
            ]
        )
    )

    assert result.matches[0].score == 0
    assert result.matches[0].not_comparable == ["magnesium"]


def test_marks_ambiguous_flattened_parent_hierarchy_not_comparable() -> None:
    base = service().get_product("example:magnesium-glycinate")
    assert base is not None
    ingredients = [
        Ingredient(
            canonical_name="magnesium",
            label_name="Independent magnesium one",
            amount="10",
            unit="mg",
        ),
        Ingredient(
            canonical_name="magnesium",
            label_name="Independent magnesium two",
            amount="20",
            unit="mg",
        ),
        Ingredient(
            canonical_name="magnesium",
            label_name="Nested magnesium",
            amount="30",
            unit="mg",
            parent_ingredient="magnesium",
        ),
    ]
    matching_service = CatalogService(
        InMemoryCatalogRepository(
            [base.model_copy(update={"active_ingredients": ingredients})]
        )
    )

    result = matching_service.match_products(
        ProductMatchQuery(
            required_ingredients=[
                IngredientTarget(
                    name="magnesium",
                    maximum_amount="40",
                    unit="mg",
                )
            ]
        )
    )

    assert result.matches[0].not_comparable == ["magnesium"]


def test_reports_unquantified_incompatible_and_inequality_rows() -> None:
    base = service().get_product("example:magnesium-glycinate")
    assert base is not None
    ingredients = [
        *base.active_ingredients,
        Ingredient(canonical_name="zinc", label_name="Zinc"),
        Ingredient(
            canonical_name="calcium",
            label_name="Calcium",
            amount="100",
            unit="IU",
        ),
        Ingredient(
            canonical_name="iron",
            label_name="Iron",
            amount="10",
            unit="mg",
            amount_operator="<",
        ),
    ]
    matching_service = CatalogService(
        InMemoryCatalogRepository(
            [base.model_copy(update={"active_ingredients": ingredients})]
        )
    )

    result = matching_service.match_products(
        ProductMatchQuery(
            required_ingredients=[IngredientTarget(name="magnesium")],
            preferred_ingredients=[
                IngredientTarget(name="zinc", minimum_amount="1", unit="mg"),
                IngredientTarget(name="calcium", minimum_amount="1", unit="mg"),
                IngredientTarget(name="iron", minimum_amount="1", unit="mg"),
            ],
        )
    )

    match = result.matches[0]
    assert match.unquantified == ["zinc"]
    assert match.unit_mismatches == ["calcium"]
    assert match.not_comparable == ["iron"]


def test_bounds_candidate_pagination_with_stable_product_ids() -> None:
    base = service().get_product("example:magnesium-glycinate")
    assert base is not None
    products = [
        base.model_copy(
            update={
                "id": f"example:duplicate-{index:03}",
                "source_product_id": f"duplicate-{index:03}",
                "name": "Duplicate Name",
                "brand": "Duplicate Brand",
            }
        )
        for index in range(60)
    ]
    matching_service = CatalogService(InMemoryCatalogRepository(products))

    result = matching_service.match_products(
        ProductMatchQuery(
            required_ingredients=[IngredientTarget(name="magnesium")],
            limit=50,
            candidate_limit=50,
        )
    )

    assert result.candidate_total == 60
    assert result.evaluated_count == 50
    assert result.truncated is True
    assert [match.product.id for match in result.matches] == [
        f"example:duplicate-{index:03}" for index in range(50)
    ]
