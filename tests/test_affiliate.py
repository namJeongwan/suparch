import gzip
from datetime import UTC, datetime
from pathlib import Path

import pytest

from suparch.affiliate import (
    AffiliateFeedPolicy,
    AffiliateFeedStats,
    DuplicateProductConflictError,
    IHerbAffiliateFeedReader,
    affiliate_feed_quality_failures,
)

FIXTURE = Path(__file__).parent / "fixtures" / "iherb_affiliate_feed.csv"


def test_imports_only_english_usd_iherb_supplements() -> None:
    stats = AffiliateFeedStats()
    products = list(
        IHerbAffiliateFeedReader().iter_products(
            FIXTURE,
            stats=stats,
            imported_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
    )

    assert [product.id for product in products] == [
        "iherb:12345",
        "iherb:55555",
        "iherb:66666",
    ]
    assert products[0].name == "Doctor's Best, Magnesium Glycinate"
    assert products[0].brand == "Doctor's Best"
    assert products[0].upc == "00012345678905"
    assert products[0].price is not None
    assert str(products[0].price.amount) == "19.99"
    assert products[0].price.currency == "USD"
    assert products[0].locale == "en-US"
    assert products[0].active_ingredients == []
    assert stats.total == 7
    assert stats.imported == 3
    assert stats.duplicates == 1
    assert stats.non_supplement == 1
    assert stats.non_usd == 1
    assert stats.invalid == 1
    assert stats.missing_gtin == 1
    assert stats.invalid_gtin == 1
    assert stats.valid_gtins == 1
    assert stats.gtin_coverage == 1 / 3
    assert stats.as_dict()["gtin_coverage"] == 0.333333


def test_reports_affiliate_feed_quality_failures() -> None:
    stats = AffiliateFeedStats(total=10, imported=3, missing_gtin=1, invalid_gtin=1)

    failures = affiliate_feed_quality_failures(
        stats,
        min_products=4,
        min_gtin_coverage=0.5,
    )

    assert failures == [
        "imported products 3 is below minimum 4",
        "GTIN coverage 33.33% is below minimum 50.00%",
    ]


def test_reads_gzip_feed(tmp_path: Path) -> None:
    compressed = tmp_path / "iherb.csv.gz"
    with gzip.open(compressed, "wt", encoding="utf-8", newline="") as output:
        output.write(FIXTURE.read_text(encoding="utf-8"))

    stats = AffiliateFeedStats()
    products = list(IHerbAffiliateFeedReader().iter_products(compressed, stats=stats))

    assert len(products) == 3
    assert stats.imported == 3


def test_skips_malformed_csv_row(tmp_path: Path) -> None:
    feed = tmp_path / "malformed.csv"
    feed.write_text(
        "Name,URL,Manufacturer,Price,Currency,Category\n"
        "Vitamin C,https://www.iherb.com/pr/vitamin-c/1,Example,9.99,USD,"
        "Supplements,unexpected\n",
        encoding="utf-8",
    )
    stats = AffiliateFeedStats()

    products = list(IHerbAffiliateFeedReader().iter_products(feed, stats=stats))

    assert products == []
    assert stats.invalid == 1


def test_uses_valid_barcode_when_an_earlier_alias_is_invalid(tmp_path: Path) -> None:
    feed = tmp_path / "barcodes.csv"
    feed.write_text(
        "Name,URL,Manufacturer,Price,Currency,Category,GTIN,UPC\n"
        "Vitamin C,https://www.iherb.com/pr/vitamin-c/1,Example,9.99,USD,"
        "Supplements,012345678904,012345678905\n",
        encoding="utf-8",
    )
    stats = AffiliateFeedStats()

    products = list(IHerbAffiliateFeedReader().iter_products(feed, stats=stats))

    assert products[0].upc == "00012345678905"
    assert stats.invalid_gtin == 0
    assert stats.gtin_coverage == 1


def test_rejects_conflicting_duplicate_product_rows(tmp_path: Path) -> None:
    feed = tmp_path / "duplicates.csv"
    feed.write_text(
        "Name,URL,Manufacturer,Price,Currency,Category,UPC\n"
        "Vitamin C,https://www.iherb.com/pr/vitamin-c/1,Example,9.99,USD,"
        "Supplements,012345678905\n"
        "Vitamin C,https://www.iherb.com/pr/vitamin-c/1,Example,10.99,USD,"
        "Supplements,012345678905\n",
        encoding="utf-8",
    )

    with pytest.raises(DuplicateProductConflictError, match="iherb:1"):
        list(IHerbAffiliateFeedReader().iter_products(feed))


def test_rejects_blank_category_policy() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        AffiliateFeedPolicy(category_keywords=(" ",))
