import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from suparch.models import Product


@dataclass(slots=True)
class EnrichmentStats:
    matched: int = 0


def enrich_iherb_with_dsld(
    iherb_products: Iterable[Product],
    dsld_products: Iterable[Product],
    *,
    stats: EnrichmentStats | None = None,
) -> Iterator[Product]:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as spool:
        candidate_upcs: set[str] = set()
        for product in iherb_products:
            if product.source != "iherb":
                raise ValueError(
                    f"Expected iHerb product, got {product.source}:{product.id}"
                )
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
                yield product
                continue
            if stats is not None:
                stats.matched += 1
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
            yield product.model_copy(
                deep=True,
                update={
                    "supplement_form": (
                        product.supplement_form or match.supplement_form
                    ),
                    "product_type": product.product_type or match.product_type,
                    "target_groups": product.target_groups or match.target_groups,
                    "serving_size": product.serving_size or match.serving_size,
                    "servings_per_container": (
                        product.servings_per_container
                        or match.servings_per_container
                    ),
                    "active_ingredients": (
                        product.active_ingredients or match.active_ingredients
                    ),
                    "other_ingredients": (
                        product.other_ingredients or match.other_ingredients
                    ),
                    "parser_version": parser_version,
                    "parser_confidence": min(
                        product.parser_confidence,
                        match.parser_confidence,
                    ),
                },
            )


def _upc_match_key(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(character for character in value if character.isdigit())
    return digits.lstrip("0") or "0"
