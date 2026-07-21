from decimal import Decimal

from suparch.models import (
    CatalogInfo,
    ComparisonEntry,
    Ingredient,
    IngredientComparison,
    IngredientFit,
    IngredientObservation,
    IngredientTarget,
    Product,
    ProductComparisonResult,
    ProductMatch,
    ProductMatchQuery,
    ProductMatchResult,
    ProductSearchQuery,
    ProductSearchResult,
    ProductSummary,
    StackContribution,
    StackResult,
    StackSelection,
    StackTotal,
)
from suparch.normalization import (
    canonicalize_ingredient,
    normalize_amount,
    normalize_unit,
)
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

    def match_products(self, request: ProductMatchQuery) -> ProductMatchResult:
        """Rank labels against caller-supplied targets without making medical claims."""
        required = _canonical_targets(request.required_ingredients)
        preferred = _canonical_targets(request.preferred_ingredients)
        overlap = set(required) & set(preferred)
        if overlap:
            raise ValueError(
                "Ingredients cannot be both required and preferred: "
                + ", ".join(sorted(overlap))
            )

        candidate_search = ProductSearchQuery(
            query=request.query,
            on_market=request.on_market,
            supplement_forms=request.supplement_forms,
            product_types=request.product_types,
            target_groups=request.target_groups,
            include_ingredients=list(required),
            exclude_ingredients=request.exclude_ingredients,
            brands=request.brands,
            max_price=request.max_price,
            currency=request.currency,
            limit=50,
        )
        first_page = self._repository.search_products(candidate_search)
        candidate_total = first_page.total
        summaries = list(first_page.products)
        evaluated_count = min(candidate_total, request.candidate_limit)
        offset = len(summaries)
        while offset < evaluated_count:
            page_size = min(50, evaluated_count - offset)
            page = self._repository.search_products(
                candidate_search.model_copy(
                    update={"limit": page_size, "offset": offset}
                )
            )
            if not page.products:
                break
            summaries.extend(page.products)
            offset += len(page.products)

        products = self._repository.get_products(
            [summary.id for summary in summaries[:evaluated_count]]
        )
        matches = [
            _match_product(product, required=required, preferred=preferred)
            for product in products
        ]
        matches.sort(
            key=lambda match: (
                -match.score,
                len(match.missing_ingredients),
                len(match.below_minimum) + len(match.above_maximum),
                match.product.brand.casefold(),
                match.product.name.casefold(),
                match.product.id,
            )
        )
        return ProductMatchResult(
            matches=matches[: request.limit],
            candidate_total=candidate_total,
            evaluated_count=len(products),
            truncated=candidate_total > len(products),
        )

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
                amount, unit = normalize_amount(
                    ingredient.amount,
                    ingredient.unit,
                )
                if amount is None or unit is None:
                    amount = ingredient.normalized_amount
                    unit = ingredient.normalized_unit
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


def _canonical_targets(
    targets: list[IngredientTarget],
) -> dict[str, IngredientTarget]:
    canonical: dict[str, IngredientTarget] = {}
    for target in targets:
        name = canonicalize_ingredient(target.name)[0]
        if name in canonical:
            raise ValueError(f"Duplicate ingredient target: {name}")
        if target.unit:
            _, normalized_unit = normalize_amount(
                Decimal("1"),
                normalize_unit(target.unit),
            )
            if normalized_unit is None:
                raise ValueError(f"Unsupported ingredient target unit: {target.unit}")
        canonical[name] = target
    return canonical


def _match_product(
    product: Product,
    *,
    required: dict[str, IngredientTarget],
    preferred: dict[str, IngredientTarget],
) -> ProductMatch:
    fits = [
        _ingredient_fit(product, name, target, required=True)
        for name, target in required.items()
    ]
    fits.extend(
        _ingredient_fit(product, name, target, required=False)
        for name, target in preferred.items()
    )

    required_matches = sum(fit.required and fit.status == "matched" for fit in fits)
    preferred_matches = sum(
        not fit.required and fit.status == "matched" for fit in fits
    )
    possible_points = len(required) * 2 + len(preferred)
    earned_points = required_matches * 2 + preferred_matches
    score = (
        (Decimal(earned_points) * 100 / Decimal(possible_points)).quantize(
            Decimal("0.01")
        )
        if possible_points
        else Decimal("0")
    )

    return ProductMatch(
        product=ProductSummary.from_product(product),
        score=score,
        required_matches=required_matches,
        preferred_matches=preferred_matches,
        missing_ingredients=_fit_names(fits, "missing"),
        below_minimum=_fit_names(fits, "below_minimum"),
        above_maximum=_fit_names(fits, "above_maximum"),
        unquantified=_fit_names(fits, "unquantified"),
        unit_mismatches=_fit_names(fits, "unit_mismatch"),
        not_comparable=_fit_names(fits, "not_comparable"),
        fits=fits,
    )


