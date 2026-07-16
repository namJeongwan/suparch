import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import suparch.dsld as dsld_module
from suparch.dsld import (
    DSLD_API_BASE,
    DSLD_PARSER_VERSION,
    DsldClient,
    DsldProductMapper,
    sync_dsld_to_jsonl,
)
from suparch.models import StackSelection
from suparch.repositories import InMemoryCatalogRepository
from suparch.services import CatalogService

FIXTURE = Path(__file__).parent / "fixtures" / "dsld_label.json"


def label_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def write_incomplete_sync_state(
    output: Path,
    *,
    query: str = "*",
    status: int = 1,
    limit: int | None = 2,
) -> None:
    Path(f"{output}.sync.json").write_text(
        json.dumps(
            {
                "source": "NIH DSLD",
                "api_base": DSLD_API_BASE,
                "parser_version": DSLD_PARSER_VERSION,
                "query": query,
                "status": status,
                "limit": limit,
                "sort_by": "entryDate",
                "sort_order": "desc",
                "complete": False,
            }
        ),
        encoding="utf-8",
    )


def test_maps_dsld_label_to_product() -> None:
    product = DsldProductMapper().map_label(label_fixture())

    assert product.id == "dsld:19279"
    assert product.upc == "012345678905"
    assert product.on_market is True
    assert product.supplement_form == "Capsule"
    assert product.product_type == "Single Vitamin and Mineral"
    assert product.target_groups == ["Vegan", "Adult (18 - 50 Years)"]
    assert product.serving_size == "3 Capsule(s)"
    assert product.servings_per_container == Decimal("30")
    assert product.active_ingredients[0].canonical_name == "magnesium"
    assert product.active_ingredients[0].taxonomy_name == "Magnesium"
    assert product.active_ingredients[0].amount_operator == "="
    assert product.active_ingredients[0].normalized_amount == Decimal("400000")
    assert product.active_ingredients[0].daily_value_operator == "="
    assert product.active_ingredients[0].daily_values[0].percent == Decimal("100")
    assert product.active_ingredients[1].parent_ingredient == "magnesium"
    assert product.other_ingredients == ["Vegetarian Capsule", "Silicon Dioxide"]


def test_dsld_client_searches_and_fetches_labels() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search-filter"):
            return httpx.Response(
                200,
                json={
                    "hits": [{"_id": "19279"}],
                    "stats": {"count": 1},
                },
            )
        return httpx.Response(200, json=label_fixture())

    with DsldClient(transport=httpx.MockTransport(handler)) as client:
        ids, total = client.search_label_ids(query="magnesium", size=1)
        label = client.get_label(ids[0])

    assert ids == [19279]
    assert total == 1
    assert label["fullName"] == "Magnesium"


