import csv
import gzip
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from suparch.barcodes import canonicalize_gtin
from suparch.models import Money, Product

AFFILIATE_PARSER_VERSION = "iherb-affiliate-feed-v1"
PRODUCT_ID_RE = re.compile(r"/pr/(?:[^/]+/)?(\d+)(?:[/?#]|$)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AffiliateFeedPolicy:
    locale: str = "en-US"
    currency: str = "USD"
    category_keywords: tuple[str, ...] = ("supplement",)

    def __post_init__(self) -> None:
        keywords = tuple(keyword.strip() for keyword in self.category_keywords)
        if not keywords or any(not keyword for keyword in keywords):
            raise ValueError("category_keywords must contain non-blank values")
        object.__setattr__(self, "category_keywords", keywords)


@dataclass(slots=True)
class AffiliateFeedStats:
    total: int = 0
    imported: int = 0
    non_supplement: int = 0
    non_usd: int = 0
    invalid: int = 0
    missing_gtin: int = 0
    invalid_gtin: int = 0
    duplicates: int = 0

    @property
    def valid_gtins(self) -> int:
        return self.imported - self.missing_gtin - self.invalid_gtin

    @property
    def import_rate(self) -> float:
        return self.imported / self.total if self.total else 0.0

    @property
    def gtin_coverage(self) -> float:
        return self.valid_gtins / self.imported if self.imported else 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "total": self.total,
            "imported": self.imported,
            "non_supplement": self.non_supplement,
            "non_usd": self.non_usd,
            "invalid": self.invalid,
            "missing_gtin": self.missing_gtin,
            "invalid_gtin": self.invalid_gtin,
            "duplicates": self.duplicates,
            "valid_gtins": self.valid_gtins,
            "import_rate": round(self.import_rate, 6),
            "gtin_coverage": round(self.gtin_coverage, 6),
        }


class IHerbAffiliateFeedReader:
    """Map an approved English iHerb affiliate CSV feed to Product records."""

    def __init__(self, policy: AffiliateFeedPolicy | None = None) -> None:
        self.policy = policy or AffiliateFeedPolicy()

    def iter_products(
        self,
        path: Path,
        *,
        stats: AffiliateFeedStats | None = None,
        imported_at: datetime | None = None,
    ) -> Iterator[Product]:
        counters = stats or AffiliateFeedStats()
        timestamp = imported_at or datetime.now(UTC)
        seen_products: dict[str, tuple[object, ...]] = {}
        with _open_feed(path) as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                raise ValueError("Affiliate feed has no CSV header")
            for raw_row in reader:
                counters.total += 1
                try:
                    row = _normalize_row(raw_row)
                except ValueError:
                    counters.invalid += 1
                    continue
                category = _category_text(row)
                if not _is_supplement(category, self.policy.category_keywords):
                    counters.non_supplement += 1
                    continue
                try:
                    product = self._map_row(row, category, timestamp)
                except CurrencyMismatchError:
                    counters.non_usd += 1
                    continue
                except (InvalidOperation, ValueError):
                    counters.invalid += 1
                    continue
                fingerprint = _product_fingerprint(product)
                previous = seen_products.get(product.id)
                if previous is not None:
                    if previous != fingerprint:
                        raise DuplicateProductConflictError(
                            f"Conflicting duplicate iHerb product ID: {product.id}"
                        )
                    counters.duplicates += 1
                    continue
                seen_products[product.id] = fingerprint
                gtin_values = _barcode_values(row)
                if gtin_values and product.upc is None:
                    counters.invalid_gtin += 1
                elif product.upc is None:
                    counters.missing_gtin += 1
                counters.imported += 1
                yield product

    def _map_row(
        self,
        row: dict[str, str],
        category: str,
        imported_at: datetime,
    ) -> Product:
        url = _required(row, "url", "producturl", "link", "deeplink", "trackinglink")
        product_id = _iherb_product_id(url)
        name = _required(row, "name", "title", "productname", "producttitle")
        brand = _required(row, "brand", "manufacturer", "vendor")
        price, currency = _price(row, self.policy.currency)
        gtin = _canonical_gtin(row)
        return Product(
            id=f"iherb:{product_id}",
            source="iherb",
            source_product_id=product_id,
            name=name,
            brand=brand,
            upc=gtin,
            product_type=category,
            active_ingredients=[],
            other_ingredients=[],
            price=Money(amount=price, currency=currency),
            product_url=url,
            crawled_at=imported_at,
            locale=self.policy.locale,
            parser_version=AFFILIATE_PARSER_VERSION,
            parser_confidence=Decimal("1"),
        )


