import argparse
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from suparch.catalog import (
    SQLiteCatalogBuilder,
    catalog_sha256,
    iter_catalog_inputs,
    iter_json_catalog,
    write_catalog_artifacts,
)
from suparch.crawler import IHerbClient, IHerbSitemapClient
from suparch.dsld import DsldClient, iter_dsld_products, sync_dsld_to_jsonl
from suparch.enrichment import EnrichmentStats, enrich_iherb_with_dsld
from suparch.models import Product
from suparch.parser import IHerbProductParser
from suparch.repositories import SqliteCatalogRepository


def _write_product(product: Product, output: Path | None) -> None:
    payload = product.model_dump_json(indent=2)
    if output:
        output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


def _merge_products(database: Path, new_products: list[Product]) -> None:
    products: list[Product] = []
    if database.is_file():
        products = SqliteCatalogRepository(database).list_products()
    products_by_id = {existing.id: existing for existing in products}
    products_by_id.update({product.id: product for product in new_products})
    SQLiteCatalogBuilder().build(
        list(products_by_id.values()),
        database,
        metadata={"updated_at": datetime.now(UTC).isoformat()},
    )
    write_catalog_artifacts(database)


def _build(args: argparse.Namespace) -> None:
    SQLiteCatalogBuilder().build(
        iter_catalog_inputs(args.input),
        args.output,
        metadata={"built_at": datetime.now(UTC).isoformat()},
    )
    manifest_path, checksum_path = write_catalog_artifacts(args.output)
    with sqlite3.connect(
        f"file:{args.output.resolve()}?mode=ro",
        uri=True,
    ) as connection:
        product_count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    print(
        f"built {args.output} with {product_count} products; "
        f"manifest={manifest_path}; checksum={checksum_path}"
    )


def _parse_html(args: argparse.Namespace) -> None:
    product = IHerbProductParser().parse(
        args.input.read_text(encoding="utf-8"),
        url=args.url,
        locale=args.locale,
    )
    if args.database:
        _merge_products(args.database, [product])
        print(f"ingested {product.id} into {args.database}")
    else:
        _write_product(product, args.output)


def _fetch(args: argparse.Namespace) -> None:
    if not args.allow_live_fetch:
        raise SystemExit(
            "Live fetching is disabled by default. Re-run with --allow-live-fetch "
            "after reviewing the site's current terms and robots policy."
        )
    try:
        page = IHerbClient().fetch_product_page(args.url)
    except (PermissionError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
    product = IHerbProductParser().parse(
        page.html,
        url=page.url,
        locale=args.locale,
    )
    if args.database:
        _merge_products(args.database, [product])
        print(f"fetched and ingested {product.id} into {args.database}")
    else:
        _write_product(product, args.output)


def _iherb_discover(args: argparse.Namespace) -> None:
    if args.limit < 0:
        raise SystemExit("--limit must be zero or positive")
    limit = args.limit or None
    references = IHerbSitemapClient().iter_product_references(limit=limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.output.name}.",
        suffix=".tmp",
        dir=args.output.parent,
    )
    temporary = Path(temporary_name)
    count = 0
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            for reference in references:
                destination.write(
                    json.dumps(
                        {
                            "url": reference.url,
                            "last_modified": (
                                reference.last_modified.isoformat()
                                if reference.last_modified
                                else None
                            ),
                        }
                    )
                    + "\n"
                )
                count += 1
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, args.output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    print(f"discovered {count} iHerb product URLs in {args.output}")


def _parse_manifest(args: argparse.Namespace) -> None:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise SystemExit("Manifest must be a JSON array")

    parser = IHerbProductParser()
    products: list[Product] = []
    for item in manifest:
        if not isinstance(item, dict) or "input" not in item or "url" not in item:
            raise SystemExit("Each manifest item requires input and url")
        input_path = (args.manifest.parent / item["input"]).resolve()
        products.append(
            parser.parse(
                input_path.read_text(encoding="utf-8"),
                url=item["url"],
                locale=item.get("locale"),
            )
        )

    _merge_products(args.database, products)
    print(f"ingested {len(products)} products into {args.database}")


def _verify(args: argparse.Namespace) -> None:
    with sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        product_count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    checksum = catalog_sha256(args.database)
    checksum_path = Path(f"{args.database.resolve()}.sha256")
    if checksum_path.is_file():
        expected = checksum_path.read_text(encoding="utf-8").split()[0]
        if checksum != expected:
            raise SystemExit(
                f"catalog checksum mismatch: expected {expected}, got {checksum}"
            )
    if integrity != "ok":
        raise SystemExit(f"catalog integrity check failed: {integrity}")
    print(
        json.dumps(
            {
                "database": str(args.database),
                "integrity": integrity,
                "schema_version": schema_version,
                "product_count": product_count,
                "sha256": checksum,
            },
            indent=2,
        )
    )


