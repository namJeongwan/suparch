import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from suparch.barcodes import canonicalize_gtin
from suparch.models import Product


@dataclass(slots=True)
class EnrichmentStats:
    total: int = 0
    matched: int = 0
    labeled: int = 0
    skipped_without_label: int = 0

    @property
    def match_coverage(self) -> float:
        return self.matched / self.total if self.total else 0.0

    @property
    def label_coverage(self) -> float:
        return self.labeled / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "total": self.total,
            "matched": self.matched,
            "labeled": self.labeled,
            "skipped_without_label": self.skipped_without_label,
            "match_coverage": round(self.match_coverage, 6),
            "label_coverage": round(self.label_coverage, 6),
        }


def enrich_products_with_dsld(
    retail_products: Iterable[Product],
    dsld_products: Iterable[Product],
    *,
    stats: EnrichmentStats | None = None,
    require_label: bool = False,
) -> Iterator[Product]:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as spool:
        candidate_upcs: set[str] = set()
        for product in retail_products:
            if product.source == "dsld":
                raise ValueError("DSLD labels cannot be enriched as retail offers")
            if stats is not None:
                stats.total += 1
            spool.write(product.model_dump_json() + "\n")
            key = _upc_match_key(product.upc)
            if key:
                candidate_upcs.add(key)

        dsld_by_upc: dict[str, Product] = {}
        for product in dsld_products:
            if product.source != "dsld" or not product.upc:
                continue
            key = _upc_match_key(product.upc)
            if key not in candidate_upcs:
                continue
            current = dsld_by_upc.get(key)
            if current is None or (
                product.on_market is True and current.on_market is not True
            ):
                dsld_by_upc[key] = product

        spool.seek(0)
        for line in spool:
            product = Product.model_validate_json(line)
            match = dsld_by_upc.get(_upc_match_key(product.upc))
            if match is None:
                if require_label and not product.active_ingredients:
                    if stats is not None:
                        stats.skipped_without_label += 1
                    continue
                if stats is not None and product.active_ingredients:
                    stats.labeled += 1
                yield product
                continue
            if stats is not None:
                stats.matched += 1
            use_dsld_label = not product.active_ingredients
            parser_version = ";".join(
                filter(
                    None,
                    [
                        product.parser_version,
                        (
                            f"dsld:{match.source_product_id}:"
                            f"{match.parser_version or 'unknown'}"
                        ),
                    ],
                )
            )
            enriched_product = product.model_copy(
                deep=True,
                update={
                    "supplement_form": (
                        product.supplement_form or match.supplement_form
                    ),
                    "product_type": product.product_type or match.product_type,
                    "target_groups": (
                        match.target_groups
                        if use_dsld_label
                        else product.target_groups
                    ),
                    "serving_size": (
                        match.serving_size
                        if use_dsld_label
                        else product.serving_size
                    ),
                    "servings_per_container": (
                        match.servings_per_container
                        if use_dsld_label
                        else product.servings_per_container
                    ),
                    "active_ingredients": (
                        match.active_ingredients
                        if use_dsld_label
                        else product.active_ingredients
                    ),
                    "other_ingredients": (
                        match.other_ingredients
                        if use_dsld_label
                        else product.other_ingredients
                    ),
                    "parser_version": parser_version,
                    "parser_confidence": min(
                        product.parser_confidence,
                        match.parser_confidence,
                    ),
                },
            )
            if require_label and not enriched_product.active_ingredients:
                if stats is not None:
                    stats.skipped_without_label += 1
                continue
            if stats is not None and enriched_product.active_ingredients:
                stats.labeled += 1
            yield enriched_product


def enrich_iherb_with_dsld(
    iherb_products: Iterable[Product],
    dsld_products: Iterable[Product],
    *,
    stats: EnrichmentStats | None = None,
    require_label: bool = False,
) -> Iterator[Product]:
    """Backward-compatible wrapper for the original iHerb-only pipeline."""
    def validated_products() -> Iterator[Product]:
        for product in iherb_products:
            if product.source != "iherb":
                raise ValueError(
                    f"Expected iHerb product, got {product.source}:{product.id}"
                )
            yield product

    yield from enrich_products_with_dsld(
        validated_products(),
        dsld_products,
        stats=stats,
        require_label=require_label,
    )


def enrichment_quality_failures(
    stats: EnrichmentStats,
    *,
    output_products: int,
    min_label_coverage: float = 0.0,
) -> list[str]:
    if not 0 <= min_label_coverage <= 1:
        raise ValueError("min_label_coverage must be between 0 and 1")

    failures: list[str] = []
    if output_products == 0:
        failures.append("enrichment produced no products")
    if stats.label_coverage < min_label_coverage:
        failures.append(
            f"label coverage {stats.label_coverage:.2%} is below minimum "
            f"{min_label_coverage:.2%}"
        )
    return failures


def _upc_match_key(value: str | None) -> str:
    return canonicalize_gtin(value) or ""
