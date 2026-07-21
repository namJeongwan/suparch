import json
import subprocess
import sys
from pathlib import Path

import pytest

import suparch.cli as cli_module
from suparch.catalog import load_json_catalog
from suparch.models import OfferContext

AFFILIATE_FIXTURE = Path(__file__).parent / "fixtures" / "iherb_affiliate_feed.csv"
SAMPLE_CATALOG = (
    Path(__file__).parents[1] / "src" / "suparch" / "data" / "sample_catalog.json"
)


class FakeKrogerClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        assert client_id == "client-id"
        assert client_secret == "client-secret"

    def __enter__(self) -> "FakeKrogerClient":
        return self

    def __exit__(self, *args: object) -> None:
        pass


def kroger_cli_product():
    product = load_json_catalog(SAMPLE_CATALOG)[0]
    return product.model_copy(
        deep=True,
        update={
            "id": "kroger:example",
            "source": "kroger",
            "source_product_id": "example",
            "offer_context": OfferContext(
                location_id="01400943",
                fulfillment=["curbside"],
            ),
        },
    )


def test_kroger_sync_requires_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("KROGER_CLIENT_ID", raising=False)
    monkeypatch.delenv("KROGER_CLIENT_SECRET", raising=False)
    args = cli_module.build_parser().parse_args(
        [
            "kroger-sync",
            "--term",
            "magnesium",
            "--location-id",
            "01400943",
            "--output",
            str(tmp_path / "products.jsonl"),
        ]
    )

    with pytest.raises(SystemExit, match="KROGER_CLIENT_ID"):
        args.handler(args)


def test_kroger_sync_writes_atomic_output_and_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KROGER_CLIENT_ID", "client-id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(cli_module, "KrogerClient", FakeKrogerClient)

    def fake_products(client, **kwargs):
        del client
        kwargs["stats"].received = 1
        kwargs["stats"].imported = 1
        yield kroger_cli_product()

    monkeypatch.setattr(cli_module, "iter_kroger_products", fake_products)
    output = tmp_path / "products.jsonl"
    report = tmp_path / "report.json"
    args = cli_module.build_parser().parse_args(
        [
            "kroger-sync",
            "--term",
            "magnesium",
            "--location-id",
            "01400943",
            "--output",
            str(output),
            "--report",
            str(report),
        ]
    )

    args.handler(args)

    assert json.loads(output.read_text(encoding="utf-8"))["source"] == "kroger"
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    assert report_payload["location_id"] == "01400943"
    assert report_payload["terms"] == ["magnesium"]


def test_failed_kroger_sync_preserves_output_and_rejects_path_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KROGER_CLIENT_ID", "client-id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(cli_module, "KrogerClient", FakeKrogerClient)

    def failed_products(client, **kwargs):
        del client
        kwargs["stats"].received = 1
        kwargs["stats"].imported = 1
        yield kroger_cli_product()
        raise ValueError("conflicting product")

    monkeypatch.setattr(cli_module, "iter_kroger_products", failed_products)
    output = tmp_path / "products.jsonl"
    output.write_text("previous snapshot\n", encoding="utf-8")
    args = cli_module.build_parser().parse_args(
        [
            "kroger-sync",
            "--term",
            "magnesium",
            "--location-id",
            "01400943",
            "--output",
            str(output),
        ]
    )
    with pytest.raises(SystemExit, match="conflicting product"):
        args.handler(args)
    assert output.read_text(encoding="utf-8") == "previous snapshot\n"

    collision = cli_module.build_parser().parse_args(
        [
            "kroger-sync",
            "--term",
            "magnesium",
            "--location-id",
            "01400943",
            "--output",
            str(output),
            "--report",
            str(output),
        ]
    )
    with pytest.raises(SystemExit, match="output = report"):
        collision.handler(collision)