def _dsld_sync(args: argparse.Namespace) -> None:
    status = {
        "off-market": 0,
        "on-market": 1,
        "all": 2,
    }[args.status]
    limit = args.limit if args.limit > 0 else None
    with DsldClient() as client:
        written = sync_dsld_to_jsonl(
            client=client,
            output=args.output,
            query=args.query,
            status=status,
            limit=limit,
            page_size=args.page_size,
            workers=args.workers,
            resume=args.resume,
        )
    print(f"wrote {written} new DSLD products to {args.output}")


def _enrich_dsld(args: argparse.Namespace) -> None:
    stats = EnrichmentStats()
    enriched = enrich_iherb_with_dsld(
        iter_json_catalog(args.iherb),
        iter_dsld_products(args.dsld),
        stats=stats,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.output.name}.",
        suffix=".tmp",
        dir=args.output.parent,
    )
    temporary = Path(temporary_name)
    count = 0
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            for product in enriched:
                destination.write(product.model_dump_json() + "\n")
                count += 1
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, args.output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    print(
        f"wrote {count} iHerb products to {args.output}; "
        f"DSLD matches={stats.matched}"
    )
    if args.database:
        SQLiteCatalogBuilder().build(
            iter_json_catalog(args.output),
            args.database,
            metadata={
                "built_at": datetime.now(UTC).isoformat(),
                "product_source": "iHerb",
                "label_enrichment": "NIH DSLD v9 by UPC",
            },
        )
        manifest_path, checksum_path = write_catalog_artifacts(args.database)
        print(
            f"built {args.database}; manifest={manifest_path}; "
            f"checksum={checksum_path}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suparch-catalog",
        description="Build and inspect immutable Suparch SQLite catalogs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build SQLite from a JSON catalog")
    build.add_argument(
        "--input",
        type=Path,
        action="append",
        required=True,
        help="JSON or JSONL input; repeat to merge multiple files",
    )
    build.add_argument("--output", type=Path, required=True)
    build.set_defaults(handler=_build)

    parse_html = subparsers.add_parser(
        "parse-html",
        help="Parse a saved iHerb product page",
    )
    parse_html.add_argument("--input", type=Path, required=True)
    parse_html.add_argument("--url", required=True)
    parse_html.add_argument("--locale")
    parse_html.add_argument("--output", type=Path)
    parse_html.add_argument("--database", type=Path)
    parse_html.set_defaults(handler=_parse_html)

    parse_manifest = subparsers.add_parser(
        "parse-manifest",
        help="Parse many saved product pages and publish one catalog update",
    )
    parse_manifest.add_argument("--manifest", type=Path, required=True)
    parse_manifest.add_argument("--database", type=Path, required=True)
    parse_manifest.set_defaults(handler=_parse_manifest)

    fetch = subparsers.add_parser(
        "fetch",
        help="Fetch and parse one public iHerb product URL",
    )
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--locale")
    fetch.add_argument("--output", type=Path)
    fetch.add_argument("--database", type=Path)
    fetch.add_argument("--allow-live-fetch", action="store_true")
    fetch.set_defaults(handler=_fetch)

    iherb_discover = subparsers.add_parser(
        "iherb-discover",
        help="Discover product URLs from iHerb's published sitemaps",
    )
    iherb_discover.add_argument("--output", type=Path, required=True)
    iherb_discover.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum product references; use 0 for all sitemap entries",
    )
    iherb_discover.set_defaults(handler=_iherb_discover)

    verify = subparsers.add_parser("verify", help="Verify a SQLite catalog")
    verify.add_argument("--database", type=Path, required=True)
    verify.set_defaults(handler=_verify)

    dsld_sync = subparsers.add_parser(
        "dsld-sync",
        help="Sync optional NIH DSLD enrichment labels to JSONL",
    )
    dsld_sync.add_argument("--output", type=Path, required=True)
    dsld_sync.add_argument("--query", default="*")
    dsld_sync.add_argument(
        "--status",
        choices=["on-market", "off-market", "all"],
        default="on-market",
    )
    dsld_sync.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum total JSONL records; use 0 for all matching labels",
    )
    dsld_sync.add_argument("--page-size", type=int, default=100)
    dsld_sync.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    dsld_sync.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    dsld_sync.set_defaults(handler=_dsld_sync)

    enrich_dsld = subparsers.add_parser(
        "enrich-dsld",
        help="Enrich iHerb products with DSLD labels matched by UPC",
    )
    enrich_dsld.add_argument("--iherb", type=Path, required=True)
    enrich_dsld.add_argument("--dsld", type=Path, required=True)
    enrich_dsld.add_argument("--output", type=Path, required=True)
    enrich_dsld.add_argument("--database", type=Path)
    enrich_dsld.set_defaults(handler=_enrich_dsld)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
