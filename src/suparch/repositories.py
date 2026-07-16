import json
import re
import sqlite3
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter

from suparch.catalog import validate_catalog
from suparch.models import Product, ProductSearchQuery, ProductSearchResult, ProductSummary
from suparch.normalization import canonicalize_ingredient, normalize_text


class CatalogRepository(Protocol):
    def list_products(self) -> list[Product]: ...

    def get_product(self, product_id: str) -> Product | None: ...

    def search_products(self, search: ProductSearchQuery) -> ProductSearchResult: ...

    def get_products(self, product_ids: list[str]) -> list[Product]: ...


class InMemoryCatalogRepository:
    def __init__(self, products: list[Product] | None = None) -> None:
        self._products = {product.id: product for product in products or []}

    def list_products(self) -> list[Product]:
        return list(self._products.values())

    def get_product(self, product_id: str) -> Product | None:
        return self._products.get(product_id)

    def get_products(self, product_ids: list[str]) -> list[Product]:
        return [
            self._products[product_id]
            for product_id in product_ids
            if product_id in self._products
        ]

    def search_products(self, search: ProductSearchQuery) -> ProductSearchResult:
        products = [product for product in self._products.values() if _matches(product, search)]
        products.sort(key=lambda product: (product.brand.casefold(), product.name.casefold()))
        total = len(products)
        selected = products[search.offset : search.offset + search.limit]
        return ProductSearchResult(
            products=[ProductSummary.from_product(product) for product in selected],
            total=total,
        )


