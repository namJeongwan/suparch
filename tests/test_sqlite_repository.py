import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from suparch.catalog import SQLiteCatalogBuilder, load_json_catalog
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
