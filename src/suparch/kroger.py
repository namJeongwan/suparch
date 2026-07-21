import json
import random
import time
import urllib.parse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from suparch import __version__
from suparch.barcodes import canonicalize_gtin
from suparch.models import Money, OfferContext, Product

KROGER_API_BASE = "https://api.kroger.com/v1"
KROGER_PARSER_VERSION = "kroger-api-v1"
DEFAULT_SUPPLEMENT_CATEGORY_KEYWORDS = (
    "vitamins & supplements",
    "vitamins and supplements",
    "dietary supplements",
    "sports nutrition",
)


class DuplicateKrogerProductConflictError(ValueError):
    pass


@dataclass(slots=True)
class KrogerSyncStats:
    received: int = 0
    imported: int = 0
    duplicates: int = 0
    non_supplement: int = 0
    invalid: int = 0
    missing_gtin: int = 0
    missing_price: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "imported": self.imported,
            "duplicates": self.duplicates,
            "non_supplement": self.non_supplement,
            "invalid": self.invalid,
            "missing_gtin": self.missing_gtin,
            "missing_price": self.missing_price,
        }


class KrogerClient:
    """Client for Kroger's public OAuth2 Products API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout_seconds: float = 30,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not client_id.strip() or not client_secret.strip():
            raise ValueError("Kroger client ID and secret must not be blank")
        self.client_id = client_id
        self.client_secret = client_secret
        self.max_retries = max_retries
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_seconds,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    f"Suparch/{__version__} (Kroger public API client; "
                    "https://github.com/namJeongwan/suparch)"
                ),
            },
        )

    def __enter__(self) -> "KrogerClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def search_products(
        self,
        *,
        term: str,
        location_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        term = term.strip()
        location_id = location_id.strip()
        if not term:
            raise ValueError("Kroger search term must not be blank")
        if not location_id:
            raise ValueError("Kroger location ID must not be blank")
        if limit < 1:
            raise ValueError("Kroger search limit must be positive")

        products: list[dict[str, Any]] = []
        start = 0
        while len(products) < limit:
            page_size = min(50, limit - len(products))
            payload = self._get_json(
                "/products",
                params={
                    "filter.term": term,
                    "filter.locationId": location_id,
                    "filter.start": start,
                    "filter.limit": page_size,
                },
            )
            page = payload.get("data") or []
            if not isinstance(page, list):
                raise RuntimeError("Kroger Products API returned invalid data")
            products.extend(item for item in page if isinstance(item, dict))
            if len(page) < page_size:
                break
            start += len(page)
        return products[:limit]

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, object],
    ) -> dict[str, Any]:
        for auth_attempt in range(2):
            token = self._get_access_token()
            response = self._request(
                "GET",
                f"{KROGER_API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code != 401 or auth_attempt == 1:
                response.raise_for_status()
                return _json_object(response, "Kroger Products API")
            self._access_token = None
            self._token_expires_at = 0
        raise RuntimeError("Kroger Products API authentication failed")

    def _get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        response = self._request(
            "POST",
            f"{KROGER_API_BASE}/connect/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "scope": "product.compact",
            },
            auth=httpx.BasicAuth(self.client_id, self.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = _json_object(response, "Kroger OAuth")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Kroger OAuth response did not include an access token")
        expires_in = max(int(payload.get("expires_in", 1800)), 1)
        self._access_token = token
        self._token_expires_at = time.monotonic() + max(expires_in - 60, 1)
        return token

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable Kroger response: {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                return response
            except httpx.HTTPStatusError as error:
                last_error = error
                if attempt >= self.max_retries:
                    break
                retry_after = error.response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                time.sleep(min(delay, 30) + random.uniform(0, 0.25))
            except httpx.HTTPError as error:
                last_error = error
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2**attempt, 30) + random.uniform(0, 0.25))
        raise RuntimeError(f"Kroger request failed: {method} {url}") from last_error


class KrogerProductMapper:
    def map_product(
        self,
        payload: dict[str, Any],
        *,
        location_id: str,
        fetched_at: datetime | None = None,
    ) -> Product:
        product_id = str(payload.get("productId") or "").strip()
        name = str(payload.get("description") or "").strip()
        if not product_id or not name:
            raise ValueError("Kroger product requires productId and description")

        upc = canonicalize_gtin(payload.get("upc"))
        price, fulfillment = _select_offer(payload.get("items"))
        categories = [
            str(category).strip()
            for category in payload.get("categories") or []
            if str(category).strip()
        ]
        query = upc or product_id
        product_url = "https://www.kroger.com/search?" + urllib.parse.urlencode(
            {"query": query}
        )
        return Product(
            id=f"kroger:{product_id}",
            source="kroger",
            source_product_id=product_id,
            name=name,
            brand=str(payload.get("brand") or "Unknown brand").strip(),
            upc=upc,
            on_market=True,
            product_type=categories[-1] if categories else None,
            active_ingredients=[],
            other_ingredients=[],
            price=price,
            offer_context=OfferContext(
                location_id=location_id,
                fulfillment=fulfillment,
            ),
            product_url=product_url,
            crawled_at=fetched_at or datetime.now(UTC),
            locale="en-US",
            parser_version=KROGER_PARSER_VERSION,
            parser_confidence=Decimal("1"),
        )


def iter_kroger_products(
    client: KrogerClient,
    *,
    terms: Sequence[str],
    location_id: str,
    limit_per_term: int = 100,
    category_keywords: Sequence[str] = DEFAULT_SUPPLEMENT_CATEGORY_KEYWORDS,
    stats: KrogerSyncStats | None = None,
    fetched_at: datetime | None = None,
) -> Iterator[Product]:
    normalized_categories = tuple(
        keyword.strip().casefold() for keyword in category_keywords if keyword.strip()
    )
    if not normalized_categories:
        raise ValueError("Kroger category keywords must not be empty")
    mapper = KrogerProductMapper()
    seen: dict[str, Product] = {}
    for term in terms:
        for payload in client.search_products(
            term=term,
            location_id=location_id,
            limit=limit_per_term,
        ):
            if stats is not None:
                stats.received += 1
            categories = " ".join(
                str(category) for category in payload.get("categories") or []
            ).casefold()
            if not any(keyword in categories for keyword in normalized_categories):
                if stats is not None:
                    stats.non_supplement += 1
                continue
            try:
                product = mapper.map_product(
                    payload,
                    location_id=location_id,
                    fetched_at=fetched_at,
                )
            except (TypeError, ValueError, InvalidOperation):
                if stats is not None:
                    stats.invalid += 1
                continue
            existing = seen.get(product.id)
            if existing is not None:
                if stats is not None:
                    stats.duplicates += 1
                if _product_fingerprint(existing) != _product_fingerprint(product):
                    raise DuplicateKrogerProductConflictError(
                        f"Conflicting Kroger product payloads for {product.id}"
                    )
                continue
            seen[product.id] = product
            if stats is not None:
                stats.imported += 1
                if product.upc is None:
                    stats.missing_gtin += 1
                if product.price is None:
                    stats.missing_price += 1
            yield product


def _select_offer(items: object) -> tuple[Money | None, list[str]]:
    if not isinstance(items, list):
        return None, []
    candidates: list[tuple[Decimal, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("price"), dict):
            continue
        price = item["price"]
        value = price.get("promo") or price.get("regular")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError):
            continue
        if amount.is_finite() and amount >= 0:
            candidates.append((amount, item))
    if not candidates:
        return None, _select_fulfillment(items)
    amount, selected = min(candidates, key=lambda candidate: candidate[0])
    return Money(amount=amount, currency="USD"), _select_fulfillment([selected])


def _select_fulfillment(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    available: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        fulfillment = item.get("fulfillment")
        if not isinstance(fulfillment, dict):
            continue
        available.update(
            str(name) for name, enabled in fulfillment.items() if enabled is True
        )
    return sorted(available)


def _product_fingerprint(product: Product) -> dict[str, object]:
    return product.model_dump(mode="json", exclude={"crawled_at"})


def _json_object(response: httpx.Response, source: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{source} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{source} returned no object")
    return payload
