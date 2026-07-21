# Suparch

<!-- mcp-name: io.github.namjeongwan/suparch -->

Search supplements by what's inside.

Suparch is an open-source MCP server for searching, comparing, and calculating
structured supplement facts. It provides label data and deterministic
calculations; it does not diagnose conditions or recommend supplements.

## Current tools

- `search_products`: search by product text, included ingredients, excluded
  ingredients, ingredient forms, product type, target group, brand, and price.
- `get_product`: return the complete normalized label record for a product.
- `compare_products`: compare per-serving ingredients and forms.
- `calculate_stack`: add known label amounts for user-supplied daily servings.
- `get_catalog_info`: report snapshot schema, size, timestamps, and product count.

Production deployments use an immutable SQLite snapshot opened in read-only
mode. The crawler and catalog builder run separately from the public MCP
process, which makes the server suitable for ephemeral MCP Hub containers.

## English Kroger API MVP

The default acquisition path uses Kroger's public Products API for English
product identity, UPC, current USD price, and location-scoped availability. It
does not scrape retailer HTML. Create a Kroger developer application, choose a
store location ID, and keep the OAuth credentials outside the repository:

```bash
export KROGER_CLIENT_ID=<client-id>
export KROGER_CLIENT_SECRET=<client-secret>

uv run suparch-catalog kroger-sync \
  --location-id 01400943 \
  --term vitamin \
  --term magnesium \
  --category "vitamins & supplements" \
  --category "sports nutrition" \
  --limit-per-term 100 \
  --output build/kroger-products.jsonl \
  --report build/kroger-sync-report.json
```

Kroger prices depend on `--location-id`. Overlapping query results are
deduplicated, and non-supplement categories are rejected. Without explicit
`--category` values the filter accepts common supplement-only categories such
as `vitamins & supplements`, `dietary supplements`, and `sports nutrition`.
The report records products without a valid GTIN or price. Each retained offer
carries its Kroger location ID and available fulfillment modes into MCP
responses.
Credentials are accepted only through `KROGER_CLIENT_ID` and
`KROGER_CLIENT_SECRET`, so they do not appear in command history.

Kroger offers do not contain dependable Supplement Facts. Sync DSLD labels and
join them by UPC before building the public catalog:

```bash
uv run suparch-catalog dsld-sync \
  --query "*" \
  --status on-market \
  --limit 1000 \
  --output build/dsld-products.jsonl

uv run suparch-catalog enrich-dsld \
  --products build/kroger-products.jsonl \
  --dsld build/dsld-products.jsonl \
  --output build/enriched-products.jsonl \
  --report build/dsld-enrichment-report.json \
  --require-label \
  --database build/catalog.sqlite
```

Use several focused supplement terms for a toy catalog. A production snapshot
should use a deliberate query inventory and inspect coverage reports before
publication.

## Optional authorized iHerb affiliate feed

The optional importer accepts an approved iHerb affiliate catalog as UTF-8 CSV or CSV.GZ.
It deliberately keeps one market contract: English (`en-US`), USD prices, and
HTTPS `iherb.com` product URLs. Rows outside an English supplement category or
with another currency are excluded.

```bash
uv run suparch-catalog import-iherb-feed \
  --input build/iherb-us-feed.csv.gz \
  --report build/iherb-import-report.json \
  --min-products 1 \
  --min-gtin-coverage 0 \
  --output build/iherb-products.jsonl
```

`--min-products` and `--min-gtin-coverage` are atomic publication gates. Set
them after inspecting the first approved feed rather than guessing production
thresholds. A failed gate preserves the previous product output and writes a
non-product report containing counts and coverage ratios.

The importer recognizes common Impact and retail-feed column names for product
name, brand/manufacturer, URL, current price, currency, GTIN/UPC, and category.
It derives the canonical iHerb product ID from the `/pr/` URL, validates GTIN
check digits, removes duplicate products, and reports every skipped row class.
Use repeated `--category` options when an approved feed uses additional English
category names:

```bash
uv run suparch-catalog import-iherb-feed \
  --input build/iherb-us-feed.csv.gz \
  --category supplement \
  --category "sports nutrition" \
  --output build/iherb-products.jsonl
```

Affiliate catalogs usually provide offers rather than complete Supplement
Facts. Run the DSLD enrichment step below before relying on ingredient search,
comparison, or stack calculation. A direct `--database` build is useful for
offer/name search but can contain products without label rows.

## NIH DSLD label enrichment

NIH's Dietary Supplement Label Database (DSLD) supplies label facts when a
retail record can be matched by UPC. Standalone DSLD records must not be
presented as retailer inventory or current offers.

Sync a resumable DSLD JSONL enrichment source:

