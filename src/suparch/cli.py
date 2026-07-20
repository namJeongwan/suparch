import argparse
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from suparch.affiliate import (
    AffiliateFeedPolicy,
    AffiliateFeedStats,
    IHerbAffiliateFeedReader,
    affiliate_feed_quality_failures,
)
from suparch.catalog import (
    SQLiteCatalogBuilder,
    catalog_sha256,
    iter_catalog_inputs,
    iter_json_catalog,
    write_catalog_artifacts,
)
from suparch.crawler import IHerbClient, IHerbSitemapClient
from suparch.dsld import DsldClient, iter_dsld_products, sync_dsld_to_jsonl
from suparch.enrichment import (
    EnrichmentStats,
    enrich_products_with_dsld,
    enrichment_quality_failures,
)
from suparch.kroger import (
    DEFAULT_SUPPLEMENT_CATEGORY_KEYWORDS,
    KrogerClient,
    KrogerSyncStats,
    iter_kroger_products,
)
from suparch.models import Product
from suparch.parser import IHerbProductParser
from suparch.repositories import SqliteCatalogRepository


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _unit_interval(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def _category_keyword(value: str) -> str:
    parsed = value.strip()
    if not parsed:
        raise argparse.ArgumentTypeError("must not be blank")
    return parsed


def _validate_catalog_paths(
    paths: dict[str, Path | None],
    *,
    database: Path | None = None,
) -> None:
    candidates = {name: path for name, path in paths.items() if path is not None}
    if database is not None:
        candidates.update(
            {
                "database": database,
                "database manifest": Path(f"{database.resolve()}.manifest.json"),
                "database checksum": Path(f"{database.resolve()}.sha256"),
            }
        )

    by_path: dict[Path, list[str]] = {}
    for name, path in candidates.items():
        by_path.setdefault(path.resolve(), []).append(name)
    conflicts = [names for names in by_path.values() if len(names) > 1]
    if conflicts:
        labels = "; ".join(" = ".join(names) for names in conflicts)
        raise SystemExit(f"Catalog paths must be distinct: {labels}")


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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


def _import_iherb_feed(args: argparse.Namespace) -> None:
    _validate_catalog_paths(
        {
            "input": args.input,
            "output": args.output,
            "report": args.report,
        },
        database=args.database,
    )
    categories = tuple(args.category or ["supplement"])
    policy = AffiliateFeedPolicy(
        locale="en-US",
        currency="USD",
        category_keywords=categories,
    )
    stats = AffiliateFeedStats()
    products = IHerbAffiliateFeedReader(policy).iter_products(
        args.input,
        stats=stats,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.output.name}.",
        suffix=".tmp",
        dir=args.output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            for product in products:
                destination.write(product.model_dump_json() + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        failures = affiliate_feed_quality_failures(
            stats,
            min_products=args.min_products,
            min_gtin_coverage=args.min_gtin_coverage,
        )
        if args.report:
            _write_json_atomic(
                args.report,
                {
                    "source": "iHerb affiliate feed",
                    "locale": policy.locale,
                    "currency": policy.currency,
                    "category_keywords": list(categories),
                    "stats": stats.as_dict(),
                    "quality": {
                        "passed": not failures,
                        "min_products": args.min_products,
                        "min_gtin_coverage": args.min_gtin_coverage,
                        "failures": failures,
                    },
                },
            )
        if failures:
            raise ValueError("Affiliate feed quality check failed: " + "; ".join(failures))
        os.replace(temporary, args.output)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        raise SystemExit(str(error)) from None

    print(
        f"imported {stats.imported}/{stats.total} iHerb products; "
        f"non_supplement={stats.non_supplement}; non_usd={stats.non_usd}; "
        f"invalid={stats.invalid}; duplicates={stats.duplicates}; "
        f"missing_gtin={stats.missing_gtin}; invalid_gtin={stats.invalid_gtin}; "
        f"gtin_coverage={stats.gtin_coverage:.2%}; output={args.output}"
    )
    if args.database:
        SQLiteCatalogBuilder().build(
            iter_json_catalog(args.output),
            args.database,
            metadata={
                "built_at": datetime.now(UTC).isoformat(),
                "product_source": "iHerb affiliate feed",
                "locale": "en-US",
                "currency": "USD",
                "gtin_coverage": f"{stats.gtin_coverage:.6f}",
                "label_status": "affiliate metadata; run DSLD enrichment for facts",
            },
        )
        manifest_path, checksum_path = write_catalog_artifacts(args.database)
        print(
            f"built {args.database}; manifest={manifest_path}; "
            f"checksum={checksum_path}"
        )


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


def _kroger_sync(args: argparse.Namespace) -> None:
    _validate_catalog_paths({"output": args.output, "report": args.report})
    client_id = os.environ.get("KROGER_CLIENT_ID", "")
    client_secret = os.environ.get("KROGER_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise SystemExit(
            "KROGER_CLIENT_ID and KROGER_CLIENT_SECRET are required; "
            "create an application in the Kroger developer portal"
        )

    stats = KrogerSyncStats()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.output.name}.",
        suffix=".tmp",
        dir=args.output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with (
            KrogerClient(client_id, client_secret) as client,
            os.fdopen(descriptor, "w", encoding="utf-8") as destination,
        ):
            for product in iter_kroger_products(
                client,
                terms=args.term,
                location_id=args.location_id,
                limit_per_term=args.limit_per_term,
                category_keywords=(args.category or DEFAULT_SUPPLEMENT_CATEGORY_KEYWORDS),
                stats=stats,
            ):
                destination.write(product.model_dump_json() + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        if stats.imported == 0:
            raise ValueError("Kroger sync produced no products")
        if args.report:
            _write_json_atomic(
                args.report,
                {
                    "source": "Kroger Public Products API",
                    "location_id": args.location_id,
                    "terms": args.term,
                    "category_keywords": list(
                        args.category or DEFAULT_SUPPLEMENT_CATEGORY_KEYWORDS
                    ),
                    "limit_per_term": args.limit_per_term,
                    "stats": stats.as_dict(),
                },
            )
        os.replace(temporary, args.output)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        raise SystemExit(str(error)) from None

    print(
        f"wrote {stats.imported} Kroger products to {args.output}; "
        f"duplicates={stats.duplicates}; non_supplement={stats.non_supplement}; "
        f"invalid={stats.invalid}; "
        f"missing_gtin={stats.missing_gtin}; missing_price={stats.missing_price}"
    )


def _enrich_dsld(args: argparse.Namespace) -> None:
    products_path = args.products or args.iherb
    _validate_catalog_paths(
        {
            "retail product input": products_path,
            "DSLD input": args.dsld,
            "DSLD sync metadata": Path(f"{args.dsld.resolve()}.sync.json"),
            "output": args.output,
            "report": args.report,
        },
        database=args.database,
    )
    stats = EnrichmentStats()
    enriched = enrich_products_with_dsld(
        iter_json_catalog(products_path),
        iter_dsld_products(args.dsld),
        stats=stats,
        require_label=args.require_label,
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
        failures = enrichment_quality_failures(
            stats,
            output_products=count,
            min_label_coverage=args.min_label_coverage,
        )
        if args.report:
            _write_json_atomic(
                args.report,
                {
                    "source": "NIH DSLD v9 by UPC",
                    "require_label": args.require_label,
                    "output_products": count,
                    "stats": stats.as_dict(),
                    "quality": {
                        "passed": not failures,
                        "min_label_coverage": args.min_label_coverage,
                        "failures": failures,
                    },
                },
            )
        if failures:
            raise ValueError("Enrichment quality check failed: " + "; ".join(failures))
        os.replace(temporary, args.output)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        raise SystemExit(str(error)) from None

    print(
        f"wrote {count} retail products to {args.output}; "
        f"DSLD matches={stats.matched}; "
        f"missing labels skipped={stats.skipped_without_label}; "
        f"label_coverage={stats.label_coverage:.2%}"
    )
    if args.database:
        SQLiteCatalogBuilder().build(
            iter_json_catalog(args.output),
            args.database,
            metadata={
                "built_at": datetime.now(UTC).isoformat(),
                "product_source": "retail API/feed",
                "label_enrichment": "NIH DSLD v9 by UPC",
                "label_coverage": f"{stats.label_coverage:.6f}",
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

    import_iherb_feed = subparsers.add_parser(
        "import-iherb-feed",
        help="Import an approved English/USD iHerb affiliate CSV feed",
    )
    import_iherb_feed.add_argument("--input", type=Path, required=True)
    import_iherb_feed.add_argument("--output", type=Path, required=True)
    import_iherb_feed.add_argument("--database", type=Path)
    import_iherb_feed.add_argument("--report", type=Path)
    import_iherb_feed.add_argument("--min-products", type=_positive_int, default=1)
    import_iherb_feed.add_argument(
        "--min-gtin-coverage",
        type=_unit_interval,
        default=0.0,
        help="Fail atomically when valid GTIN coverage is below this 0..1 ratio",
    )
    import_iherb_feed.add_argument(
        "--category",
        action="append",
        type=_category_keyword,
        help="Required category substring; repeat to include more English categories",
    )
    import_iherb_feed.set_defaults(handler=_import_iherb_feed)

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

    kroger_sync = subparsers.add_parser(
        "kroger-sync",
        help="Sync English/USD offers from Kroger's public Products API",
    )
    kroger_sync.add_argument(
        "--term",
        action="append",
        required=True,
        type=_category_keyword,
        help="Product search term; repeat for more supplement queries",
    )
    kroger_sync.add_argument("--location-id", required=True)
    kroger_sync.add_argument("--limit-per-term", type=_positive_int, default=100)
    kroger_sync.add_argument(
        "--category",
        action="append",
        type=_category_keyword,
        help=(
            "Required category substring; defaults to known supplement-only categories"
        ),
    )
    kroger_sync.add_argument("--output", type=Path, required=True)
    kroger_sync.add_argument("--report", type=Path)
    kroger_sync.set_defaults(handler=_kroger_sync)

    enrich_dsld = subparsers.add_parser(
        "enrich-dsld",
        help="Enrich retail products with DSLD labels matched by UPC",
    )
    retail_input = enrich_dsld.add_mutually_exclusive_group(required=True)
    retail_input.add_argument("--products", type=Path)
    retail_input.add_argument(
        "--iherb",
        type=Path,
        help="Deprecated alias for --products",
    )
    enrich_dsld.add_argument("--dsld", type=Path, required=True)
    enrich_dsld.add_argument("--output", type=Path, required=True)
    enrich_dsld.add_argument("--database", type=Path)
    enrich_dsld.add_argument("--report", type=Path)
    enrich_dsld.add_argument(
        "--min-label-coverage",
        type=_unit_interval,
        default=0.0,
        help="Fail atomically when ingredient-label coverage is below this 0..1 ratio",
    )
    enrich_dsld.add_argument(
        "--require-label",
        action="store_true",
        help="Exclude products that still have no active Supplement Facts rows",
    )
    enrich_dsld.set_defaults(handler=_enrich_dsld)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