def test_failed_feed_quality_gate_preserves_previous_output(tmp_path: Path) -> None:
    output = tmp_path / "products.jsonl"
    report = tmp_path / "import-report.json"
    output.write_text("previous snapshot\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "suparch.cli",
            "import-iherb-feed",
            "--input",
            str(AFFILIATE_FIXTURE),
            "--output",
            str(output),
            "--report",
            str(report),
            "--min-gtin-coverage",
            "0.5",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "GTIN coverage 33.33% is below minimum 50.00%" in result.stderr
    assert output.read_text(encoding="utf-8") == "previous snapshot\n"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["quality"]["passed"] is False
    assert payload["stats"]["imported"] == 3


def test_feed_report_cannot_overwrite_input_or_database_artifacts(
    tmp_path: Path,
) -> None:
    feed = tmp_path / "feed.csv"
    feed.write_text(AFFILIATE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    original = feed.read_text(encoding="utf-8")
    output = tmp_path / "products.jsonl"

    input_collision = subprocess.run(
        [
            sys.executable,
            "-m",
            "suparch.cli",
            "import-iherb-feed",
            "--input",
            str(feed),
            "--output",
            str(output),
            "--report",
            str(feed),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    database = tmp_path / "catalog.sqlite"
    artifact_collision = subprocess.run(
        [
            sys.executable,
            "-m",
            "suparch.cli",
            "import-iherb-feed",
            "--input",
            str(feed),
            "--output",
            str(output),
            "--database",
            str(database),
            "--report",
            f"{database}.manifest.json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert input_collision.returncode != 0
    assert artifact_collision.returncode != 0
    assert "Catalog paths must be distinct" in input_collision.stderr
    assert "report = database manifest" in artifact_collision.stderr
    assert feed.read_text(encoding="utf-8") == original


def test_rejects_blank_category_argument(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "suparch.cli",
            "import-iherb-feed",
            "--input",
            str(AFFILIATE_FIXTURE),
            "--output",
            str(tmp_path / "products.jsonl"),
            "--category",
            " ",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must not be blank" in result.stderr


def test_successful_feed_quality_gate_writes_report(tmp_path: Path) -> None:
    output = tmp_path / "products.jsonl"
    report = tmp_path / "import-report.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "suparch.cli",
            "import-iherb-feed",
            "--input",
            str(AFFILIATE_FIXTURE),
            "--output",
            str(output),
            "--report",
            str(report),
            "--min-products",
            "3",
            "--min-gtin-coverage",
            "0.3",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert "gtin_coverage=33.33%" in result.stdout
    assert len(output.read_text(encoding="utf-8").splitlines()) == 3
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["quality"]["passed"] is True


def test_failed_enrichment_gate_preserves_previous_output(tmp_path: Path) -> None:
    iherb = tmp_path / "iherb.jsonl"
    dsld = tmp_path / "empty-dsld.jsonl"
    output = tmp_path / "enriched.jsonl"
    report = tmp_path / "enrichment-report.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "suparch.cli",
            "import-iherb-feed",
            "--input",
            str(AFFILIATE_FIXTURE),
            "--output",
            str(iherb),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    dsld.write_text("", encoding="utf-8")
    output.write_text("previous enriched snapshot\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "suparch.cli",
            "enrich-dsld",
            "--iherb",
            str(iherb),
            "--dsld",
            str(dsld),
            "--output",
            str(output),
            "--report",
            str(report),
            "--require-label",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "enrichment produced no products" in result.stderr
    assert output.read_text(encoding="utf-8") == "previous enriched snapshot\n"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["quality"]["passed"] is False
    assert payload["stats"]["total"] == 3
    assert payload["stats"]["labeled"] == 0


def test_enrichment_report_cannot_overwrite_dsld_sync_metadata(
    tmp_path: Path,
) -> None:
    iherb = tmp_path / "iherb.jsonl"
    dsld = tmp_path / "dsld.jsonl"
    sidecar = Path(f"{dsld}.sync.json")
    iherb.write_text("", encoding="utf-8")
    dsld.write_text("", encoding="utf-8")
    sidecar.write_text("provenance\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "suparch.cli",
            "enrich-dsld",
            "--iherb",
            str(iherb),
            "--dsld",
            str(dsld),
            "--output",
            str(tmp_path / "enriched.jsonl"),
            "--report",
            str(sidecar),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "DSLD sync metadata = report" in result.stderr
    assert sidecar.read_text(encoding="utf-8") == "provenance\n"