```bash
uv run suparch-catalog dsld-sync \
  --query magnesium \
  --status on-market \
  --limit 1000 \
  --output build/dsld-products.jsonl
```

Then enrich a Kroger or authorized affiliate Product JSON/JSONL source by UPC:

```bash
uv run suparch-catalog enrich-dsld \
  --products build/kroger-products.jsonl \
  --dsld build/dsld-products.jsonl \
  --output build/enriched-products.jsonl \
  --report build/dsld-enrichment-report.json \
  --min-label-coverage 0 \
  --require-label \
  --database build/catalog.sqlite
```

Enrichment also fails atomically when it produces no products or misses the
operator-selected `--min-label-coverage`. The report separates UPC matches from
products that actually contain ingredient rows.

Use `--limit 0` for the complete matching result set. The sync uses bounded
concurrency and retries, flushes each page to disk, resumes by DSLD label ID,
and repairs a truncated final JSONL record after an interrupted write. A sync
sidecar pins the query, market status, limit, API, and parser version so
incompatible runs cannot be mixed. Resume is only for interrupted runs; use
`--no-resume` to refresh a completed snapshot and reconcile changed labels.

## Quick start

```bash
uv sync --extra dev
uv run suparch
```

Suparch uses the bundled sample catalog by default. To use another catalog:

```bash
uv run suparch-catalog build \
  --input src/suparch/data/sample_catalog.json \
  --output catalog.sqlite

SUPARCH_DB_PATH=./catalog.sqlite uv run suparch
```

Run the MCP development inspector:

```bash
uv run mcp dev src/suparch/server.py
```

Run checks:

```bash
uv run pytest
uv run ruff check .
```

## Build and verify a catalog

```bash
uv run suparch-catalog build \
  --input products.json \
  --output catalog.sqlite

uv run suparch-catalog verify \
  --database catalog.sqlite
```

The builder writes to a temporary file, runs SQLite integrity checks, and
publishes the final database with an atomic rename. It also creates:

```text
catalog.sqlite.sha256
catalog.sqlite.manifest.json
```

Inputs may be a JSON object, JSON array, or JSONL file. Repeat `--input` to
merge multiple normalized files into one snapshot.

## Parse a saved iHerb product page

```bash
uv run suparch-catalog parse-html \
  --input product.html \
  --url https://www.iherb.com/pr/example-product/12345 \
  --output product.json
```

Discover product references from iHerb's published sitemap without using its
disallowed search path:

```bash
uv run suparch-catalog iherb-discover \
  --limit 1000 \
  --output build/iherb-product-refs.jsonl
```

The sitemap contains multiple iHerb departments, so these references are only
discovery input. An authorized feed or page-ingestion job must filter the
supplement category before publishing a Suparch catalog.

To merge the parsed product directly into a catalog snapshot:

```bash
uv run suparch-catalog parse-html \
  --input product.html \
  --url https://www.iherb.com/pr/example-product/12345 \
  --database catalog.sqlite
```

For batch ingestion, use a manifest so the catalog is loaded and published only
once:

```json
[
  {
    "input": "pages/product-12345.html",
    "url": "https://www.iherb.com/pr/example-product/12345",
    "locale": "en-US"
  }
]
```

```bash
uv run suparch-catalog parse-manifest \
  --manifest crawl-manifest.json \
  --database catalog.sqlite
```

Single-page live fetching exists for development but is disabled unless the
operator explicitly passes `--allow-live-fetch`. It checks the current
`robots.txt`, rate limits requests, and does not implement authentication,
anti-bot bypass, or browser fingerprint evasion. Operators remain responsible
for reviewing the site's current terms before using it.

At the time of the latest project check, standard product paths were allowed by
the published robots rules, but unauthenticated product requests returned HTTP
403. Suparch preserves that fail-closed behavior. See
[docs/data-sources.md](docs/data-sources.md) for approved-input options and the
official affiliate path.

## Hub deployment

Hub deployments require an API- or feed-backed retail catalog. Suparch does not
ship NIH records as a substitute for inventory, pricing, or availability.

Run the stateless Streamable HTTP transport:

```bash
SUPARCH_TRANSPORT=streamable-http \
SUPARCH_HOST=0.0.0.0 \
PORT=8000 \
SUPARCH_DB_PATH=/data/catalog.sqlite \
uv run suparch
```

If the Hub cannot mount persistent files, publish the catalog to versioned
object storage:

```bash
SUPARCH_CATALOG_URL=https://cdn.example.com/catalog-2026-07-16.sqlite \
SUPARCH_CATALOG_SHA256=<sha256> \
SUPARCH_TRANSPORT=streamable-http \
uv run suparch
```