def test_sync_writes_resumable_jsonl(tmp_path: Path) -> None:
    class FakeClient:
        def search_label_ids(
            self,
            *,
            query: str,
            status: int,
            offset: int,
            size: int,
        ) -> tuple[list[int], int]:
            del query, status, size
            return ([19279] if offset == 0 else []), 1

        def get_labels(self, ids: list[int], *, workers: int) -> list[dict]:
            del workers
            return [label_fixture() for _ in ids]

    output = tmp_path / "products.jsonl"

    first = sync_dsld_to_jsonl(
        client=FakeClient(),  # type: ignore[arg-type]
        output=output,
        limit=1,
    )
    assert first == 1
    assert len(output.read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(ValueError, match="already complete"):
        sync_dsld_to_jsonl(
            client=FakeClient(),  # type: ignore[arg-type]
            output=output,
            limit=1,
        )


def test_sync_repairs_a_truncated_final_jsonl_record(tmp_path: Path) -> None:
    class FakeClient:
        def search_label_ids(
            self,
            *,
            query: str,
            status: int,
            offset: int,
            size: int,
        ) -> tuple[list[int], int]:
            del query, status, size
            return ([19279] if offset == 0 else []), 1

        def get_labels(self, ids: list[int], *, workers: int) -> list[dict]:
            del workers
            return [label_fixture() for _ in ids]

    output = tmp_path / "products.jsonl"
    output.write_text('{"source":"dsld","source_product_id":"1"}\n{"partial"', encoding="utf-8")
    write_incomplete_sync_state(output)

    written = sync_dsld_to_jsonl(
        client=FakeClient(),  # type: ignore[arg-type]
        output=output,
        limit=2,
    )

    lines = output.read_text(encoding="utf-8").splitlines()
    assert written == 1
    assert len(lines) == 2
    assert json.loads(lines[-1])["source_product_id"] == "19279"


def test_sync_separates_a_valid_final_record_without_newline(tmp_path: Path) -> None:
    class FakeClient:
        def search_label_ids(
            self,
            *,
            query: str,
            status: int,
            offset: int,
            size: int,
        ) -> tuple[list[int], int]:
            del query, status, size
            return ([19279] if offset == 0 else []), 1

        def get_labels(self, ids: list[int], *, workers: int) -> list[dict]:
            del workers
            return [label_fixture() for _ in ids]

    output = tmp_path / "products.jsonl"
    output.write_text('{"source":"dsld","source_product_id":"1"}', encoding="utf-8")
    write_incomplete_sync_state(output)

    written = sync_dsld_to_jsonl(
        client=FakeClient(),  # type: ignore[arg-type]
        output=output,
        limit=2,
    )

    lines = output.read_text(encoding="utf-8").splitlines()
    assert written == 1
    assert len(lines) == 2
    assert [json.loads(line)["source_product_id"] for line in lines] == ["1", "19279"]


def test_maps_not_provided_quantity_without_a_false_zero() -> None:
    label = label_fixture()
    nested = label["ingredientRows"][0]["nestedRows"][0]
    nested["quantity"] = [{"quantity": 0, "unit": "NP"}]

    product = DsldProductMapper().map_label(label)
    ingredient = product.active_ingredients[1]

    assert ingredient.amount is None
    assert ingredient.unit is None
    assert ingredient.normalized_amount is None


def test_normalizes_dsld_unit_and_known_ingredient_typo() -> None:
    label = label_fixture()
    row = label["ingredientRows"][0]
    row["ingredientGroup"] = "Magnesiium"
    row["quantity"] = [{"quantity": 1, "unit": "Gram(s)"}]

    ingredient = DsldProductMapper().map_label(label).active_ingredients[0]

    assert ingredient.canonical_name == "magnesium"
    assert ingredient.unit == "g"
    assert ingredient.normalized_amount == Decimal("1000000")


def test_unknown_market_status_stays_unknown() -> None:
    label = label_fixture()
    label.pop("offMarket")

    assert DsldProductMapper().map_label(label).on_market is None


def test_preserves_taxonomy_but_canonicalizes_the_label_name() -> None:
    label = label_fixture()
    row = label["ingredientRows"][0]
    row["name"] = "Pantothenic Acid"
    row["ingredientGroup"] = "Pantothenic Acid (Vitamin B5)"

    ingredient = DsldProductMapper().map_label(label).active_ingredients[0]

    assert ingredient.canonical_name == "pantothenic acid"
    assert ingredient.taxonomy_name == "Pantothenic Acid (Vitamin B5)"


def test_selects_quantity_for_primary_serving_size() -> None:
    label = label_fixture()
    row = label["ingredientRows"][0]
    row["quantity"] = [
        {
            "servingSizeOrder": 1,
            "servingSizeQuantity": 6,
            "servingSizeUnit": "Capsule(s)",
            "operator": "=",
            "quantity": 999,
            "unit": "mg",
        },
        {
            "servingSizeOrder": 1,
            "servingSizeQuantity": 3,
            "servingSizeUnit": "Capsule(s)",
            "operator": "=",
            "quantity": 400,
            "unit": "mg",
        },
    ]

    ingredient = DsldProductMapper().map_label(label).active_ingredients[0]

    assert ingredient.amount == Decimal("400")


def test_preserves_target_specific_daily_values() -> None:
    label = label_fixture()
    quantity = label["ingredientRows"][0]["quantity"][0]
    quantity["dailyValueTargetGroup"] = [
        {
            "name": "Adults",
            "operator": "=",
            "percent": 100,
        },
        {
            "name": "Pregnant Women",
            "operator": "<",
            "percent": 80,
            "footnote": "Label-specific value",
        },
    ]

    ingredient = DsldProductMapper().map_label(label).active_ingredients[0]

    assert ingredient.daily_value_percent is None
    assert ingredient.daily_value_operator is None
    assert [value.target_group for value in ingredient.daily_values] == [
        "Adults",
        "Pregnant Women",
    ]
    assert ingredient.daily_values[1].operator == "<"


def test_single_target_specific_daily_value_is_not_exposed_as_generic() -> None:
    label = label_fixture()
    quantity = label["ingredientRows"][0]["quantity"][0]
    quantity["dailyValueTargetGroup"] = [
        {
            "name": "Pregnant Women",
            "operator": "=",
            "percent": 80,
        }
    ]

    ingredient = DsldProductMapper().map_label(label).active_ingredients[0]

    assert ingredient.daily_value_percent is None
    assert ingredient.daily_values[0].target_group == "Pregnant Women"


def test_non_equality_quantity_is_not_used_in_stack_arithmetic() -> None:
    label = label_fixture()
    label["ingredientRows"][0]["quantity"][0]["operator"] = "<"
    product = DsldProductMapper().map_label(label)
    ingredient = product.active_ingredients[0]
    service = CatalogService(InMemoryCatalogRepository([product]))

    result = service.calculate_stack([StackSelection(product_id=product.id)])

    assert ingredient.amount == Decimal("400")
    assert ingredient.amount_operator == "<"
    assert ingredient.normalized_amount is None
    assert "magnesium" not in {total.canonical_name for total in result.totals}


def test_resume_rejects_different_query_provenance(tmp_path: Path) -> None:
    output = tmp_path / "products.jsonl"
    output.write_text('{"source":"dsld","source_product_id":"1"}\n', encoding="utf-8")
    write_incomplete_sync_state(output, query="magnesium")

    with pytest.raises(ValueError, match="provenance mismatch"):
        sync_dsld_to_jsonl(
            client=object(),  # type: ignore[arg-type]
            output=output,
            query="prenatal",
            limit=2,
        )


def test_no_resume_truncates_stale_output_before_new_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "products.jsonl"
    output.write_text('{"source_product_id":"stale"}\n', encoding="utf-8")

    def fail_state_write(path: Path, state: dict) -> None:
        del state
        assert path.read_bytes() == b""
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(dsld_module, "_write_sync_state", fail_state_write)

    with pytest.raises(RuntimeError, match="simulated crash"):
        sync_dsld_to_jsonl(
            client=object(),  # type: ignore[arg-type]
            output=output,
            limit=1,
            resume=False,
        )

    assert output.read_bytes() == b""


def test_resume_requests_full_pages_when_leading_ids_are_complete(
    tmp_path: Path,
) -> None:
    class FakeClient:
        requested_sizes: list[int] = []

        def search_label_ids(
            self,
            *,
            query: str,
            status: int,
            offset: int,
            size: int,
        ) -> tuple[list[int], int]:
            del query, status
            self.requested_sizes.append(size)
            return ([1, 19279] if offset == 0 else []), 2

        def get_labels(self, ids: list[int], *, workers: int) -> list[dict]:
            del workers
            return [label_fixture() for _ in ids]

    output = tmp_path / "products.jsonl"
    output.write_text('{"source":"dsld","source_product_id":"1"}\n', encoding="utf-8")
    write_incomplete_sync_state(output)
    client = FakeClient()

    written = sync_dsld_to_jsonl(
        client=client,  # type: ignore[arg-type]
        output=output,
        limit=2,
        page_size=100,
    )

    assert written == 1
    assert client.requested_sizes == [100]


def test_client_fails_fast_for_non_retryable_http_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request)

    with DsldClient(
        transport=httpx.MockTransport(handler),
        max_retries=4,
    ) as client, pytest.raises(RuntimeError, match="HTTP 404"):
        client.get_label(999999)

    assert calls == 1


def test_client_honors_retry_after_for_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "3"},
                request=request,
            )
        return httpx.Response(200, json=label_fixture(), request=request)

    monkeypatch.setattr("suparch.dsld.time.sleep", delays.append)
    monkeypatch.setattr("suparch.dsld.random.uniform", lambda start, end: 0.125)
    with DsldClient(
        transport=httpx.MockTransport(handler),
        max_retries=1,
    ) as client:
        label = client.get_label(19279)

    assert label["id"] == 19279
    assert calls == 2
    assert delays == [3.125]


def test_client_honors_http_date_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": "Thu, 01 Jan 2099 00:00:00 GMT"},
                request=request,
            )
        return httpx.Response(200, json=label_fixture(), request=request)

    monkeypatch.setattr("suparch.dsld.time.sleep", delays.append)
    monkeypatch.setattr("suparch.dsld.random.uniform", lambda start, end: 0)
    with DsldClient(
        transport=httpx.MockTransport(handler),
        max_retries=1,
    ) as client:
        client.get_label(19279)

    assert calls == 2
    assert delays[0] > 60
