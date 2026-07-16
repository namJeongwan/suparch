import json
from pathlib import Path

from suparch.catalog import load_json_catalog
from suparch.dsld import DsldProductMapper
from suparch.enrichment import EnrichmentStats, enrich_iherb_with_dsld
from suparch.models import Product

SAMPLE_CATALOG = (
    Path(__file__).parents[1] / "src" / "suparch" / "data" / "sample_catalog.json"
)
DSLD_FIXTURE = Path(__file__).parent / "fixtures" / "dsld_label.json"


def test_enriches_by_upc_without_losing_iherb_identity() -> None:
    iherb = Product.model_validate(
        {
            **load_json_catalog(SAMPLE_CATALOG)[0].model_dump(mode="json"),
            "id": "iherb:19279",
            "source": "iherb",
            "source_product_id": "19279",
            "upc": "00012345678905",
            "active_ingredients": [],
            "other_ingredients": [],
            "product_url": "https://www.iherb.com/pr/example/19279",
        }
    )
    dsld = DsldProductMapper().map_label(
        json.loads(DSLD_FIXTURE.read_text(encoding="utf-8"))
    )

    stats = EnrichmentStats()
    enriched = list(
        enrich_iherb_with_dsld([iherb], [dsld], stats=stats)
    )[0]

    assert enriched.id == "iherb:19279"
    assert enriched.source == "iherb"
    assert str(enriched.product_url).startswith("https://www.iherb.com/pr/")
    assert enriched.price == iherb.price
    assert enriched.active_ingredients[0].canonical_name == "magnesium"
    assert "dsld:19279" in (enriched.parser_version or "")
    assert stats.matched == 1


def test_keeps_first_same_status_dsld_match_for_duplicate_upc() -> None:
    iherb = Product.model_validate(
        {
            **load_json_catalog(SAMPLE_CATALOG)[0].model_dump(mode="json"),
            "id": "iherb:19279",
            "source": "iherb",
            "source_product_id": "19279",
            "upc": "012345678905",
            "active_ingredients": [],
            "other_ingredients": [],
            "product_url": "https://www.iherb.com/pr/example/19279",
        }
    )
    newest = DsldProductMapper().map_label(
        json.loads(DSLD_FIXTURE.read_text(encoding="utf-8"))
    )
    older = newest.model_copy(
        deep=True,
        update={
            "source_product_id": "older",
            "active_ingredients": [],
        },
    )

    enriched = list(enrich_iherb_with_dsld([iherb], [newest, older]))[0]

    assert enriched.active_ingredients[0].canonical_name == "magnesium"
    assert "dsld:19279" in (enriched.parser_version or "")


def test_does_not_mix_iherb_and_dsld_label_bundles() -> None:
    iherb = Product.model_validate(
        {
            **load_json_catalog(SAMPLE_CATALOG)[0].model_dump(mode="json"),
            "id": "iherb:19279",
            "source": "iherb",
            "source_product_id": "19279",
            "upc": "012345678905",
            "product_url": "https://www.iherb.com/pr/example/19279",
        }
    )
    dsld = DsldProductMapper().map_label(
        json.loads(DSLD_FIXTURE.read_text(encoding="utf-8"))
    )

    enriched = list(enrich_iherb_with_dsld([iherb], [dsld]))[0]

    assert enriched.serving_size == iherb.serving_size
    assert enriched.servings_per_container == iherb.servings_per_container
    assert enriched.active_ingredients == iherb.active_ingredients
    assert enriched.other_ingredients == iherb.other_ingredients
