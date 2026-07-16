from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from suparch.parser import IHerbProductParser

FIXTURE = Path(__file__).parent / "fixtures" / "iherb_product.html"


def test_parses_iherb_supplement_label() -> None:
    product = IHerbProductParser().parse(
        FIXTURE.read_text(encoding="utf-8"),
        url="https://www.iherb.com/pr/example-labs-magnesium/12345",
        crawled_at=datetime(2026, 7, 16, tzinfo=UTC),
        locale="en-US",
    )

    assert product.id == "iherb:12345"
    assert product.brand == "Example Labs"
    assert product.upc == "00012345678905"
    assert product.serving_size == "2 Capsules"
    assert product.servings_per_container == Decimal("60")
    assert product.price is not None
    assert product.price.amount == Decimal("19.99")
    assert [item.canonical_name for item in product.active_ingredients] == [
        "magnesium",
        "vitamin d",
        "probiotic blend",
        "lactobacillus acidophilus",
        "probiotic cultures",
    ]
    assert product.active_ingredients[0].form == "magnesium glycinate"
    assert product.active_ingredients[3].amount is None
    assert product.active_ingredients[3].parent_ingredient == "probiotic blend"
    assert product.active_ingredients[4].amount == Decimal("50000000000")
    assert product.active_ingredients[4].unit == "CFU"
    assert product.other_ingredients == [
        "Hypromellose (capsule)",
        "rice flour",
        "silicon dioxide",
    ]


def test_rejects_iherb_page_without_supplement_facts() -> None:
    html = """
    <html>
      <head>
        <script type="application/ld+json">
          {
            "@type": "Product",
            "name": "Ordinary Shampoo",
            "brand": {"name": "Example"},
            "gtin12": "012345678905"
          }
        </script>
      </head>
      <body><h1>Ordinary Shampoo</h1></body>
    </html>
    """

    with pytest.raises(ValueError, match="refusing non-supplement"):
        IHerbProductParser().parse(
            html,
            url="https://www.iherb.com/pr/ordinary-shampoo/999",
        )
