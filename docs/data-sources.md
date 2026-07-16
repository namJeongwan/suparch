# Data sources

## iHerb product source

iHerb remains Suparch's primary product source. Its current `robots.txt`
disallows automated search URLs and publishes a product sitemap. Standard
product paths are not disallowed by path, but crawler requests currently
receive HTTP 403, so Suparch does not attempt bypasses.

The supported production route is an authorized affiliate feed/API or saved
product pages supplied by an operator. iHerb's official affiliate page lists
Partnerize, Impact, CJ, and Awin and accepts shopping-comparison partners.
The `iherb-discover` command can inventory published product sitemap references,
but the sitemap spans non-supplement departments and does not itself provide
authorization, prices, or complete Supplement Facts.

The English MVP accepts approved affiliate catalogs through
`import-iherb-feed`. Input must be UTF-8 CSV or CSV.GZ. The importer accepts
common retail-feed aliases for name, manufacturer/brand, URL, current price,
currency, GTIN/UPC, and category while enforcing `en-US`, USD, valid iHerb
product URLs, and English supplement-category keywords. Feed metadata and
Supplement Facts remain separate: an offer-only product must be enriched by a
matching label before ingredient tools can return complete results.

## NIH DSLD enrichment

The NIH Office of Dietary Supplements' DSLD v9 API can optionally enrich iHerb
records that have a matching UPC:

- <https://api.ods.od.nih.gov/dsld/v9/>
- <https://ods.od.nih.gov/Research/Dietary_Supplement_Label_Database.aspx>

DSLD data is released under CC0. Suparch can map label IDs, UPCs, market status,
serving data, supplement form, target groups, active ingredient rows, forms,
amounts, daily values, and other ingredients into its normalized Product model.
The enriched iHerb record keeps its iHerb identity, URL, and offer; its parser
provenance records the matched DSLD label ID and parser version. Standalone
DSLD records are not iHerb products and are not the default public catalog.

Each JSONL sync has a provenance sidecar. A completed snapshot must be refreshed
with a clean `--no-resume` run so changed formulations and market status are
reconciled instead of silently mixed with older records.

## iHerb access status

Suparch does not bypass `robots.txt`, authentication, rate limits, browser
challenges, or other access controls.

As checked during development on 2026-07-17, the product path was allowed by
the published robots rules, but the product request itself returned HTTP 403.
The live-fetch CLI therefore fails closed with an authorized-input message.

The public iHerb Affiliate Program welcomes approved publishers including
shopping-comparison sites, but participation requires application through an
approved affiliate platform:

- <https://www.iherb.com/info/affiliates>
- <https://www.iherb.com/info/terms-of-use>

Affiliate approval does not automatically mean that crawling is allowed.
Operators should request an authorized product feed or written data-access
permission through the applicable program.

## Supported inputs

Suparch's catalog pipeline accepts:

1. Normalized Product JSON arrays.
2. One Product JSON object.
3. Newline-delimited Product JSON (`.jsonl`).
4. Saved product HTML supplied by an authorized operator.
5. A saved-HTML manifest for one atomic batch update.
6. NIH DSLD v9 API synchronization as optional enrichment input.
7. iHerb sitemap product references for authorized downstream ingestion.
8. Approved English/USD iHerb affiliate catalogs in CSV or CSV.GZ format.

This keeps the parser and MCP server useful while data acquisition remains a
separate, explicitly authorized concern.

## Snapshot publication

Each catalog build produces:

```text
catalog.sqlite
catalog.sqlite.sha256
catalog.sqlite.manifest.json
```

The manifest includes schema version, product count, byte size, generation
time, and SHA-256. Publish all three files together using versioned object
names. The MCP runtime should receive the expected checksum through
`SUPARCH_CATALOG_SHA256` or obtain it from
`SUPARCH_CATALOG_MANIFEST_URL`. Operators may use
`SUPARCH_CATALOG_POINTER_URL` to bind an immutable release URL to its checksum.
