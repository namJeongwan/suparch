import os

from mcp.server.fastmcp import FastMCP

from suparch.models import (
    Product,
    ProductComparisonResult,
    ProductSearchQuery,
    ProductSearchResult,
    StackResult,
    StackSelection,
)
from suparch.runtime import create_repository
from suparch.services import CatalogService


def create_service() -> CatalogService:
    return CatalogService(create_repository())


service = create_service()
mcp = FastMCP(
    "Suparch",
    instructions=(
        "Search and retrieve structured supplement label facts. "
        "Do not use these tools to diagnose conditions or prescribe supplements."
    ),
    host=os.environ.get("SUPARCH_HOST", "127.0.0.1"),
    port=int(os.environ.get("PORT", os.environ.get("SUPARCH_PORT", "8000"))),
    streamable_http_path=os.environ.get("SUPARCH_MCP_PATH", "/mcp"),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def search_products(
    query: str | None = None,
    include_ingredients: list[str] | None = None,
    exclude_ingredients: list[str] | None = None,
    forms: list[str] | None = None,
    brands: list[str] | None = None,
    max_price: float | None = None,
    currency: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> ProductSearchResult:
    """Search supplement labels using objective product and ingredient filters."""
    search = ProductSearchQuery(
        query=query,
        include_ingredients=include_ingredients or [],
        exclude_ingredients=exclude_ingredients or [],
        forms=forms or [],
        brands=brands or [],
        max_price=max_price,
        currency=currency,
        limit=limit,
        offset=offset,
    )
    return service.search_products(search)


@mcp.tool()
def get_product(product_id: str) -> Product:
    """Return one complete normalized supplement label record."""
    product = service.get_product(product_id)
    if product is None:
        raise ValueError(f"Unknown product_id: {product_id}")
    return product


@mcp.tool()
def compare_products(product_ids: list[str]) -> ProductComparisonResult:
    """Compare per-serving label facts across two or more products."""
    if len(set(product_ids)) < 2:
        raise ValueError("At least two distinct product_ids are required")
    if len(product_ids) > 20:
        raise ValueError("At most 20 product_ids can be compared at once")
    return service.compare_products(product_ids)


@mcp.tool()
def calculate_stack(selections: list[StackSelection]) -> StackResult:
    """Sum known label amounts for a user-supplied product and serving combination."""
    if len(selections) > 50:
        raise ValueError("At most 50 stack selections are allowed")
    return service.calculate_stack(selections)


def main() -> None:
    transport = os.environ.get("SUPARCH_TRANSPORT", "stdio")
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError(f"Unsupported SUPARCH_TRANSPORT: {transport}")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
