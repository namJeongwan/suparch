from pathlib import Path

import pytest

import suparch.runtime as runtime_module
from suparch.catalog import SQLiteCatalogBuilder, catalog_sha256, load_json_catalog
from suparch.repositories import JsonCatalogRepository
from suparch.runtime import create_repository

SAMPLE_CATALOG = (
    Path(__file__).parents[1] / "src" / "suparch" / "data" / "sample_catalog.json"
)


def test_http_runtime_fails_closed_without_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPARCH_DB_PATH", raising=False)
    monkeypatch.delenv("SUPARCH_CATALOG_URL", raising=False)
    monkeypatch.delenv("SUPARCH_CATALOG_POINTER_URL", raising=False)
    monkeypatch.delenv("SUPARCH_ALLOW_SAMPLE_CATALOG", raising=False)
    monkeypatch.setenv("SUPARCH_TRANSPORT", "streamable-http")

    with pytest.raises(RuntimeError, match="require SUPARCH_DB_PATH"):
        create_repository()


def test_sample_catalog_requires_explicit_http_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPARCH_DB_PATH", raising=False)
    monkeypatch.delenv("SUPARCH_CATALOG_URL", raising=False)
    monkeypatch.delenv("SUPARCH_CATALOG_POINTER_URL", raising=False)
    monkeypatch.setenv("SUPARCH_TRANSPORT", "streamable-http")
    monkeypatch.setenv("SUPARCH_ALLOW_SAMPLE_CATALOG", "1")

    repository = create_repository()

    assert isinstance(repository, JsonCatalogRepository)
    assert len(repository.list_products()) == 3


def test_remote_catalog_uses_manifest_checksum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite"
    cache = tmp_path / "cache.sqlite"
    SQLiteCatalogBuilder().build(load_json_catalog(SAMPLE_CATALOG), source)
    checksum = catalog_sha256(source)
    checksums: list[str | None] = []

    def fake_manifest(url: str) -> str:
        assert url == "https://example.com/catalog.sqlite.manifest.json"
        return checksum

    def fake_download(
        url: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
    ) -> Path:
        assert url == "https://example.com/catalog.sqlite"
        checksums.append(expected_sha256)
        destination.write_bytes(source.read_bytes())
        return destination

    monkeypatch.setenv("SUPARCH_CATALOG_URL", "https://example.com/catalog.sqlite")
    monkeypatch.setenv("SUPARCH_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.delenv("SUPARCH_CATALOG_SHA256", raising=False)
    monkeypatch.delenv("SUPARCH_CATALOG_MANIFEST_URL", raising=False)
    monkeypatch.delenv("SUPARCH_CATALOG_POINTER_URL", raising=False)
    monkeypatch.setattr(runtime_module, "fetch_catalog_manifest_sha256", fake_manifest)
    monkeypatch.setattr(runtime_module, "download_catalog", fake_download)

    repository = create_repository()

    assert repository.catalog_info().product_count == 3
    assert checksums == [checksum]


def test_remote_catalog_requires_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPARCH_CATALOG_URL", "http://example.com/catalog.sqlite")
    monkeypatch.delenv("SUPARCH_CATALOG_POINTER_URL", raising=False)

    with pytest.raises(ValueError, match="must use HTTPS"):
        create_repository()


def test_official_pointer_resolves_immutable_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite"
    cache = tmp_path / "cache.sqlite"
    SQLiteCatalogBuilder().build(load_json_catalog(SAMPLE_CATALOG), source)
    checksum = catalog_sha256(source)
    downloads: list[tuple[str, str | None]] = []

    monkeypatch.delenv("SUPARCH_CATALOG_URL", raising=False)
    monkeypatch.setenv(
        "SUPARCH_CATALOG_POINTER_URL",
        "https://example.com/catalog-pointer.json",
    )
    monkeypatch.setenv("SUPARCH_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.setattr(
        runtime_module,
        "fetch_catalog_pointer",
        lambda url: ("https://example.com/catalog-123.sqlite", checksum, 4),
    )

    def fake_download(
        url: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
    ) -> Path:
        downloads.append((url, expected_sha256))
        destination.write_bytes(source.read_bytes())
        return destination

    monkeypatch.setattr(runtime_module, "download_catalog", fake_download)

    repository = create_repository()

    assert repository.catalog_info().product_count == 3
    assert downloads == [("https://example.com/catalog-123.sqlite", checksum)]


def test_official_pointer_rejects_incompatible_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPARCH_CATALOG_URL", raising=False)
    monkeypatch.setenv(
        "SUPARCH_CATALOG_POINTER_URL",
        "https://example.com/v3/catalog-pointer.json",
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_catalog_pointer",
        lambda url: ("https://example.com/catalog.sqlite", "a" * 64, 3),
    )

    with pytest.raises(RuntimeError, match="schema mismatch"):
        create_repository()


def test_remote_cache_is_bound_to_url_and_checksum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite"
    cache = tmp_path / "cache.sqlite"
    SQLiteCatalogBuilder().build(load_json_catalog(SAMPLE_CATALOG), source)
    checksum = catalog_sha256(source)
    downloads: list[str] = []

    monkeypatch.setenv("SUPARCH_CATALOG_URL", "https://example.com/catalog-a.sqlite")
    monkeypatch.setenv("SUPARCH_CATALOG_SHA256", checksum)
    monkeypatch.setenv("SUPARCH_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.delenv("SUPARCH_CATALOG_POINTER_URL", raising=False)

    def fake_download(
        url: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
    ) -> Path:
        assert expected_sha256 == checksum
        downloads.append(url)
        destination.write_bytes(source.read_bytes())
        return destination

    monkeypatch.setattr(runtime_module, "download_catalog", fake_download)

    create_repository()
    create_repository()
    tampered_products = load_json_catalog(SAMPLE_CATALOG)
    tampered_products[0].name = "Tampered but structurally valid"
    SQLiteCatalogBuilder().build(tampered_products, cache)
    create_repository()
    monkeypatch.setenv("SUPARCH_CATALOG_URL", "https://example.com/catalog-b.sqlite")
    create_repository()

    assert downloads == [
        "https://example.com/catalog-a.sqlite",
        "https://example.com/catalog-a.sqlite",
        "https://example.com/catalog-b.sqlite",
    ]
