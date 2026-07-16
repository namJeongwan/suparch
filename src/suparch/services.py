from suparch.models import (
    CatalogInfo,
    ComparisonEntry,
    IngredientComparison,
    Product,
    ProductComparisonResult,
    ProductSearchQuery,
    ProductSearchResult,
    ProductSummary,
    StackContribution,
    StackResult,
    StackSelection,
    StackTotal,
)
from suparch.normalization import normalize_amount
from suparch.repositories import CatalogRepository


class CatalogService:
    def __init__(self, repository: CatalogRepository) -> None:
        self._repository = repository

    def get_product(self, product_id: str) -> Product | None:
        return self._repository.get_product(product_id)

    def catalog_info(self) -> CatalogInfo:
        return self._repository.catalog_info()

    def search_products(self, search: ProductSearchQuery) -> ProductSearchResult:
        return self._repository.search_products(search)

    def compare_products(self, product_ids: list[str]) -> ProductComparisonResult:
        products = self._require_products(product_ids)
        by_ingredient: dict[str, list[ComparisonEntry]] = {}
        for product in products:
            for ingredient in product.active_ingredients:
                by_ingredient.setdefault(ingredient.canonical_name, []).append(
                    ComparisonEntry(
                        product_id=product.id,
                        product_name=product.name,
                        label_name=ingredient.label_name,
                        form=ingredient.form,
                        amount=ingredient.amount,
                        unit=ingredient.unit,
                        amount_operator=ingredient.amount_operator,
                        daily_value_percent=ingredient.daily_value_percent,
                        daily_value_operator=ingredient.daily_value_operator,
                        daily_values=ingredient.daily_values,
                    )
                )

        ingredients = [
            IngredientComparison(canonical_name=name, entries=entries)
            for name, entries in sorted(by_ingredient.items())
        ]
        common = [
            ingredient.canonical_name
            for ingredient in ingredients
            if len({entry.product_id for entry in ingredient.entries}) == len(products)
        ]
        return ProductComparisonResult(
            products=[ProductSummary.from_product(product) for product in products],
            ingredients=ingredients,
            common_ingredients=common,
        )

    def calculate_stack(self, selections: list[StackSelection]) -> StackResult:
        if not selections:
            return StackResult(products=[], totals=[], duplicate_ingredients=[])

        product_ids = [selection.product_id for selection in selections]
        products = self._require_products(product_ids)
        products_by_id = {product.id: product for product in products}
        totals: dict[tuple[str, str], list[StackContribution]] = {}

        for selection in selections:
            product = products_by_id[selection.product_id]
            for ingredient in product.active_ingredients:
                if ingredient.amount_operator not in {None, "", "="}:
                    continue
                amount = ingredient.normalized_amount
                unit = ingredient.normalized_unit
                if amount is None or unit is None:
                    amount, unit = normalize_amount(
                        ingredient.amount,
                        ingredient.unit,
                    )
                if amount is None or unit is None:
                    amount = ingredient.amount
                    unit = ingredient.unit
                if amount is None or unit is None:
                    continue
                contribution = StackContribution(
                    product_id=product.id,
                    product_name=product.name,
                    servings_per_day=selection.servings_per_day,
                    amount=amount * selection.servings_per_day,
                    unit=unit,
                )
                totals.setdefault((ingredient.canonical_name, unit), []).append(
                    contribution
                )

        stack_totals = [
            StackTotal(
                canonical_name=canonical_name,
                total_amount=sum(
                    (contribution.amount for contribution in contributions),
                    start=contributions[0].amount * 0,
                ),
                unit=unit,
                contributions=contributions,
            )
            for (canonical_name, unit), contributions in sorted(totals.items())
        ]
        product_ids_by_ingredient: dict[str, set[str]] = {}
        for total in stack_totals:
            product_ids_by_ingredient.setdefault(total.canonical_name, set()).update(
                contribution.product_id for contribution in total.contributions
            )
        duplicate_names = sorted(
            name
            for name, contributing_products in product_ids_by_ingredient.items()
            if len(contributing_products) > 1
        )
        return StackResult(
            products=[ProductSummary.from_product(product) for product in products],
            totals=stack_totals,
            duplicate_ingredients=duplicate_names,
        )

    def _require_products(self, product_ids: list[str]) -> list[Product]:
        unique_ids = list(dict.fromkeys(product_ids))
        products = self._repository.get_products(unique_ids)
        found = {product.id for product in products}
        missing = [product_id for product_id in unique_ids if product_id not in found]
        if missing:
            raise ValueError(f"Unknown product_id(s): {', '.join(missing)}")
        return products
