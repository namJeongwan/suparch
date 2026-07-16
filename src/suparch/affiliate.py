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
        seen_ids: set[str] = set()
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
                if product.id in seen_ids:
                    counters.duplicates += 1
                    continue
                seen_ids.add(product.id)
                gtin_value = _first(
                    row,
                    "gtin",
                    "upc",
                    "upccode",
                    "barcode",
                    "ean",
                )
                if gtin_value and product.upc is None:
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
        gtin_value = _first(row, "gtin", "upc", "upccode", "barcode", "ean")
        gtin = canonicalize_gtin(gtin_value) if gtin_value else None
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