class CurrencyMismatchError(ValueError):
    pass


class DuplicateProductConflictError(ValueError):
    pass


def affiliate_feed_quality_failures(
    stats: AffiliateFeedStats,
    *,
    min_products: int = 1,
    min_gtin_coverage: float = 0.0,
) -> list[str]:
    if min_products < 1:
        raise ValueError("min_products must be positive")
    if not 0 <= min_gtin_coverage <= 1:
        raise ValueError("min_gtin_coverage must be between 0 and 1")

    failures: list[str] = []
    if stats.imported < min_products:
        failures.append(
            f"imported products {stats.imported} is below minimum {min_products}"
        )
    if stats.gtin_coverage < min_gtin_coverage:
        failures.append(
            f"GTIN coverage {stats.gtin_coverage:.2%} is below minimum "
            f"{min_gtin_coverage:.2%}"
        )
    return failures


def _open_feed(path: Path):
    if path.suffix.casefold() == ".gz":
        return gzip.open(path, mode="rt", encoding="utf-8-sig", newline="")
    return path.open(mode="r", encoding="utf-8-sig", newline="")


def _field_key(value: str | None) -> str:
    return "".join(character for character in (value or "").casefold() if character.isalnum())


def _normalize_row(row: dict[str | None, str | list[str] | None]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in row.items():
        if key is None or isinstance(value, list):
            raise ValueError("Malformed affiliate CSV row")
        normalized[_field_key(key)] = (value or "").strip()
    return normalized


def _first(row: dict[str, str], *aliases: str) -> str | None:
    for alias in aliases:
        value = row.get(_field_key(alias))
        if value:
            return value
    return None


def _barcode_values(row: dict[str, str]) -> list[str]:
    return list(
        dict.fromkeys(
            value
            for alias in ("gtin", "upc", "upccode", "barcode", "ean")
            if (value := row.get(_field_key(alias)))
        )
    )


def _canonical_gtin(row: dict[str, str]) -> str | None:
    canonical = {
        value
        for raw_value in _barcode_values(row)
        if (value := canonicalize_gtin(raw_value)) is not None
    }
    if len(canonical) > 1:
        raise ValueError("Conflicting valid GTIN values in affiliate row")
    return next(iter(canonical), None)


def _product_fingerprint(product: Product) -> tuple[object, ...]:
    return (
        product.name,
        product.brand,
        product.upc,
        product.product_type,
        product.price.amount if product.price else None,
        product.price.currency if product.price else None,
    )


def _required(row: dict[str, str], *aliases: str) -> str:
    value = _first(row, *aliases)
    if value is None:
        raise ValueError(f"Missing required affiliate field: {aliases[0]}")
    return value


def _category_text(row: dict[str, str]) -> str:
    category_fields = [
        value
        for key, value in row.items()
        if value and ("category" in key or key in {"producttype", "producttypename"})
    ]
    return " > ".join(dict.fromkeys(category_fields))


def _is_supplement(category: str, keywords: tuple[str, ...]) -> bool:
    normalized = category.casefold()
    return bool(normalized) and any(keyword.casefold() in normalized for keyword in keywords)


def _iherb_product_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (host == "iherb.com" or host.endswith(".iherb.com")):
        raise ValueError("Affiliate product URL must be HTTPS on iherb.com")
    match = PRODUCT_ID_RE.search(parsed.path)
    if match is None:
        raise ValueError("Affiliate product URL has no iHerb /pr/ product ID")
    return match.group(1)


def _price(row: dict[str, str], expected_currency: str) -> tuple[Decimal, str]:
    raw_price = _required(row, "currentprice", "price", "saleprice", "productprice")
    currency = (_first(row, "currency", "currencycode") or expected_currency).upper()
    upper_price = raw_price.upper()
    detected = next(
        (
            code
            for code in ("USD", "EUR", "GBP", "CAD", "AUD")
            if code in upper_price
        ),
        None,
    )
    detected = detected or next(
        (
            code
            for symbol, code in (("€", "EUR"), ("£", "GBP"), ("C$", "CAD"), ("A$", "AUD"))
            if symbol in raw_price
        ),
        None,
    )
    if detected:
        currency = detected
    if currency != expected_currency:
        raise CurrencyMismatchError(currency)
    numeric = re.sub(r"[^0-9.\-]", "", raw_price.replace(",", ""))
    price = Decimal(numeric)
    if price < 0:
        raise ValueError("Price cannot be negative")
    return price, currency
