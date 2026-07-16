import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from suparch.models import Money, Product
from suparch.normalization import PARSER_VERSION, build_ingredient

SERVING_SIZE_RE = re.compile(
    r"Serving Size\s*:?\s*(.+?)(?=Servings Per (?:Container|Bottle)|Amount Per Serving|$)",
    re.IGNORECASE,
)
SERVINGS_RE = re.compile(
    r"Servings Per (?:Container|Bottle)\s*:?\s*([\d.]+)",
    re.IGNORECASE,
)
PRODUCT_ID_RE = re.compile(r"(?:/|^)(\d+)(?:[/?#]|$)")


class IHerbProductParser:
    def parse(
        self,
        html: str,
        *,
        url: str,
        crawled_at: datetime | None = None,
        locale: str | None = None,
    ) -> Product:
        soup = BeautifulSoup(html, "lxml")
        structured = self._json_ld_product(soup)

        name = self._product_name(soup, structured)
        brand = self._brand(soup, structured)
        source_product_id = self._source_product_id(url, soup)
        upc = self._upc(soup, structured)
        active_ingredients, serving_size, servings = self._supplement_facts(soup)
        other_ingredients = self._other_ingredients(soup)
        price = self._price(structured)

        confidence = Decimal("1")
        if not active_ingredients:
            confidence = Decimal("0.5")

        return Product(
            id=f"iherb:{source_product_id}",
            source="iherb",
            source_product_id=source_product_id,
            name=name,
            brand=brand,
            upc=upc,
            serving_size=serving_size,
            servings_per_container=servings,
            active_ingredients=active_ingredients,
            other_ingredients=other_ingredients,
            price=price,
            product_url=url,
            crawled_at=crawled_at or datetime.now(UTC),
            locale=locale,
            parser_version=PARSER_VERSION,
            parser_confidence=confidence,
        )

    @staticmethod
    def _json_ld_product(soup: BeautifulSoup) -> dict[str, object]:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(script.get_text(strip=True))
            except (json.JSONDecodeError, TypeError):
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                graph = candidate.get("@graph")
                if isinstance(graph, list):
                    candidates.extend(graph)
                if candidate.get("@type") == "Product":
                    return candidate
        return {}

    @staticmethod
    def _product_name(soup: BeautifulSoup, structured: dict[str, object]) -> str:
        value = structured.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
        heading = soup.find("h1")
        if heading:
            return heading.get_text(" ", strip=True)
        raise ValueError("Could not find product name")

    @staticmethod
    def _brand(soup: BeautifulSoup, structured: dict[str, object]) -> str:
        value = structured.get("brand")
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        if isinstance(value, str) and value.strip():
            return value.strip()
        element = soup.select_one(
            "[data-ga-event-action='Brand'], .brand-name, [itemprop='brand']"
        )
        if element:
            return element.get_text(" ", strip=True)
        heading = soup.find("h1")
        if heading:
            text = heading.get_text(" ", strip=True)
            if "," in text:
                return text.split(",", 1)[0].strip()
        raise ValueError("Could not find product brand")

    @staticmethod
    def _source_product_id(url: str, soup: BeautifulSoup) -> str:
        parsed = urlparse(url)
        matches = PRODUCT_ID_RE.findall(parsed.path)
        if matches:
            return matches[-1]
        text = soup.get_text(" ", strip=True)
        product_code = re.search(r"Product code\s*:?\s*([A-Z0-9-]+)", text, re.I)
        if product_code:
            return product_code.group(1)
        slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            return slug
        raise ValueError("Could not determine source product id")

    @staticmethod
    def _upc(
        soup: BeautifulSoup,
        structured: dict[str, object],
    ) -> str | None:
        for key in ("gtin14", "gtin13", "gtin12", "gtin8", "gtin"):
            digits = _barcode_digits(structured.get(key))
            if digits:
                return digits

        text = soup.get_text(" ", strip=True)
        match = re.search(
            r"\b(?:UPC|GTIN(?:-1[234]|-8)?)\s*:?\s*([0-9][0-9 -]{6,20})",
            text,
            re.IGNORECASE,
        )
        return _barcode_digits(match.group(1)) if match else None

    def _supplement_facts(
        self,
        soup: BeautifulSoup,
    ) -> tuple[list, str | None, Decimal | None]:
        table = self._find_supplement_table(soup)
        if table is None:
            return [], None, None

        container = table.find_parent(["section", "div"]) or table.parent
        context = container.get_text(" ", strip=True) if container else ""
        serving_match = SERVING_SIZE_RE.search(context)
        servings_match = SERVINGS_RE.search(context)
        serving_size = serving_match.group(1).strip() if serving_match else None
        if serving_size:
            serving_size = re.split(
                r"Servings Per (?:Container|Bottle)",
                serving_size,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()
        servings = Decimal(servings_match.group(1)) if servings_match else None

        ingredients = []
        current_parent: str | None = None
        for row in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            if self._is_header_row(cells):
                continue
            label = cells[0].strip()
            amount_text = cells[1].strip()
            daily_value_text = cells[2].strip() if len(cells) > 2 else None
            if not label:
                continue
            ingredient = build_ingredient(
                label,
                amount_text,
                daily_value_text,
                raw_text=" | ".join(cells),
                parent_ingredient=current_parent if not amount_text else None,
            )
            ingredients.append(ingredient)
            if amount_text:
                current_parent = ingredient.canonical_name
        return ingredients, serving_size, servings

    @staticmethod
    def _find_supplement_table(soup: BeautifulSoup) -> Tag | None:
        candidates: list[tuple[int, Tag]] = []
        for table in soup.find_all("table"):
            text = table.get_text(" ", strip=True).casefold()
            score = 0
            if "amount per serving" in text:
                score += 2
            if "daily value" in text or "%dv" in text:
                score += 1
            previous = table.find_previous(["h2", "h3", "h4", "strong"])
            if previous and "supplement facts" in previous.get_text(" ", strip=True).casefold():
                score += 3
            if score:
                candidates.append((score, table))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    @staticmethod
    def _is_header_row(cells: list[str]) -> bool:
        text = " ".join(cells).casefold()
        return any(
            marker in text
            for marker in (
                "amount per serving",
                "% daily value",
                "%daily value",
                "supplement facts",
            )
        )

    @staticmethod
    def _other_ingredients(soup: BeautifulSoup) -> list[str]:
        heading = soup.find(
            lambda tag: isinstance(tag, Tag)
            and tag.name in {"h2", "h3", "h4", "strong"}
            and "other ingredients" in tag.get_text(" ", strip=True).casefold()
        )
        if heading is None:
            return []

        chunks: list[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
                break
            if isinstance(sibling, Tag):
                text = sibling.get_text(" ", strip=True)
            else:
                text = str(sibling).strip()
            if text:
                chunks.append(text)
            if chunks:
                break
        return _split_ingredient_list(" ".join(chunks))

    @staticmethod
    def _price(structured: dict[str, object]) -> Money | None:
        offers = structured.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            return None
        price = offers.get("price")
        currency = offers.get("priceCurrency")
        if price is None or not isinstance(currency, str):
            return None
        try:
            return Money(amount=Decimal(str(price)), currency=currency.upper())
        except Exception:
            return None


def _split_ingredient_list(value: str) -> list[str]:
    values: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        elif character in ",;" and depth == 0:
            item = value[start:index].strip(" .")
            if item:
                values.append(item)
            start = index + 1
    final = value[start:].strip(" .")
    if final:
        values.append(final)
    return values


def _barcode_digits(value: object) -> str | None:
    if value is None:
        return None
    digits = "".join(character for character in str(value) if character.isdigit())
    if 8 <= len(digits) <= 14:
        return digits
    return None
