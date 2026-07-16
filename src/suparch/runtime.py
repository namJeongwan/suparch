import os
from pathlib import Path

from suparch.catalog import download_catalog
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

    catalog_url = os.environ.get("SUPARCH_CATALOG_URL")
    if catalog_url:
        cache_path = Path(
            os.environ.get("SUPARCH_CATALOG_CACHE_PATH", DEFAULT_REMOTE_CACHE)
        )
        refresh = os.environ.get("SUPARCH_CATALOG_REFRESH") == "1"
        if refresh or not cache_path.is_file():
            download_catalog(
                catalog_url,
                cache_path,
                expected_sha256=os.environ.get("SUPARCH_CATALOG_SHA256"),
            )
        return SqliteCatalogRepository(cache_path)

    transport = os.environ.get("SUPARCH_TRANSPORT", "stdio")
    allow_sample = os.environ.get("SUPARCH_ALLOW_SAMPLE_CATALOG") == "1"
    if transport != "stdio" and not allow_sample:
        raise RuntimeError(
            "HTTP/SSE deployments require SUPARCH_DB_PATH or "
            "SUPARCH_CATALOG_URL. Set SUPARCH_ALLOW_SAMPLE_CATALOG=1 only "
            "for an explicit demo deployment."
        )

    json_path = Path(
        os.environ.get("SUPARCH_CATALOG_PATH", DEFAULT_JSON_CATALOG)
    )
    return JsonCatalogRepository(json_path)
