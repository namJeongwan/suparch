import json
import os
import random
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from suparch import __version__
from suparch.models import DailyValue, Ingredient, Product
from suparch.normalization import (
    canonicalize_ingredient,
    normalize_amount,
    normalize_text,
    normalize_unit,
)

DSLD_API_BASE = "https://api.ods.od.nih.gov/dsld/v9"
DSLD_LABEL_BASE = "https://dsld.od.nih.gov/label"
DSLD_PARSER_VERSION = "dsld-v9.4"


class DsldClient:
    """NIH DSLD v9 API client with bounded retries and concurrency."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30,
        max_retries: int = 4,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_seconds,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    f"Suparch/{__version__} (DSLD CC0 catalog client; "
                    "https://github.com/namJeongwan/suparch)"
                ),
            },
        )

    def __enter__(self) -> "DsldClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def search_label_ids(
        self,
        *,
        query: str = "*",
        status: int = 1,
        offset: int = 0,
        size: int = 100,
    ) -> tuple[list[int], int]:
        payload = self._get_json(
            "/search-filter",
            params={
                "q": query,
                "status": status,
                "from": offset,
                "size": size,
                "sort_by": "entryDate",
                "sort_order": "desc",
            },
        )
        hits = payload.get("hits", [])
        ids = [int(hit["_id"]) for hit in hits]
        total = int(payload.get("stats", {}).get("count", len(ids)))
        return ids, total

    def get_label(self, dsld_id: int) -> dict[str, Any]:
        payload = self._get_json(f"/label/{dsld_id}")
        if not isinstance(payload, dict):
            raise ValueError(f"DSLD label {dsld_id} returned no object")
        return payload

    def get_labels(
        self,
        dsld_ids: list[int],
        *,
        workers: int = 4,
    ) -> list[dict[str, Any]]:
        if workers <= 1:
            return [self.get_label(dsld_id) for dsld_id in dsld_ids]

        labels_by_id: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.get_label, dsld_id): dsld_id
                for dsld_id in dsld_ids
            }
            for future in as_completed(futures):
                dsld_id = futures[future]
                labels_by_id[dsld_id] = future.result()
        return [labels_by_id[dsld_id] for dsld_id in dsld_ids]

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(
                    f"{DSLD_API_BASE}{path}",
                    params=params,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable DSLD response: {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if status != 429 and status < 500:
                    raise RuntimeError(
                        f"DSLD request rejected with HTTP {status}: {path}"
                    ) from error
                last_error = error
                if attempt >= self.max_retries:
                    break
                time.sleep(_retry_delay(error.response, attempt))
            except (httpx.HTTPError, json.JSONDecodeError) as error:
                last_error = error
                if attempt >= self.max_retries:
                    break
                time.sleep(_retry_delay(None, attempt))
        raise RuntimeError(f"DSLD request failed: {path}") from last_error


class DsldProductMapper:
    def map_label(
        self,
        label: dict[str, Any],
        *,
        crawled_at: datetime | None = None,
    ) -> Product:
        dsld_id = int(label["id"])
        serving_size, primary_serving = self._primary_serving(label)
        ingredients: list[Ingredient] = []
        for row in label.get("ingredientRows") or []:
            ingredients.extend(
                self._map_ingredient_row(
                    row,
                    parent=None,
                    primary_serving=primary_serving,
                )
            )

        other_ingredients = [
            ingredient["name"].strip()
            for ingredient in (
                (label.get("otheringredients") or {}).get("ingredients") or []
            )
            if ingredient.get("name")
        ]
        supplement_form = (
            (label.get("physicalState") or {}).get("langualCodeDescription")
        )
        product_type = (
            (label.get("productType") or {}).get("langualCodeDescription")
        )
        target_groups = [
            str(value).strip()
            for value in label.get("targetGroups") or []
            if str(value).strip()
        ]

        return Product(
            id=f"dsld:{dsld_id}",
            source="dsld",
            source_product_id=str(dsld_id),
            name=(label.get("fullName") or f"DSLD label {dsld_id}").strip(),
            brand=(label.get("brandName") or "Unknown brand").strip(),
            upc=_normalize_upc(label.get("upcSku")),
            on_market=_on_market(label.get("offMarket")),
            supplement_form=supplement_form,
            product_type=product_type,
            target_groups=target_groups,
            serving_size=serving_size,
            servings_per_container=_decimal_or_none(
                label.get("servingsPerContainer")
            ),
            active_ingredients=ingredients,
            other_ingredients=other_ingredients,
            price=None,
            product_url=f"{DSLD_LABEL_BASE}/{dsld_id}",
            crawled_at=crawled_at or datetime.now(UTC),
            locale="en-US",
            parser_version=DSLD_PARSER_VERSION,
            parser_confidence=Decimal("1"),
        )

    def _map_ingredient_row(
        self,
        row: dict[str, Any],
        *,
        parent: str | None,
        primary_serving: dict[str, Any] | None,
    ) -> list[Ingredient]:
        name = str(row.get("name") or row.get("ingredientGroup") or "").strip()
        if not name:
            return []
        group = str(row.get("ingredientGroup") or "").strip()
        canonical_name = canonicalize_ingredient(name)[0]
        taxonomy_name = group if normalize_text(group) not in {"", "tbd"} else None
        quantity = _quantity_for_serving(row, primary_serving)
        amount, unit = _quantity_amount_and_unit(quantity)
        amount_operator = _operator(quantity)
        if amount_operator in {None, "", "="}:
            normalized_amount, normalized_unit = normalize_amount(amount, unit)
        else:
            normalized_amount, normalized_unit = None, None
        daily_values = _daily_values(quantity)
        if len(daily_values) == 1 and _is_generic_daily_value(daily_values[0]):
            daily_value = daily_values[0].percent
            daily_value_operator = daily_values[0].operator
        else:
            daily_value = None
            daily_value_operator = None
        forms = [
            str(form.get("name")).strip()
            for form in row.get("forms") or []
            if form.get("name")
        ]

        ingredient = Ingredient(
            canonical_name=canonical_name,
            label_name=name,
            taxonomy_name=taxonomy_name,
            form=", ".join(forms) or None,
            amount=amount,
            unit=unit,
            amount_operator=amount_operator,
            normalized_amount=normalized_amount,
            normalized_unit=normalized_unit,
            daily_value_percent=daily_value,
            daily_value_operator=daily_value_operator,
            daily_values=daily_values,
            raw_text=row.get("notes"),
            parent_ingredient=parent,
            confidence=Decimal("1"),
        )
        result = [ingredient]
        for nested in row.get("nestedRows") or []:
            result.extend(
                self._map_ingredient_row(
                    nested,
                    parent=canonical_name,
                    primary_serving=primary_serving,
                )
            )
        return result

    @staticmethod
    def _primary_serving(
        label: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any] | None]:
        sizes = label.get("servingSizes") or []
        if not sizes:
            return None, None
        size = sizes[0]
        quantity = size.get("minQuantity")
        unit = size.get("unit")
        if quantity is None or not unit:
            return None, size
        return f"{quantity} {unit}", size


def sync_dsld_to_jsonl(
    *,
    client: DsldClient,
    output: Path,
    query: str = "*",
    status: int = 1,
    limit: int | None = None,
    page_size: int = 100,
    workers: int = 4,
    resume: bool = True,
) -> int:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive or None")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")

    output.parent.mkdir(parents=True, exist_ok=True)
    state = _prepare_sync_state(
        output=output,
        query=query,
        status=status,
        limit=limit,
        resume=resume,
    )
    completed = _completed_dsld_ids(output) if resume and output.is_file() else set()
    existing_count = len(completed)
    mapper = DsldProductMapper()
    written = 0
    offset = 0

    with output.open("a" if resume else "w", encoding="utf-8") as destination:
        while limit is None or existing_count + written < limit:
            ids, total = client.search_label_ids(
                query=query,
                status=status,
                offset=offset,
                size=page_size,
            )
            if not ids:
                break
            offset += len(ids)
            pending_ids = [dsld_id for dsld_id in ids if dsld_id not in completed]
            if limit is not None:
                remaining = limit - existing_count - written
                pending_ids = pending_ids[:remaining]
            for label in client.get_labels(pending_ids, workers=workers):
                product = mapper.map_label(label)
                destination.write(product.model_dump_json() + "\n")
                completed.add(int(product.source_product_id))
                written += 1
            destination.flush()
            os.fsync(destination.fileno())
            if offset >= total:
                break
    state.update(
        {
            "complete": True,
            "completed_at": datetime.now(UTC).isoformat(),
            "record_count": existing_count + written,
        }
    )
    _write_sync_state(output, state)
    return written


def iter_dsld_products(path: Path) -> Iterator[Product]:
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield Product.model_validate_json(line)


def _completed_dsld_ids(path: Path) -> set[int]:
    completed: set[int] = set()
    with path.open("rb+") as source:
        while True:
            line_start = source.tell()
            line = source.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                if source.read(1):
                    raise
                source.seek(line_start)
                source.truncate()
                break
            if payload.get("source") == "dsld":
                completed.add(int(payload["source_product_id"]))
            if not line.endswith(b"\n"):
                source.seek(0, os.SEEK_END)
                source.write(b"\n")
                source.flush()
                os.fsync(source.fileno())
                break
    return completed


def _quantity_for_serving(
    row: dict[str, Any],
    primary_serving: dict[str, Any] | None,
) -> dict[str, Any] | None:
    quantities = row.get("quantity") or []
    if not quantities:
        return None
    if primary_serving is not None:
        matching = []
        for quantity in quantities:
            if not _serving_field_matches(
                quantity.get("servingSizeOrder"),
                primary_serving.get("order"),
            ):
                continue
            if not _serving_field_matches(
                quantity.get("servingSizeQuantity"),
                primary_serving.get("minQuantity"),
            ):
                continue
            if not _serving_unit_matches(
                quantity.get("servingSizeUnit"),
                primary_serving.get("unit"),
            ):
                continue
            matching.append(quantity)
        if len(matching) == 1:
            return matching[0]
        if matching and len({_quantity_signature(item) for item in matching}) == 1:
            return matching[0]
    return quantities[0] if len(quantities) == 1 else None


def _daily_values(quantity: dict[str, Any] | None) -> list[DailyValue]:
    if not quantity:
        return []
    targets = quantity.get("dailyValueTargetGroup") or []
    values: list[DailyValue] = []
    for target in targets:
        percent = _decimal_or_none(target.get("percent"))
        if percent is None:
            continue
        values.append(
            DailyValue(
                target_group=str(target.get("name") or "").strip() or None,
                percent=percent,
                operator=str(target.get("operator") or "").strip() or None,
                footnote=str(target.get("footnote") or "").strip() or None,
            )
        )
    return values


def _quantity_amount_and_unit(
    quantity: dict[str, Any] | None,
) -> tuple[Decimal | None, str | None]:
    if not quantity:
        return None, None
    raw_unit = str(quantity.get("unit") or "").strip()
    if normalize_text(raw_unit) in {"", "np", "not provided"}:
        return None, None
    return _decimal_or_none(quantity.get("quantity")), normalize_unit(raw_unit)


def _operator(quantity: dict[str, Any] | None) -> str | None:
    if not quantity:
        return None
    return str(quantity.get("operator") or "").strip() or None


def _serving_field_matches(value: object, expected: object) -> bool:
    if value is None or expected is None:
        return True
    return _decimal_or_none(value) == _decimal_or_none(expected)


def _serving_unit_matches(value: object, expected: object) -> bool:
    if value is None or expected is None:
        return True
    return normalize_text(str(value)) == normalize_text(str(expected))


def _quantity_signature(quantity: dict[str, Any]) -> tuple[object, ...]:
    return (
        quantity.get("operator"),
        quantity.get("quantity"),
        quantity.get("unit"),
    )


def _decimal_or_none(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def _normalize_upc(value: object) -> str | None:
    if not value:
        return None
    digits = "".join(character for character in str(value) if character.isdigit())
    return digits or None


def _on_market(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"0", "false", "no"}:
            return True
        if normalized in {"1", "true", "yes"}:
            return False
        return None
    return not bool(value)


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(float(retry_after), 0)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = max(
                        (retry_at - datetime.now(UTC)).total_seconds(),
                        0,
                    )
                except (TypeError, ValueError):
                    delay = None
            if delay is not None:
                return delay + random.uniform(0, 0.25)  # noqa: S311
    return min(2**attempt, 8) + random.uniform(0, 0.25)  # noqa: S311


def _is_generic_daily_value(value: DailyValue) -> bool:
    if value.target_group is None:
        return True
    return normalize_text(value.target_group) in {
        "all",
        "all users",
        "general",
    }


def _sync_state_path(output: Path) -> Path:
    return Path(f"{output}.sync.json")


def _prepare_sync_state(
    *,
    output: Path,
    query: str,
    status: int,
    limit: int | None,
    resume: bool,
) -> dict[str, Any]:
    state_path = _sync_state_path(output)
    expected = {
        "source": "NIH DSLD",
        "api_base": DSLD_API_BASE,
        "parser_version": DSLD_PARSER_VERSION,
        "query": query,
        "status": status,
        "limit": limit,
        "sort_by": "entryDate",
        "sort_order": "desc",
    }
    if not resume:
        with output.open("wb") as destination:
            destination.flush()
            os.fsync(destination.fileno())
    if resume and output.is_file() and output.stat().st_size:
        if not state_path.is_file():
            raise ValueError(
                f"Cannot resume {output} without {state_path}; use --no-resume"
            )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        mismatches = [
            key for key, value in expected.items() if state.get(key) != value
        ]
        if mismatches:
            raise ValueError(
                "DSLD resume provenance mismatch for "
                f"{', '.join(mismatches)}; use --no-resume"
            )
        if state.get("complete"):
            raise ValueError(
                "DSLD sync is already complete; use --no-resume to refresh "
                "market status and changed labels"
            )
        return state

    state = {
        **expected,
        "complete": False,
        "started_at": datetime.now(UTC).isoformat(),
    }
    _write_sync_state(output, state)
    return state


def _write_sync_state(output: Path, state: dict[str, Any]) -> None:
    state_path = _sync_state_path(output)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.",
        suffix=".tmp",
        dir=state_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(state, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, state_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
