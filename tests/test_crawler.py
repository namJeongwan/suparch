from pathlib import Path

import httpx
import pytest

from suparch.crawler import (
    CrawlPolicy,
    IHerbClient,
    IHerbSitemapClient,
    _RobotsRules,
)

FIXTURE = Path(__file__).parent / "fixtures" / "iherb_product.html"
PRODUCT_URL = "https://www.iherb.com/pr/example-magnesium/12345"


def test_fetches_product_allowed_by_downloaded_robots() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text=(
                    "User-agent: *\n"
                    "Allow: /tr/list\n"
                    "\n"
                    "Disallow: /search\n"
                    "Disallow: /Search\n"
                ),
                request=request,
            )
        return httpx.Response(
            200,
            text=FIXTURE.read_text(encoding="utf-8"),
            request=request,
        )

    client = IHerbClient(
        CrawlPolicy(delay_seconds=0),
        transport=httpx.MockTransport(handler),
    )

    assert "Amount Per Serving" in client.fetch_product(PRODUCT_URL)


def test_fails_closed_when_robots_cannot_be_verified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    client = IHerbClient(
        CrawlPolicy(delay_seconds=0),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="Could not verify robots"):
        client.fetch_product(PRODUCT_URL)


def test_reports_authorized_input_path_when_product_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /search\n",
                request=request,
            )
        return httpx.Response(403, request=request)

    client = IHerbClient(
        CrawlPolicy(delay_seconds=0),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PermissionError, match="authorized affiliate feed"):
        client.fetch_product(PRODUCT_URL)


def test_discovers_product_references_from_published_sitemaps() -> None:
    index = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://www.iherb.com/sitemaps/products-0-www-0.xml</loc></sitemap>
      <sitemap><loc>https://www.iherb.com/sitemaps/blog-0-www-0.xml</loc></sitemap>
    </sitemapindex>
    """
    products = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url>
        <loc>https://www.iherb.com/pr/example-magnesium/12345</loc>
        <lastmod>2026-07-06T08:10:21+00:00</lastmod>
      </url>
      <url><loc>https://www.iherb.com/c/vitamins</loc></url>
    </urlset>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        payload = index if request.url.path == "/sitemap_index.xml" else products
        return httpx.Response(200, text=payload, request=request)

    client = IHerbSitemapClient(transport=httpx.MockTransport(handler))
    references = list(client.iter_product_references())

    assert [reference.url for reference in references] == [PRODUCT_URL]
    assert references[0].last_modified is not None


def test_rejects_sitemap_with_entity_declarations() -> None:
    payload = """<?xml version="1.0"?>
    <!DOCTYPE sitemapindex [<!ENTITY x "unsafe">]>
    <sitemapindex>&x;</sitemapindex>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=payload, request=request)

    client = IHerbSitemapClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="unsupported XML"):
        list(client.iter_product_references())


def test_rejects_utf16_sitemap_before_xml_parsing() -> None:
    payload = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE sitemapindex [<!ENTITY x "unsafe">]>'
        "<sitemapindex>&x;</sitemapindex>"
    ).encode("utf-16")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, request=request)

    client = IHerbSitemapClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="UTF-8"):
        list(client.iter_product_references())


def test_rejects_external_sitemap_before_requesting_it() -> None:
    requested_hosts: list[str] = []
    index = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://evil.example/sitemaps/products-0.xml</loc></sitemap>
    </sitemapindex>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        return httpx.Response(200, text=index, request=request)

    client = IHerbSitemapClient(
        CrawlPolicy(delay_seconds=0),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="Only HTTPS iHerb"):
        list(client.iter_product_references())
    assert requested_hosts == ["www.iherb.com"]


def test_rejects_external_product_redirect_before_requesting_it() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /search\n",
                request=request,
            )
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/pr/stolen/999"},
            request=request,
        )

    client = IHerbClient(
        CrawlPolicy(delay_seconds=0),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="Only HTTPS iHerb"):
        client.fetch_product(PRODUCT_URL)
    assert requested_hosts == ["www.iherb.com", "www.iherb.com"]


def test_robots_rules_support_iherb_wildcards_and_end_anchors() -> None:
    rules = _RobotsRules.parse(
        "User-agent: *\n"
        "Disallow: /pr/*/lib/*\n"
        "Disallow: /search$\n"
        "Allow: /pr/\n"
    )

    assert not rules.can_fetch("Suparch/0.2.1", "https://www.iherb.com/pr/a/lib/b")
    assert not rules.can_fetch("Suparch/0.2.1", "https://www.iherb.com/search")
    assert rules.can_fetch("Suparch/0.2.1", "https://www.iherb.com/search/results")
    assert rules.can_fetch("Suparch/0.2.1", PRODUCT_URL)


def test_returns_final_iherb_url_after_product_redirect() -> None:
    final_url = "https://www.iherb.com/pr/example-magnesium-new/54321"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /search\n",
                request=request,
            )
        if str(request.url) == PRODUCT_URL:
            return httpx.Response(
                302,
                headers={"Location": final_url},
                request=request,
            )
        return httpx.Response(200, text="final product", request=request)

    client = IHerbClient(
        CrawlPolicy(delay_seconds=0),
        transport=httpx.MockTransport(handler),
    )

    page = client.fetch_product_page(PRODUCT_URL)

    assert page.url == final_url
    assert page.html == "final product"


def test_sitemap_retry_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []
    index = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                request=request,
            )
        return httpx.Response(200, text=index, request=request)

    monkeypatch.setattr("suparch.crawler.time.sleep", sleeps.append)
    client = IHerbSitemapClient(
        CrawlPolicy(delay_seconds=0),
        transport=httpx.MockTransport(handler),
    )

    assert list(client.iter_product_references()) == []
    assert 7.0 in sleeps