class JsonCatalogRepository(InMemoryCatalogRepository):
    def __init__(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        products = TypeAdapter(list[Product]).validate_python(payload)
        super().__init__(products)


def _matches(product: Product, search: ProductSearchQuery) -> bool:
    ingredient_names = {
        normalize_text(ingredient.canonical_name) for ingredient in product.active_ingredients
    }
    ingredient_forms = {
        normalize_text(ingredient.form)
        for ingredient in product.active_ingredients
        if ingredient.form
    }

    if search.query:
        haystack = normalize_text(
            " ".join(
                [
                    product.name,
                    product.brand,
                    *ingredient_names,
                    *ingredient_forms,
                ]
            )
        )
        if normalize_text(search.query) not in haystack:
            return False

    if any(
        canonicalize_ingredient(name)[0] not in ingredient_names
        for name in search.include_ingredients
    ):
        return False
    if any(
        canonicalize_ingredient(name)[0] in ingredient_names
        for name in search.exclude_ingredients
    ):
        return False
    if search.forms and not all(
        any(normalize_text(required) in form for form in ingredient_forms)
        for required in search.forms
    ):
        return False
    if search.brands and normalize_text(product.brand) not in {
        normalize_text(brand) for brand in search.brands
    }:
        return False
    if search.max_price is not None:
        if product.price is None or product.price.amount > search.max_price:
            return False
        if search.currency and product.price.currency != search.currency:
            return False
    elif search.currency and (
        product.price is None or product.price.currency != search.currency
    ):
        return False
    return True


class SqliteCatalogRepository:
    """Read-only SQLite repository intended for MCP runtime use."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"Catalog database not found: {self.path}")
        validate_catalog(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def list_products(self) -> list[Product]:
        with self._connect() as connection:
            ids = [row["id"] for row in connection.execute("SELECT id FROM products")]
            products: list[Product] = []
            for start in range(0, len(ids), 500):
                products.extend(self._load_products(connection, ids[start : start + 500]))
            return products

    def get_product(self, product_id: str) -> Product | None:
        products = self.get_products([product_id])
        return products[0] if products else None

    def get_products(self, product_ids: list[str]) -> list[Product]:
        if not product_ids:
            return []
        with self._connect() as connection:
            products = self._load_products(connection, product_ids)
        order = {product_id: index for index, product_id in enumerate(product_ids)}
        return sorted(products, key=lambda product: order[product.id])

    def search_products(self, search: ProductSearchQuery) -> ProductSearchResult:
        joins: list[str] = []
        conditions: list[str] = []
        parameters: list[object] = []

        if search.query:
            joins.append("JOIN product_search ON product_search.product_id = p.id")
            conditions.append("product_search MATCH ?")
            parameters.append(_fts_query(search.query))

        for ingredient in search.include_ingredients:
            conditions.append(
                """
                EXISTS (
                    SELECT 1 FROM product_ingredients pi
                    WHERE pi.product_id = p.id AND pi.canonical_name = ?
                )
                """
            )
            parameters.append(canonicalize_ingredient(ingredient)[0])

        for ingredient in search.exclude_ingredients:
            conditions.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM product_ingredients pi
                    WHERE pi.product_id = p.id AND pi.canonical_name = ?
                )
                """
            )
            parameters.append(canonicalize_ingredient(ingredient)[0])

        for form in search.forms:
            conditions.append(
                """
                EXISTS (
                    SELECT 1 FROM product_ingredients pi
                    WHERE pi.product_id = p.id AND pi.form LIKE ?
                )
                """
            )
            parameters.append(f"%{normalize_text(form)}%")

        if search.brands:
            placeholders = ", ".join("?" for _ in search.brands)
            conditions.append(f"lower(p.brand) IN ({placeholders})")
            parameters.extend(brand.casefold() for brand in search.brands)

        if search.max_price is not None:
            conditions.append("CAST(p.price_amount AS REAL) <= CAST(? AS REAL)")
            parameters.append(str(search.max_price))
        if search.currency:
            conditions.append("p.price_currency = ?")
            parameters.append(search.currency)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        join_sql = " ".join(joins)

        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(DISTINCT p.id) FROM products p {join_sql} {where}",
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT DISTINCT p.id
                FROM products p
                {join_sql}
                {where}
                ORDER BY lower(p.brand), lower(p.name)
                LIMIT ? OFFSET ?
                """,
                [*parameters, search.limit, search.offset],
            ).fetchall()
            products = self._load_products(connection, [row["id"] for row in rows])

        products_by_id = {product.id: product for product in products}
        ordered = [products_by_id[row["id"]] for row in rows]
        return ProductSearchResult(
            products=[ProductSummary.from_product(product) for product in ordered],
            total=total,
        )

    @staticmethod
    def _load_products(
        connection: sqlite3.Connection,
        product_ids: list[str],
    ) -> list[Product]:
        if not product_ids:
            return []
        placeholders = ", ".join("?" for _ in product_ids)
        product_rows = connection.execute(
            f"SELECT * FROM products WHERE id IN ({placeholders})",
            product_ids,
        ).fetchall()
        ingredient_rows = connection.execute(
            f"""
            SELECT * FROM product_ingredients
            WHERE product_id IN ({placeholders})
            ORDER BY product_id, position
            """,
            product_ids,
        ).fetchall()
        other_rows = connection.execute(
            f"""
            SELECT * FROM other_ingredients
            WHERE product_id IN ({placeholders})
            ORDER BY product_id, position
            """,
            product_ids,
        ).fetchall()

        ingredients: dict[str, list[dict[str, object]]] = {}
        for row in ingredient_rows:
            ingredients.setdefault(row["product_id"], []).append(
                {
                    "canonical_name": row["canonical_name"],
                    "label_name": row["label_name"],
                    "form": row["form"],
                    "amount": row["amount"],
                    "unit": row["unit"],
                    "normalized_amount": row["normalized_amount"],
                    "normalized_unit": row["normalized_unit"],
                    "daily_value_percent": row["daily_value_percent"],
                    "raw_text": row["raw_text"],
                    "parent_ingredient": row["parent_ingredient"],
                    "confidence": row["confidence"],
                }
            )

        others: dict[str, list[str]] = {}
        for row in other_rows:
            others.setdefault(row["product_id"], []).append(row["name"])

        products: list[Product] = []
        for row in product_rows:
            price = None
            if row["price_amount"] is not None and row["price_currency"] is not None:
                price = {
                    "amount": row["price_amount"],
                    "currency": row["price_currency"],
                }
            products.append(
                Product(
                    id=row["id"],
                    source=row["source"],
                    source_product_id=row["source_product_id"],
                    name=row["name"],
                    brand=row["brand"],
                    serving_size=row["serving_size"],
                    servings_per_container=row["servings_per_container"],
                    active_ingredients=ingredients.get(row["id"], []),
                    other_ingredients=others.get(row["id"], []),
                    price=price,
                    product_url=row["product_url"],
                    crawled_at=row["crawled_at"],
                    locale=row["locale"],
                    parser_version=row["parser_version"],
                    parser_confidence=row["parser_confidence"],
                )
            )
        return products


def _fts_query(value: str) -> str:
    tokens = re.findall(r"[\w]+", normalize_text(value), flags=re.UNICODE)
    if not tokens:
        return '""'
    return " AND ".join(f'"{token}"*' for token in tokens)
