import json
import sqlite3
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest

from suparch.catalog import (
    SQLiteCatalogBuilder,
    fetch_catalog_manifest_sha256,
    fetch_catalog_pointer,
    load_catalog_inputs,
    load_json_catalog,
    validate_catalog,
    write_catalog_artifacts,
)
from suparch.models import OfferContext, ProductSearchQuery
from suparch.repositories import SqliteCatalogRepository

SAMPLE_CATALOG = (
    Path(__file__).parents[1] / "src" / "suparch" / "data" / "sample_catalog.json"
)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.sqlite"
    SQLiteCatalogBuilder().build(load_json_catalog(SAMPLE_CATALOG), path)
    return path


def test_builds_and_searches_read_only_catalog(database: Path) -> None:
    repository = SqliteCatalogRepository(database)

    result = repository.search_products(
        ProductSearchQuery(
            query="magnesium",
            include_ingredients=["magnesium"],
            exclude_ingredients=["calcium"],
        )
    )

    assert result.total == 1
    assert result.products[0].id == "example:magnesium-glycinate"


def test_searches_catalog_with_fts_prefix(database: Path) -> None:
    repository = SqliteCatalogRepository(database)

    result = repository.search_products(ProductSearchQuery(query="vita"))

    assert [product.id for product in result.products] == ["example:vitamin-d3"]


def test_catalog_passes_integrity_check_and_rejects_writes(database: Path) -> None:
    uri = f"file:{database.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM products")


def test_price_filter_is_numeric_not_lexicographic(tmp_path: Path) -> None:
    products = load_json_catalog(SAMPLE_CATALOG)
    assert products[0].price is not None
    products[0].price.amount = Decimal("100")
    database = tmp_path / "prices.sqlite"
    SQLiteCatalogBuilder().build(products, database)

    result = SqliteCatalogRepository(database).search_products(
        ProductSearchQuery(max_price="20", currency="USD")
    )

    assert "example:magnesium-glycinate" not in {
        product.id for product in result.products
    }


def test_rejects_foreign_sqlite_database(tmp_path: Path) -> None:
    database = tmp_path / "foreign.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(RuntimeError, match="schema version"):
        SqliteCatalogRepository(database)


def test_rejects_schema_missing_a_consumed_column(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "ALTER TABLE product_ingredients DROP COLUMN daily_value_operator"
        )

    with pytest.raises(RuntimeError, match="daily_value_operator"):
        validate_catalog(database)


def test_lists_catalog_larger_than_sqlite_variable_limit(tmp_path: Path) -> None:
    template = load_json_catalog(SAMPLE_CATALOG)[0]
    products = [
        template.model_copy(
            deep=True,
            update={
                "id": f"example:product-{index}",
                "source_product_id": f"product-{index}",
                "name": f"Product {index}",
            },
        )
        for index in range(1100)
    ]
    database = tmp_path / "large.sqlite"
    SQLiteCatalogBuilder().build(products, database)

    assert len(SqliteCatalogRepository(database).list_products()) == 1100


def test_writes_catalog_manifest_and_checksum(database: Path) -> None:
    manifest_path, checksum_path = write_catalog_artifacts(database)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 4
    assert manifest["product_count"] == 3
    assert manifest["sha256"] == checksum_path.read_text(encoding="utf-8").split()[0]


def test_reads_checksum_from_remote_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksum = "a" * 64

    class Response(BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        "suparch.catalog.urllib.request.urlopen",
        lambda request, timeout: Response(
            json.dumps({"sha256": checksum}).encode()
        ),
    )

    assert (
        fetch_catalog_manifest_sha256(
            "https://example.com/catalog.sqlite.manifest.json"
        )
        == checksum
    )


def test_reads_immutable_catalog_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksum = "b" * 64

    class Response(BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        "suparch.catalog.urllib.request.urlopen",
        lambda request, timeout: Response(
            json.dumps(
                {
                    "catalog_url": (
                        "https://github.com/example/releases/download/"
                        "catalog-1/suparch-catalog.sqlite"
                    ),
                    "sha256": checksum,
                    "schema_version": 4,
                }
            ).encode()
        ),
    )

    assert fetch_catalog_pointer(
        "https://example.com/catalog-pointer.json"
    ) == (
        (
            "https://github.com/example/releases/download/"
            "catalog-1/suparch-catalog.sqlite"
        ),
        checksum,
        4,
    )


def test_loads_and_merges_jsonl_inputs(tmp_path: Path) -> None:
    products = load_json_catalog(SAMPLE_CATALOG)
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.json"
    first.write_text(
        products[0].model_dump_json() + "\n" + products[1].model_dump_json() + "\n",
        encoding="utf-8",
    )
    second.write_text(products[2].model_dump_json(), encoding="utf-8")

    loaded = load_catalog_inputs([first, second])

    assert {product.id for product in loaded} == {
        "example:magnesium-glycinate",
        "example:calcium-magnesium",
        "example:vitamin-d3",
    }


def test_streaming_builder_replaces_duplicate_product_ids(tmp_path: Path) -> None:
    products = load_json_catalog(SAMPLE_CATALOG)
    replacement = products[0].model_copy(
        update={"name": "Replacement Magnesium"}
    )
    database = tmp_path / "duplicates.sqlite"

    SQLiteCatalogBuilder().build(
        iter([products[0], products[1], replacement]),
        database,
    )
    repository = SqliteCatalogRepository(database)

    assert repository.catalog_info().product_count == 2
    assert repository.get_product(products[0].id).name == "Replacement Magnesium"  # type: ignore[union-attr]


def test_filters_normalized_supplement_form(database: Path) -> None:
    product = load_json_catalog(SAMPLE_CATALOG)[0]
    product.supplement_form = "Capsule(s)"
    SQLiteCatalogBuilder().build([product], database)

    result = SqliteCatalogRepository(database).search_products(
        ProductSearchQuery(supplement_forms=["capsule s"])
    )

    assert result.total == 1


def test_filters_product_type_and_target_group(database: Path) -> None:
    product = load_json_catalog(SAMPLE_CATALOG)[0]
    product.product_type = "Multi-Vitamin and Mineral (MVM)"
    product.target_groups = ["Pregnant and Lactating"]
    SQLiteCatalogBuilder().build([product], database)

    result = SqliteCatalogRepository(database).search_products(
        ProductSearchQuery(
            product_types=["multi vitamin and mineral mvm"],
            target_groups=["pregnant and lactating"],
        )
    )

    assert result.total == 1


def test_reports_sqlite_catalog_info(database: Path) -> None:
    info = SqliteCatalogRepository(database).catalog_info()

    assert info.schema_version == 4
    assert info.product_count == 3
    assert info.source == "sqlite"
    assert info.database_bytes is not None


def test_round_trips_offer_location_and_fulfillment(tmp_path: Path) -> None:
    product = load_json_catalog(SAMPLE_CATALOG)[0]
    product.offer_context = OfferContext(
        location_id="01400943",
        fulfillment=["curbside", "delivery"],
    )
    database = tmp_path / "offer-context.sqlite"

    SQLiteCatalogBuilder().build([product], database)
    restored = SqliteCatalogRepository(database).get_product(product.id)

    assert restored is not None
    assert restored.offer_context is not None
    assert restored.offer_context.location_id == "01400943"
    assert restored.offer_context.fulfillment == ["curbside", "delivery"]
