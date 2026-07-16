import re
from decimal import Decimal, InvalidOperation

from suparch.models import DailyValue, Ingredient

PARSER_VERSION = "0.1.0"

ALIASES = {
    "magnesiium": "magnesium",
    "vitamin b1": "thiamin",
    "thiamine": "thiamin",
    "vitamin b2": "riboflavin",
    "vitamin b3": "niacin",
    "nicotinamide": "niacin",
    "vitamin b5": "pantothenic acid",
    "pantothenic acid": "pantothenic acid",
    "pyridoxine": "vitamin b6",
    "vitamin b6": "vitamin b6",
    "vitamin b7": "biotin",
    "vitamin d3": "vitamin d",
    "vitamin d2": "vitamin d",
    "cholecalciferol": "vitamin d",
    "ergocalciferol": "vitamin d",
    "vitamin b9": "folate",
    "folic acid": "folate",
    "folate dfe": "folate",
    "l methylfolate": "folate",
    "5 methyltetrahydrofolate": "folate",
    "vitamin b12": "vitamin b12",
    "cyanocobalamin": "vitamin b12",
    "methylcobalamin": "vitamin b12",
}

UNIT_ALIASES = {
    "g": "g",
    "mg": "mg",
    "mcg": "mcg",
    "µg": "mcg",
    "μg": "mcg",
    "ug": "mcg",
    "mcg dfe": "mcg",
    "milligram": "mg",
    "milligrams": "mg",
    "microgram": "mcg",
    "micrograms": "mcg",
    "gram": "g",
    "grams": "g",
    "gram(s)": "g",
    "i.u.": "IU",
    "iu": "IU",
    "cfu": "CFU",
    "cfus": "CFU",
}

MASS_TO_MCG = {
    "mcg": Decimal("1"),
    "mg": Decimal("1000"),
    "g": Decimal("1000000"),
}

FOOTNOTE_RE = re.compile(r"[*†‡]+")
SPACE_RE = re.compile(r"\s+")
FORM_RE = re.compile(r"\((?:as|from)\s+(.+?)\)", re.IGNORECASE)
AMOUNT_RE = re.compile(
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:(?P<magnitude>million|billion|trillion)\s+)?"
    r"(?P<unit>mcg\s+DFE|mcg|µg|μg|ug|mg|g|IU|I\.U\.|CFUs?)\b",
    re.IGNORECASE,
)

MAGNITUDES = {
    "million": Decimal("1000000"),
    "billion": Decimal("1000000000"),
    "trillion": Decimal("1000000000000"),
}
SUPERSCRIPT_FOOTNOTES = str.maketrans("", "", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def normalize_text(value: str) -> str:
    value = FOOTNOTE_RE.sub("", value)
    value = value.replace("®", "").replace("™", "")
    value = re.sub(r"[^0-9A-Za-z가-힣]+", " ", value)
    return SPACE_RE.sub(" ", value).strip().casefold()


def normalize_unit(value: str) -> str:
    cleaned = SPACE_RE.sub(" ", value.strip()).casefold()
    return UNIT_ALIASES.get(cleaned, value.strip())


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    cleaned = value.replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def split_label_and_form(label: str) -> tuple[str, str | None]:
    match = FORM_RE.search(label)
    form = match.group(1).strip() if match else None
    base = FORM_RE.sub("", label)
    base = re.sub(r"\s+", " ", base).strip(" ,;")
    return base, form


def canonicalize_ingredient(label: str) -> tuple[str, str | None]:
    base, form = split_label_and_form(label)
    normalized = normalize_text(base)

    canonical = ALIASES.get(normalized)
    if canonical is None:
        for alias, target in ALIASES.items():
            if normalized.startswith(alias):
                canonical = target
                break
    canonical = canonical or normalized

    return canonical, normalize_text(form) if form else None


def parse_amount(value: str | None) -> tuple[Decimal | None, str | None]:
    if not value:
        return None, None
    value = FOOTNOTE_RE.sub("", value).translate(SUPERSCRIPT_FOOTNOTES)
    match = AMOUNT_RE.search(value)
    if not match:
        return None, None
    amount = parse_decimal(match.group("amount"))
    magnitude = match.group("magnitude")
    if amount is not None and magnitude:
        amount *= MAGNITUDES[magnitude.casefold()]
    return amount, normalize_unit(match.group("unit"))


def normalize_amount(
    amount: Decimal | None,
    unit: str | None,
) -> tuple[Decimal | None, str | None]:
    if amount is None or unit is None:
        return None, None
    factor = MASS_TO_MCG.get(unit)
    if factor is not None:
        return amount * factor, "mcg"
    if unit in {"IU", "CFU"}:
        return amount, unit
    return None, None


def build_ingredient(
    label_name: str,
    amount_text: str | None,
    daily_value_text: str | None = None,
    *,
    raw_text: str | None = None,
    parent_ingredient: str | None = None,
    confidence: Decimal = Decimal("1"),
) -> Ingredient:
    canonical_name, form = canonicalize_ingredient(label_name)
    amount, unit = parse_amount(amount_text)
    normalized_amount, normalized_unit = normalize_amount(amount, unit)
    daily_value = parse_decimal(
        daily_value_text.replace("%", "") if daily_value_text else None
    )
    daily_values = (
        [DailyValue(percent=daily_value)]
        if daily_value is not None
        else []
    )
    return Ingredient(
        canonical_name=canonical_name,
        label_name=label_name.strip(),
        form=form,
        amount=amount,
        unit=unit,
        normalized_amount=normalized_amount,
        normalized_unit=normalized_unit,
        daily_value_percent=daily_value,
        daily_values=daily_values,
        raw_text=raw_text,
        parent_ingredient=parent_ingredient,
        confidence=confidence,
    )
