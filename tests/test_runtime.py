import pytest

from suparch.repositories import JsonCatalogRepository
from suparch.runtime import create_repository


def test_http_runtime_fails_closed_without_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPARCH_DB_PATH", raising=False)
    monkeypatch.delenv("SUPARCH_CATALOG_URL", raising=False)
    monkeypatch.delenv("SUPARCH_ALLOW_SAMPLE_CATALOG", raising=False)
    monkeypatch.setenv("SUPARCH_TRANSPORT", "streamable-http")

    with pytest.raises(RuntimeError, match="require SUPARCH_DB_PATH"):
        create_repository()


def test_sample_catalog_requires_explicit_http_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPARCH_DB_PATH", raising=False)
    monkeypatch.delenv("SUPARCH_CATALOG_URL", raising=False)
    monkeypatch.setenv("SUPARCH_TRANSPORT", "streamable-http")
    monkeypatch.setenv("SUPARCH_ALLOW_SAMPLE_CATALOG", "1")

    repository = create_repository()

    assert isinstance(repository, JsonCatalogRepository)
    assert len(repository.list_products()) == 3
