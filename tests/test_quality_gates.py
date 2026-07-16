import json
import subprocess
import sys
from pathlib import Path

AFFILIATE_FIXTURE = Path(__file__).parent / "fixtures" / "iherb_affiliate_feed.csv"


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
