import json
import os
import tempfile
import urllib.parse
from pathlib import Path

from suparch.catalog import (
    SCHEMA_VERSION,
    catalog_sha256,
    download_catalog,
    fetch_catalog_manifest_sha256,
    fetch_catalog_pointer,
)
from suparch.repositories import (
    CatalogRepository,
    JsonCatalogRepository,
    SqliteCatalogRepository,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_JSON_CATALOG = PACKAGE_ROOT / "data" / "sample_catalog.json"
DEFAULT_REMOTE_CACHE = Path("/tmp/suparch/catalog.sqlite")


def create_repository() -> CatalogRepository:
    database_path = os.environ.get("SUPARCH_DB_PATH")
    if database_path:
        return SqliteCatalogRepository(Path(database_path))

    remote = _remote_catalog()
    if remote:
        catalog_url, expected_sha256 = remote
        cache_path = Path(
            os.environ.get("SUPARCH_CATALOG_CACHE_PATH", DEFAULT_REMOTE_CACHE)
        )
        refresh = os.environ.get("SUPARCH_CATALOG_REFRESH") == "1"
        if refresh or not _valid_cached_catalog(
            cache_path,
            catalog_url,
            expected_sha256,
        ):
            download_catalog(
                catalog_url,
                cache_path,
                expected_sha256=expected_sha256,
            )
            _write_cache_metadata(cache_path, catalog_url, expected_sha256)
        return SqliteCatalogRepository(cache_path)

    transport = os.environ.get("SUPARCH_TRANSPORT", "stdio")
    allow_sample = os.environ.get("SUPARCH_ALLOW_SAMPLE_CATALOG") == "1"
    if transport != "stdio" and not allow_sample:
        raise RuntimeError(
            "HTTP/SSE deployments require SUPARCH_DB_PATH or "
            "SUPARCH_CATALOG_URL/SUPARCH_CATALOG_POINTER_URL. "
            "Set SUPARCH_ALLOW_SAMPLE_CATALOG=1 only "
            "for an explicit demo deployment."
        )

    json_path = Path(
        os.environ.get("SUPARCH_CATALOG_PATH", DEFAULT_JSON_CATALOG)
    )
    return JsonCatalogRepository(json_path)


def _remote_catalog() -> tuple[str, str] | None:
    catalog_url = os.environ.get("SUPARCH_CATALOG_URL")
    if catalog_url:
        if urllib.parse.urlparse(catalog_url).scheme != "https":
            raise ValueError("SUPARCH_CATALOG_URL must use HTTPS")
        expected_sha256 = os.environ.get("SUPARCH_CATALOG_SHA256")
        if expected_sha256 is None:
            manifest_url = os.environ.get(
                "SUPARCH_CATALOG_MANIFEST_URL",
                f"{catalog_url}.manifest.json",
            )
            expected_sha256 = fetch_catalog_manifest_sha256(manifest_url)
        return catalog_url, expected_sha256.casefold()

    pointer_url = os.environ.get("SUPARCH_CATALOG_POINTER_URL")
    if pointer_url:
        catalog_url, checksum, schema_version = fetch_catalog_pointer(pointer_url)
        if schema_version != SCHEMA_VERSION:
            raise RuntimeError(
                "Catalog pointer schema mismatch: "
                f"expected {SCHEMA_VERSION}, got {schema_version}"
            )
        return catalog_url, checksum
    return None


def _valid_cached_catalog(
    cache_path: Path,
    catalog_url: str,
    expected_sha256: str,
) -> bool:
    metadata_path = _cache_metadata_path(cache_path)
    if not cache_path.is_file() or not metadata_path.is_file():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata != {
            "catalog_url": catalog_url,
            "sha256": expected_sha256,
        }:
            return False
        return catalog_sha256(cache_path) == expected_sha256
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _write_cache_metadata(
    cache_path: Path,
    catalog_url: str,
    expected_sha256: str,
) -> None:
    metadata_path = _cache_metadata_path(cache_path)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{metadata_path.name}.",
        suffix=".tmp",
        dir=metadata_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(
                {
                    "catalog_url": catalog_url,
                    "sha256": expected_sha256,
                },
                destination,
                indent=2,
                sort_keys=True,
            )
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, metadata_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _cache_metadata_path(cache_path: Path) -> Path:
    return Path(f"{cache_path}.cache.json")
