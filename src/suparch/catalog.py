import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from suparch.models import Product
from suparch.normalization import normalize_text

SCHEMA_VERSION = 4
REQUIRED_COLUMNS = {
    "metadata": {"key", "value"},
    "products": {
        "id",
        "source",
        "source_product_id",
        "name",
        "brand",
        "upc",
        "on_market",
        "supplement_form",
        "supplement_form_normalized",
        "product_type",
        "product_type_normalized",
        "target_groups_json",
        "serving_size",
        "servings_per_container",
        "price_amount",
        "price_currency",
        "offer_location_id",
        "fulfillment_json",
        "product_url",
        "crawled_at",
        "locale",
        "parser_version",
        "parser_confidence",
    },
    "product_ingredients": {
        "product_id",
        "position",
        "canonical_name",
        "label_name",
        "taxonomy_name",
        "form",
        "amount",
        "unit",
        "amount_operator",
        "normalized_amount",
        "normalized_unit",
        "daily_value_percent",
        "daily_value_operator",
        "daily_values_json",
        "raw_text",
        "parent_ingredient",
        "confidence",
    },
    "product_target_groups": {
        "product_id",
        "position",
        "name",
        "normalized_name",
    },
    "other_ingredients": {"product_id", "position", "name"},
    "product_search": {
        "product_id",
        "name",
        "brand",
        "upc",
        "product_type",
        "target_groups",
        "ingredients",
        "forms",
    },
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
    upc TEXT,
    on_market INTEGER,
    supplement_form TEXT,
    supplement_form_normalized TEXT,
    product_type TEXT,
    product_type_normalized TEXT,
    target_groups_json TEXT NOT NULL,
    serving_size TEXT,
    servings_per_container TEXT,
    price_amount TEXT,
    price_currency TEXT,
    offer_location_id TEXT,
    fulfillment_json TEXT NOT NULL,
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
    taxonomy_name TEXT,
    form TEXT,
    amount TEXT,
    unit TEXT,
    amount_operator TEXT,
    normalized_amount TEXT,
    normalized_unit TEXT,
    daily_value_percent TEXT,
    daily_value_operator TEXT,
    daily_values_json TEXT NOT NULL,
    raw_text TEXT,
    parent_ingredient TEXT,
    confidence TEXT NOT NULL,
    PRIMARY KEY (product_id, position)
);

CREATE INDEX idx_product_ingredients_name
ON product_ingredients(canonical_name, product_id);

CREATE INDEX idx_product_ingredients_form
ON product_ingredients(form, product_id);

CREATE INDEX idx_products_supplement_form
ON products(supplement_form_normalized, id);

CREATE INDEX idx_products_product_type
ON products(product_type_normalized, id);

CREATE TABLE product_target_groups (
    product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    PRIMARY KEY (product_id, position)
);

CREATE INDEX idx_product_target_groups_name
ON product_target_groups(normalized_name, product_id);

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
    upc,
    product_type,
    target_groups,
    ingredients,
    forms,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def iter_json_catalog(path: Path) -> Iterator[Product]:
    if path.suffix.casefold() == ".jsonl":
        adapter = TypeAdapter(Product)
        with path.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    yield adapter.validate_python(json.loads(line))
        return

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = [payload]
    yield from TypeAdapter(list[Product]).validate_python(payload)


def load_json_catalog(path: Path) -> list[Product]:
    return list(iter_json_catalog(path))


def load_catalog_inputs(paths: list[Path]) -> list[Product]:
    products_by_id: dict[str, Product] = {}
    for path in paths:
        for product in load_json_catalog(path):
            products_by_id[product.id] = product
    return list(products_by_id.values())


def iter_catalog_inputs(paths: list[Path]) -> Iterator[Product]:
    for path in paths:
        yield from iter_json_catalog(path)