def _ingredient_fit(
    product: Product,
    canonical_name: str,
    target: IngredientTarget,
    *,
    required: bool,
) -> IngredientFit:
    ingredients = [
        ingredient
        for ingredient in product.active_ingredients
        if ingredient.canonical_name == canonical_name
    ]
    observations = [
        IngredientObservation(
            label_name=ingredient.label_name,
            form=ingredient.form,
            amount=ingredient.amount,
            unit=ingredient.unit,
            amount_operator=ingredient.amount_operator,
        )
        for ingredient in ingredients
    ]
    if not ingredients:
        status = "missing"
    elif (
        target.minimum_amount is None
        and target.maximum_amount is None
        and target.unit is None
    ):
        status = "matched"
    else:
        status = _quantified_fit_status(ingredients, target)
    return IngredientFit(
        canonical_name=canonical_name,
        required=required,
        minimum_amount=target.minimum_amount,
        maximum_amount=target.maximum_amount,
        unit=target.unit,
        status=status,
        observations=observations,
    )


def _quantified_fit_status(
    ingredients: list[Ingredient],
    target: IngredientTarget,
) -> str:
    target_unit = normalize_unit(target.unit) if target.unit else None
    normalized_minimum, normalized_unit = normalize_amount(
        target.minimum_amount,
        target_unit,
    )
    normalized_maximum, maximum_unit = normalize_amount(
        target.maximum_amount,
        target_unit,
    )
    _, unit_only = normalize_amount(Decimal("1"), target_unit)
    if target.unit and unit_only is None:
        raise ValueError(f"Unsupported ingredient target unit: {target.unit}")
    comparison_unit = normalized_unit or maximum_unit or unit_only

    exact_rows: list[tuple[Ingredient, Decimal]] = []
    unquantified_rows: list[Ingredient] = []
    unit_mismatch_rows: list[Ingredient] = []
    non_equality_rows: list[Ingredient] = []
    for ingredient in ingredients:
        amount, unit = normalize_amount(ingredient.amount, ingredient.unit)
        if amount is None or unit is None:
            amount = ingredient.normalized_amount
            unit = ingredient.normalized_unit
        if amount is None or unit is None:
            unquantified_rows.append(ingredient)
            continue
        if comparison_unit and unit != comparison_unit:
            unit_mismatch_rows.append(ingredient)
            continue
        if ingredient.amount_operator not in {None, "", "="}:
            non_equality_rows.append(ingredient)
            continue
        exact_rows.append((ingredient, amount))

    if exact_rows:
        nested_exact = [
            amount
            for ingredient, amount in exact_rows
            if ingredient.parent_ingredient == ingredient.canonical_name
        ]
        top_level_exact = [
            amount
            for ingredient, amount in exact_rows
            if ingredient.parent_ingredient != ingredient.canonical_name
        ]
        nested_problem_rows = [
            ingredient
            for ingredient in (
                unquantified_rows + unit_mismatch_rows + non_equality_rows
            )
            if ingredient.parent_ingredient == ingredient.canonical_name
        ]
        top_level_problem_rows = [
            ingredient
            for ingredient in (
                unquantified_rows + unit_mismatch_rows + non_equality_rows
            )
            if ingredient.parent_ingredient != ingredient.canonical_name
        ]
        if nested_exact:
            if nested_problem_rows or len(top_level_exact) + len(
                top_level_problem_rows
            ) > 1:
                return "not_comparable"
            exact_amounts = nested_exact
        else:
            if top_level_problem_rows or (
                nested_problem_rows and len(top_level_exact) != 1
            ):
                return "not_comparable"
            exact_amounts = top_level_exact
        total_amount = sum(exact_amounts, start=Decimal("0"))
        if (
            normalized_minimum is None or total_amount >= normalized_minimum
        ) and (
            normalized_maximum is None or total_amount <= normalized_maximum
        ):
            return "matched"
        if normalized_minimum is not None and total_amount < normalized_minimum:
            return "below_minimum"
        if normalized_maximum is not None and total_amount > normalized_maximum:
            return "above_maximum"
        return "not_comparable"
    if non_equality_rows:
        return "not_comparable"
    if unit_mismatch_rows:
        return "unit_mismatch"
    if unquantified_rows:
        return "unquantified"
    return "unquantified"


def _fit_names(fits: list[IngredientFit], status: str) -> list[str]:
    return sorted(fit.canonical_name for fit in fits if fit.status == status)
