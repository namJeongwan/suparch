import gzip
from datetime import UTC, datetime
from pathlib import Path

from suparch.affiliate import AffiliateFeedStats, IHerbAffiliateFeedReader

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