class SQLiteCatalogBuilder:
    """Build immutable catalog snapshots and publish them with an atomic rename."""

    def build(
        self,
        products: Iterable[Product],
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
        products: Iterable[Product],
        output: Path,
        metadata: dict[str, str],
    ) -> None:
        with sqlite3.connect(output) as connection:
            connection.executescript(SCHEMA)
            base_metadata = {
                "schema_version": str(SCHEMA_VERSION),
            }
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [*base_metadata.items(), *metadata.items()],
            )

            product_count = 0
            seen_product_ids: set[str] = set()
            for product in products:
                if product.id in seen_product_ids:
                    connection.execute(
                        "DELETE FROM product_search WHERE product_id = ?",
                        (product.id,),
                    )
                    connection.execute(
                        "DELETE FROM products WHERE id = ?",
                        (product.id,),
                    )
                else:
                    product_count += 1
                    seen_product_ids.add(product.id)
                price_amount = str(product.price.amount) if product.price else None
                price_currency = product.price.currency if product.price else None
                connection.execute(
                    """
                    INSERT INTO products(
                        id, source, source_product_id, name, brand, upc,
                        on_market, supplement_form, supplement_form_normalized,
                        product_type, product_type_normalized, target_groups_json,
                        serving_size, servings_per_container,
                        price_amount, price_currency, offer_location_id,
                        fulfillment_json, product_url,
                        crawled_at, locale, parser_version, parser_confidence
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        product.id,
                        product.source,
                        product.source_product_id,
                        product.name,
                        product.brand,
                        product.upc,
                        (
                            int(product.on_market)
                            if product.on_market is not None
                            else None
                        ),
                        product.supplement_form,
                        normalize_text(product.supplement_form or ""),
                        product.product_type,
                        normalize_text(product.product_type or ""),
                        json.dumps(product.target_groups),
                        product.serving_size,
                        (
                            str(product.servings_per_container)
                            if product.servings_per_container is not None
                            else None
                        ),
                        price_amount,
                        price_currency,
                        (
                            product.offer_context.location_id
                            if product.offer_context
                            else None
                        ),
                        json.dumps(
                            product.offer_context.fulfillment
                            if product.offer_context
                            else []
                        ),
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
                        product_id, position, canonical_name, label_name,
                        taxonomy_name, form, amount, unit, amount_operator,
                        normalized_amount, normalized_unit, daily_value_percent,
                        daily_value_operator, daily_values_json, raw_text,
                        parent_ingredient, confidence
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        (
                            product.id,
                            position,
                            ingredient.canonical_name,
                            ingredient.label_name,
                            ingredient.taxonomy_name,
                            ingredient.form,
                            (
                                str(ingredient.amount)
                                if ingredient.amount is not None
                                else None
                            ),
                            ingredient.unit,
                            ingredient.amount_operator,
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
                            ingredient.daily_value_operator,
                            json.dumps(
                                [
                                    daily_value.model_dump(mode="json")
                                    for daily_value in ingredient.daily_values
                                ]
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
                    INSERT INTO product_target_groups(
                        product_id, position, name, normalized_name
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (product.id, position, name, normalize_text(name))
                        for position, name in enumerate(product.target_groups)
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
                        product_id, name, brand, upc, product_type,
                        target_groups, ingredients, forms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product.id,
                        product.name,
                        product.brand,
                        product.upc or "",
                        product.product_type or "",
                        " ".join(product.target_groups),
                        " ".join(
                            " ".join(
                                filter(
                                    None,
                                    [
                                        ingredient.canonical_name,
                                        ingredient.label_name,
                                        ingredient.taxonomy_name,
                                    ],
                                )
                            )
                            for ingredient in product.active_ingredients
                        ),
                        " ".join(
                            ingredient.form or ""
                            for ingredient in product.active_ingredients
                        ),
                    ),
                )

            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                ("product_count", str(product_count)),
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
            headers={"User-Agent": "Suparch/0.2 catalog-downloader"},
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


def fetch_catalog_manifest_sha256(url: str) -> str:
    payload = _fetch_small_json(url, "catalog-manifest-client")
    checksum = payload.get("sha256")
    return _validate_sha256(checksum, "Catalog manifest")


def fetch_catalog_pointer(url: str) -> tuple[str, str, int]:
    payload = _fetch_small_json(url, "catalog-pointer-client")
    catalog_url = payload.get("catalog_url")
    if (
        not isinstance(catalog_url, str)
        or urllib.parse.urlparse(catalog_url).scheme != "https"
    ):
        raise ValueError("Catalog pointer has no valid HTTPS catalog URL")
    checksum = _validate_sha256(payload.get("sha256"), "Catalog pointer")
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise ValueError("Catalog pointer has no valid schema version")
    return catalog_url, checksum, schema_version


def _fetch_small_json(url: str, client_name: str) -> dict[str, object]:
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("Catalog metadata URL must use HTTPS")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"Suparch/0.2 {client_name}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload_bytes = response.read(1_000_001)
    if len(payload_bytes) > 1_000_000:
        raise ValueError("Catalog metadata exceeds 1 MB")
    payload = json.loads(payload_bytes)
    if not isinstance(payload, dict):
        raise ValueError("Catalog metadata must be a JSON object")
    return payload


def _validate_sha256(value: object, source: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise ValueError(f"{source} has no valid SHA-256")
    return value.casefold()


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


def catalog_sha256(path: Path) -> str:
    with path.open("rb") as catalog_file:
        return hashlib.file_digest(catalog_file, "sha256").hexdigest()


def write_catalog_artifacts(path: Path) -> tuple[Path, Path]:
    path = path.resolve()
    validate_catalog(path)
    checksum = catalog_sha256(path)
    checksum_path = Path(f"{path}.sha256")
    manifest_path = Path(f"{path}.manifest.json")

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        product_count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]

    manifest = {
        "catalog": path.name,
        "sha256": checksum,
        "bytes": path.stat().st_size,
        "schema_version": schema_version,
        "product_count": product_count,
        "generated_at": datetime.now(UTC).isoformat(),
        "metadata": metadata,
    }
    _atomic_write_text(checksum_path, f"{checksum}  {path.name}\n")
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest_path, checksum_path


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
