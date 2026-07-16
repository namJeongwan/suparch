import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from suparch.catalog import (
    SQLiteCatalogBuilder,
    catalog_sha256,
    iter_catalog_inputs,
    write_catalog_artifacts,
)
from suparch.crawler import IHerbClient
from suparch.dsld import DsldClient, iter_dsld_products, sync_dsld_to_jsonl
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
    html = IHerbClient().fetch_product(args.url)
    product = IHerbProductParser().parse(
        html,
        url=args.url,
        locale=args.locale,
    )
    if args.database:
        _merge_products(args.database, [product])
        print(f"fetched and ingested {product.id} into {args.database}")
    else:
        _write_product(product, args.output)


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

    if args.database:
        SQLiteCatalogBuilder().build(
            iter_dsld_products(args.output),
            args.database,
            metadata={
                "built_at": datetime.now(UTC).isoformat(),
                "label_source": "NIH DSLD v9",
                "license": "CC0-1.0",
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

    verify = subparsers.add_parser("verify", help="Verify a SQLite catalog")
    verify.add_argument("--database", type=Path, required=True)
    verify.set_defaults(handler=_verify)

    dsld_sync = subparsers.add_parser(
        "dsld-sync",
        help="Sync real supplement labels from the NIH DSLD v9 API",
    )
    dsld_sync.add_argument("--output", type=Path, required=True)
    dsld_sync.add_argument("--database", type=Path)
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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