Suparch downloads and validates the artifact during startup, then opens it
read-only. The MCP endpoint defaults to `/mcp`.

Build the container:

```bash
docker build -t suparch .
docker run --rm -p 8000:8000 \
  -e SUPARCH_CATALOG_URL=https://cdn.example.com/catalog.sqlite \
  -e SUPARCH_CATALOG_SHA256=<sha256> \
  suparch
```

If `SUPARCH_CATALOG_SHA256` is omitted for a custom URL, Suparch derives the
manifest URL by appending `.manifest.json`. Set
`SUPARCH_CATALOG_MANIFEST_URL` only when the manifest lives elsewhere.

## MCP Registry

The repository includes an official Registry-compatible `server.json` for the
OCI package `ghcr.io/namjeongwan/suparch`. Version tags such as `v0.1.0`
publish the corresponding image through GitHub Actions. Registry publication
should only run after that exact image tag is publicly available.

## JSON catalog format

```json
[
  {
    "id": "source:product-id",
    "source": "example",
    "source_product_id": "product-id",
    "name": "Magnesium Glycinate",
    "brand": "Example Labs",
    "serving_size": "2 capsules",
    "servings_per_container": 60,
    "active_ingredients": [
      {
        "canonical_name": "magnesium",
        "label_name": "Magnesium",
        "form": "magnesium glycinate",
        "amount": "200",
        "unit": "mg",
        "normalized_amount": "200000",
        "normalized_unit": "mcg",
        "daily_value_percent": "48"
      }
    ],
    "other_ingredients": ["hypromellose"],
    "price": {
      "amount": "19.99",
      "currency": "USD"
    },
    "product_url": "https://example.com/products/product-id",
    "crawled_at": "2026-07-16T00:00:00Z"
  }
]
```

All label strings are preserved alongside normalized values. Normalization must
never destroy the source label text.

DSLD quantity and daily-value operators are preserved. Non-equality amounts
such as `< 1 g` remain visible in label details but are excluded from stack
arithmetic because they are not exact values. When a label provides different
daily values for adults, children, pregnancy, or lactation, every target-group
entry is returned instead of presenting the first percentage as universal.

Search results return compact summaries with at most 20 canonical ingredient
names plus the total ingredient count. Use `get_product` for the complete label.

## Architecture

```text
Kroger Products API -------------------> retail Product JSONL
authorized affiliate feed ------------> retail Product JSONL
                                                  |
                                       DSLD enrichment by UPC
                                                  |
                                                  v
                                        immutable SQLite snapshot
                                                  |
MCP client -> stateless Suparch server -> read-only repository
```

The MCP layer only retrieves and calculates product facts. Domain-specific
skills or clients remain responsible for interpreting symptoms, selecting
nutrient targets, and presenting medical guidance.

See [docs/architecture.md](docs/architecture.md) for the deployment and data
publication design.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `SUPARCH_DB_PATH` | Read-only SQLite file mounted into the runtime |
| `SUPARCH_CATALOG_POINTER_URL` | Operator-managed pointer containing an immutable catalog URL and SHA-256 |
| `SUPARCH_CATALOG_URL` | HTTPS SQLite artifact downloaded on startup |
| `SUPARCH_CATALOG_MANIFEST_URL` | Optional custom manifest URL; defaults to `<catalog URL>.manifest.json` |
| `SUPARCH_CATALOG_SHA256` | Optional artifact checksum |
| `SUPARCH_CATALOG_CACHE_PATH` | Download destination, default `/tmp/suparch/catalog.sqlite` |
| `SUPARCH_TRANSPORT` | `stdio`, `sse`, or `streamable-http` |
| `SUPARCH_HOST` | HTTP bind host |
| `SUPARCH_PORT` / `PORT` | HTTP port |
| `SUPARCH_MCP_PATH` | Streamable HTTP endpoint, default `/mcp` |

## iHerb data acquisition

iHerb's current robots policy disallows automated `/search` URLs and advertises
product sitemaps. Standard product pages are not disallowed by path, but
unauthenticated crawler requests currently receive HTTP 403. Suparch does not
evade that access control.

The production path is:

```text
approved iHerb affiliate feed/API or operator-supplied pages
  -> iHerb Product records
  -> optional DSLD enrichment by UPC
  -> immutable SQLite snapshot
  -> read-only MCP
```

iHerb lists Partnerize, Impact, CJ, and Awin as official affiliate platforms
and explicitly accepts shopping-comparison partners. An approved feed or
written data-access permission is therefore the next external dependency.
See [docs/affiliate-onboarding.md](docs/affiliate-onboarding.md) for the exact
application text, requested feed fields, licensing questions, and safe handoff.
