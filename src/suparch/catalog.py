import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.request
from pathlib import Path

from pydantic import TypeAdapter

from suparch.models import Product

SCHEMA_VERSION = 2
REQUIRED_COLUMNS = {
    "metadata": {"key", "value"},
    "products": {
        "id",
        "source",
        "source_product_id",
        "name",
        "brand",
        "product_url",
        "crawled_at",
    },
    "product_ingredients": {
        "product_id",
        "position",
        "canonical_name",
        "label_name",
        "parent_ingredient",
    },
    "other_ingredients": {"product_id", "position", "name"},
    "product_search": {"product_id", "name", "brand", "ingredients", "forms"},
}

SCHEMA = """
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE products (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    serving_size TEXT,
    servings_per_container TEXT,
    price_amount TEXT,
    price_currency TEXT,
    product_url TEXT NOT NULL,
    crawled_at TEXT NOT NULL,
    locale TEXT,
    parser_version TEXT,
    parser_confidence TEXT NOT NULL
);

CREATE TABLE product_ingredients (
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    canonical_name TEXT NOT NULL,
    label_name TEXT NOT NULL,
    form TEXT,
    amount TEXT,
    unit TEXT,
    normalized_amount TEXT,
    normalized_unit TEXT,
    daily_value_percent TEXT,
    raw_text TEXT,
    parent_ingredient TEXT,
    confidence TEXT NOT NULL,
    PRIMARY KEY (product_id, position)
);

CREATE INDEX idx_product_ingredients_name
ON product_ingredients(canonical_name, product_id);

CREATE INDEX idx_product_ingredients_form
ON product_ingredients(form, product_id);

CREATE TABLE other_ingredients (
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (product_id, position)
);

CREATE VIRTUAL TABLE product_search USING fts5(
    product_id UNINDEXED,
    name,
    brand,
    ingredients,
    forms,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def load_json_catalog(path: Path) -> list[Product]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = [payload]
    return TypeAdapter(list[Product]).validate_python(payload)


class SQLiteCatalogBuilder:
    """Build immutable catalog snapshots and publish them with an atomic rename."""

    def build(
        self,
        products: list[Product],
        output: Path,
        *,
        metadata: dict[str, str] | None = None,
    ) -> Path:
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)

        try:
            self._write(products, temporary, metadata or {})
            validate_catalog(temporary)
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return output

    @staticmethod
    def _write(
        products: list[Product],
        output: Path,
        metadata: dict[str, str],
    ) -> None:
        with sqlite3.connect(output) as connection:
            connection.executescript(SCHEMA)
            base_metadata = {
                "schema_version": str(SCHEMA_VERSION),
                "product_count": str(len(products)),
            }
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [*base_metadata.items(), *metadata.items()],
            )

            for product in products:
                price_amount = str(product.price.amount) if product.price else None
                price_currency = product.price.currency if product.price else None
                connection.execute(
                    """
                    INSERT INTO products(
                        id, source, source_product_id, name, brand,
                        serving_size, servings_per_container,
                        price_amount, price_currency, product_url,
                        crawled_at, locale, parser_version, parser_confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product.id,
                        product.source,
                        product.source_product_id,
                        product.name,
                        product.brand,
                        product.serving_size,
                        (
                            str(product.servings_per_container)
                            if product.servings_per_container is not None
                            else None
                        ),
                        price_amount,
                        price_currency,
                        str(product.product_url),
                        product.crawled_at.isoformat(),
                        product.locale,
                        product.parser_version,
                        str(product.parser_confidence),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO product_ingredients(
                        product_id, position, canonical_name, label_name, form,
                        amount, unit, normalized_amount, normalized_unit,
                        daily_value_percent, raw_text, parent_ingredient, confidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            product.id,
                            position,
                            ingredient.canonical_name,
                            ingredient.label_name,
                            ingredient.form,
                            (
                                str(ingredient.amount)
                                if ingredient.amount is not None
                                else None
                            ),
                            ingredient.unit,
                            (
                                str(ingredient.normalized_amount)
                                if ingredient.normalized_amount is not None
                                else None
                            ),
                            ingredient.normalized_unit,
                            (
                                str(ingredient.daily_value_percent)
                                if ingredient.daily_value_percent is not None
                                else None
                            ),
                            ingredient.raw_text,
                            ingredient.parent_ingredient,
                            str(ingredient.confidence),
                        )
                        for position, ingredient in enumerate(
                            product.active_ingredients
                        )
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO other_ingredients(product_id, position, name)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (product.id, position, name)
                        for position, name in enumerate(product.other_ingredients)
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO product_search(
                        product_id, name, brand, ingredients, forms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        product.id,
                        product.name,
                        product.brand,
                        " ".join(
                            f"{ingredient.canonical_name} {ingredient.label_name}"
                            for ingredient in product.active_ingredients
                        ),
                        " ".join(
                            ingredient.form or ""
                            for ingredient in product.active_ingredients
                        ),
                    ),
                )

            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {result}")
            connection.execute("VACUUM")


def download_catalog(
    url: str,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> Path:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".download",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Suparch/0.1 catalog-downloader"},
        )
        with (
            urllib.request.urlopen(request, timeout=60) as response,  # noqa: S310
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)

        if expected_sha256:
            with temporary.open("rb") as catalog_file:
                actual = hashlib.file_digest(catalog_file, "sha256").hexdigest()
            if actual.casefold() != expected_sha256.casefold():
                raise ValueError(
                    f"Catalog checksum mismatch: expected {expected_sha256}, got {actual}"
                )

        validate_catalog(temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def validate_catalog(path: Path) -> None:
    resolved = path.resolve()
    with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Catalog integrity check failed: {integrity}")

        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported catalog schema version: {version}; "
                f"expected {SCHEMA_VERSION}"
            )

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        missing_tables = set(REQUIRED_COLUMNS) - tables
        if missing_tables:
            raise RuntimeError(
                f"Catalog is missing required tables: {sorted(missing_tables)}"
            )

        for table, required_columns in REQUIRED_COLUMNS.items():
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            missing_columns = required_columns - columns
            if missing_columns:
                raise RuntimeError(
                    f"Catalog table {table} is missing columns: "
                    f"{sorted(missing_columns)}"
                )
