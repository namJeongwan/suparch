from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from suparch.kroger import (
    DuplicateKrogerProductConflictError,
    KrogerClient,
    KrogerProductMapper,
    KrogerSyncStats,
    iter_kroger_products,
)


def kroger_product() -> dict:
    return {
        "productId": "0001234567890",
        "upc": "012345678905",
        "brand": "Example Nutrition",
        "description": "Magnesium Glycinate 90 Capsules",
        "categories": ["Health", "Vitamins & Supplements"],
        "items": [
            {
                "itemId": "0001234567890",
                "price": {"regular": 19.99, "promo": 17.49},
                "fulfillment": {
                    "curbside": True,
                    "delivery": True,
                    "shipToHome": False,
                },
                "size": "90 ct",
            },
            {
                "itemId": "0001234567891",
                "price": {"regular": 21.99, "promo": 0},
                "fulfillment": {"shipToHome": True},
                "size": "90 ct",
            },
        ],
    }


def test_searches_kroger_with_oauth_and_location_price_context() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/connect/oauth2/token"):
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 1800},
                request=request,
            )
        return httpx.Response(
            200,
            json={"data": [kroger_product()]},
            request=request,
        )

    with KrogerClient(
        "client-id",
        "client-secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        products = client.search_products(
            term="magnesium",
            location_id="01400943",
            limit=10,
        )

    assert products[0]["description"].startswith("Magnesium")
    assert [request.url.path for request in requests] == [
        "/v1/connect/oauth2/token",
        "/v1/products",
    ]
    assert requests[1].url.params["filter.term"] == "magnesium"
    assert requests[1].url.params["filter.locationId"] == "01400943"
    assert requests[1].headers["Authorization"] == "Bearer token"


def test_maps_kroger_offer_to_normalized_product() -> None:
    product = KrogerProductMapper().map_product(
        kroger_product(),
        location_id="01400943",
        fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
    )

    assert product.id == "kroger:0001234567890"
    assert product.source == "kroger"
    assert product.upc == "00012345678905"
    assert product.product_type == "Vitamins & Supplements"
    assert product.price is not None
    assert product.price.amount == Decimal("17.49")
    assert product.price.currency == "USD"
    assert product.active_ingredients == []
    assert product.offer_context is not None
    assert product.offer_context.location_id == "01400943"
    assert product.offer_context.fulfillment == ["curbside", "delivery"]
    assert "query=00012345678905" in str(product.product_url)


def test_deduplicates_overlapping_kroger_search_terms_and_reports_gaps() -> None:
    payload_without_offer = {
        **kroger_product(),
        "productId": "second",
        "upc": "not-a-gtin",
        "items": [],
    }
    food = {
        **kroger_product(),
        "productId": "food",
        "categories": ["Grocery", "Nutrition"],
    }

    class FakeClient:
        def search_products(
            self,
            *,
            term: str,
            location_id: str,
            limit: int,
        ) -> list[dict]:
            del location_id, limit
            return (
                [kroger_product(), payload_without_offer, food]
                if term == "vitamin"
                else [kroger_product()]
            )

    stats = KrogerSyncStats()
    products = list(
        iter_kroger_products(
            FakeClient(),  # type: ignore[arg-type]
            terms=["vitamin", "magnesium"],
            location_id="01400943",
            stats=stats,
        )
    )

    assert [product.id for product in products] == [
        "kroger:0001234567890",
        "kroger:second",
    ]
    assert stats.received == 4
    assert stats.imported == 2
    assert stats.duplicates == 1
    assert stats.non_supplement == 1
    assert stats.missing_gtin == 1
    assert stats.missing_price == 1


def test_rejects_conflicting_products_from_overlapping_terms() -> None:
    changed = kroger_product()
    changed["items"][0]["price"]["promo"] = 16.99

    class FakeClient:
        def search_products(
            self,
            *,
            term: str,
            location_id: str,
            limit: int,
        ) -> list[dict]:
            del location_id, limit
            return [kroger_product() if term == "vitamin" else changed]

    with pytest.raises(DuplicateKrogerProductConflictError, match="kroger:"):
        list(
            iter_kroger_products(
                FakeClient(),  # type: ignore[arg-type]
                terms=["vitamin", "magnesium"],
                location_id="01400943",
                fetched_at=datetime(2026, 7, 20, tzinfo=UTC),
            )
        )
