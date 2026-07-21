# iHerb affiliate feed onboarding

Suparch must not publish a real iHerb catalog until both feed access and the
intended data use are approved. iHerb currently lists Partnerize, Impact, CJ,
and Awin as official platforms and accepts shopping-comparison publishers:

- <https://www.iherb.com/info/affiliates>
- <https://www.iherb.com/info/terms-of-use>

## Suggested application profile

- Website: `https://github.com/namJeongwan/suparch`
- Publisher type: shopping comparison / content / software tool
- Market: United States
- Language and currency: English (`en-US`) and USD
- Promotion method: structured product search and comparison with attributable
  deep links; no medical diagnosis or supplement prescription

Suggested English description:

> Suparch is an open-source Model Context Protocol server for searching and
> comparing dietary supplements by objective product and Supplement Facts
> data. It helps users inspect product names, brands, prices, ingredient forms,
> per-serving amounts, and overlapping ingredients. The service does not
> diagnose conditions or prescribe supplements. We plan to support the US
> English market and link users to the corresponding iHerb product pages.

## Ask before using the feed

Request an English/USD product catalog or API containing, where available:

- stable product or catalog item ID;
- product name and brand/manufacturer;
- direct iHerb product URL and affiliate deep link;
- current price and ISO currency;
- GTIN, UPC, or EAN;
- category or product type;
- stock or market availability;
- feed update timestamp.

Also obtain a written answer to these questions:

1. May Suparch cache normalized product metadata and prices in SQLite?
2. May a public MCP endpoint return those factual fields to end users?
3. May Suparch publish the derived SQLite snapshot, or must each operator keep
   its affiliate feed and snapshot private?
4. What refresh interval and price-staleness rules are required?
5. Must returned product URLs use the network's tracked affiliate deep link?

Approval to participate in an affiliate program does not by itself prove that
raw feed redistribution or a public derived snapshot is permitted. If public
redistribution is not approved, operators can still build private snapshots;
the Suparch MCP runtime remains read-only and does not expose feed credentials.

## Safe handoff

Never commit the raw feed, platform credentials, or access URLs. Place a
downloaded CSV or CSV.GZ under the ignored `build/` directory and run:

```bash
uv run suparch-catalog import-iherb-feed \
  --input build/iherb-us-feed.csv.gz \
  --output build/iherb-products.jsonl \
  --report build/iherb-import-report.json
```

Review the report, choose evidence-based minimum product and GTIN coverage
values, then rerun with the quality gates. After syncing DSLD, build only the
products with verified label rows:

```bash
uv run suparch-catalog enrich-dsld \
  --products build/iherb-products.jsonl \
  --dsld build/dsld-products.jsonl \
  --output build/enriched-iherb-products.jsonl \
  --report build/dsld-enrichment-report.json \
  --require-label \
  --database build/catalog.sqlite
```

The two report files contain aggregate counts and coverage ratios, not product
rows or affiliate credentials. The final database, manifest, and checksum are
published only after both quality gates pass.
