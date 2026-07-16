from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, HttpUrl, StringConstraints

NormalizedName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, to_lower=True),
]


class Ingredient(BaseModel):
    """One active ingredient row from a Supplement Facts label."""

    canonical_name: NormalizedName
    label_name: str = Field(min_length=1)
    form: str | None = None
    amount: Decimal | None = Field(default=None, ge=0)
    unit: str | None = None
    normalized_amount: Decimal | None = Field(default=None, ge=0)
    normalized_unit: str | None = None
    daily_value_percent: Decimal | None = Field(default=None, ge=0)
    raw_text: str | None = None
    parent_ingredient: str | None = None
    confidence: Decimal = Field(default=Decimal("1"), ge=0, le=1)


class Money(BaseModel):
    amount: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Product(BaseModel):
    """Normalized product label with source provenance."""

    id: str = Field(min_length=1)
    source: NormalizedName
    source_product_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    brand: str = Field(min_length=1)
    serving_size: str | None = None
    servings_per_container: Decimal | None = Field(default=None, gt=0)
    active_ingredients: list[Ingredient] = Field(default_factory=list)
    other_ingredients: list[str] = Field(default_factory=list)
    price: Money | None = None
    product_url: HttpUrl
    crawled_at: datetime
    locale: str | None = None
    parser_version: str | None = None
    parser_confidence: Decimal = Field(default=Decimal("1"), ge=0, le=1)


class ProductSummary(BaseModel):
    id: str
    name: str
    brand: str
    active_ingredients: list[Ingredient]
    price: Money | None
    product_url: HttpUrl
    crawled_at: datetime

    @classmethod
    def from_product(cls, product: Product) -> "ProductSummary":
        return cls.model_validate(product.model_dump())


class ProductSearchQuery(BaseModel):
    query: str | None = None
    include_ingredients: list[NormalizedName] = Field(default_factory=list)
    exclude_ingredients: list[NormalizedName] = Field(default_factory=list)
    forms: list[NormalizedName] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    max_price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


class ProductSearchResult(BaseModel):
    products: list[ProductSummary]
    total: int


class ComparisonEntry(BaseModel):
    product_id: str
    product_name: str
    label_name: str
    form: str | None = None
    amount: Decimal | None = None
    unit: str | None = None
    daily_value_percent: Decimal | None = None


class IngredientComparison(BaseModel):
    canonical_name: str
    entries: list[ComparisonEntry]


class ProductComparisonResult(BaseModel):
    products: list[ProductSummary]
    ingredients: list[IngredientComparison]
    common_ingredients: list[str]


class StackSelection(BaseModel):
    product_id: str
    servings_per_day: Decimal = Field(default=Decimal("1"), gt=0)


class StackContribution(BaseModel):
    product_id: str
    product_name: str
    servings_per_day: Decimal
    amount: Decimal
    unit: str


class StackTotal(BaseModel):
    canonical_name: str
    total_amount: Decimal
    unit: str
    contributions: list[StackContribution]


class StackResult(BaseModel):
    products: list[ProductSummary]
    totals: list[StackTotal]
    duplicate_ingredients: list[str]
