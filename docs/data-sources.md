# Data sources

## iHerb access status

Suparch does not bypass `robots.txt`, authentication, rate limits, browser
challenges, or other access controls.

As checked during development on 2026-07-16, iHerb's robots policy did not
permit the Suparch user agent to fetch public product URLs. The live-fetch CLI
therefore fails closed before requesting a product page.

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
`SUPARCH_CATALOG_SHA256`.
