import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from suparch.catalog import (
    SQLiteCatalogBuilder,
    load_catalog_inputs,
    load_json_catalog,
    write_catalog_artifacts,
)
from suparch.models import ProductSearchQuery
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

    assert manifest["schema_version"] == 2
    assert manifest["product_count"] == 3
    assert manifest["sha256"] == checksum_path.read_text(encoding="utf-8").split()[0]


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


def test_reports_sqlite_catalog_info(database: Path) -> None:
    info = SqliteCatalogRepository(database).catalog_info()

    assert info.schema_version == 2
    assert info.product_count == 3
    assert info.source == "sqlite"
    assert info.database_bytes is not None
