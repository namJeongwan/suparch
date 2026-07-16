import random
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx

from suparch import __version__


@dataclass(slots=True)
class CrawlPolicy:
    user_agent: str = (
        f"Suparch/{__version__} "
        "(+https://github.com/namJeongwan/suparch)"
    )
    timeout_seconds: float = 30
    delay_seconds: float = 2
    max_retries: int = 3
    max_redirects: int = 5


@dataclass(frozen=True, slots=True)
class IHerbProductReference:
    url: str
    last_modified: datetime | None = None


@dataclass(frozen=True, slots=True)
class FetchedProductPage:
    url: str
    html: str


@dataclass(frozen=True, slots=True)
class _RobotsRule:
    allow: bool
    pattern: str

    def matches(self, path: str) -> bool:
        anchored = self.pattern.endswith("$")
        pattern = self.pattern[:-1] if anchored else self.pattern
        expression = re.escape(pattern).replace(r"\*", ".*")
        suffix = "$" if anchored else ""
        return re.match(f"^{expression}{suffix}", path) is not None

    @property
    def precedence(self) -> int:
        return len(self.pattern.rstrip("$").replace("*", ""))


class _RobotsRules:
    def __init__(self, groups: list[tuple[list[str], list[_RobotsRule]]]) -> None:
        self.groups = groups

    @classmethod
    def parse(cls, text: str) -> "_RobotsRules":
        groups: list[tuple[list[str], list[_RobotsRule]]] = []
        agents: list[str] = []
        rules: list[_RobotsRule] = []
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.casefold()
            if field == "user-agent":
                if rules:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.casefold())
            elif field in {"allow", "disallow"} and agents:
                if not value and field == "disallow":
                    continue
                rules.append(_RobotsRule(field == "allow", value))
        if agents:
            groups.append((agents, rules))
        return cls(groups)

    def can_fetch(self, user_agent: str, url: str) -> bool:
        product_token = user_agent.split("/", 1)[0].casefold()
        selected: list[_RobotsRule] = []
        selected_length = -1
        for agents, rules in self.groups:
            matches = [
                len(agent)
                for agent in agents
                if agent == "*" or product_token.startswith(agent)
            ]
            if not matches:
                continue
            length = max(matches)
            if length > selected_length:
                selected = list(rules)
                selected_length = length
            elif length == selected_length:
                selected.extend(rules)

        path = urlparse(url).path or "/"
        query = urlparse(url).query
        if query:
            path = f"{path}?{query}"
        matched = [rule for rule in selected if rule.matches(path)]
        if not matched:
            return True
        winner = max(matched, key=lambda rule: (rule.precedence, rule.allow))
        return winner.allow


class IHerbSitemapClient:
    """Discover iHerb product URLs without using the disallowed search path."""

    index_url = "https://www.iherb.com/sitemap_index.xml"
    max_sitemap_bytes = 25_000_000

    def __init__(
        self,
        policy: CrawlPolicy | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.policy = policy or CrawlPolicy()
        self.transport = transport
        self._last_request_at = 0.0

    def iter_product_references(
        self,
        *,
        limit: int | None = None,
    ) -> Iterator[IHerbProductReference]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive or None")
        with httpx.Client(
            transport=self.transport,
            headers={"User-Agent": self.policy.user_agent},
            timeout=self.policy.timeout_seconds,
            follow_redirects=False,
        ) as client:
            index = self._get_xml(client, self.index_url)
            sitemap_urls = [
                location.text.strip()
                for location in index.findall(".//{*}sitemap/{*}loc")
                if location.text
                and "/sitemaps/products-" in location.text
            ]
            yielded = 0
            for sitemap_url in sitemap_urls:
                _validate_iherb_https(sitemap_url)
                sitemap = self._get_xml(client, sitemap_url)
                for entry in sitemap.findall(".//{*}url"):
                    location = entry.find("{*}loc")
                    if location is None or not location.text:
                        continue
                    url = location.text.strip()
                    parsed = urlparse(url)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname != "www.iherb.com"
                        or "/pr/" not in parsed.path
                    ):
                        continue
                    last_modified = entry.find("{*}lastmod")
                    yield IHerbProductReference(
                        url=url,
                        last_modified=_parse_sitemap_datetime(
                            last_modified.text if last_modified is not None else None
                        ),
                    )
                    yielded += 1
                    if limit is not None and yielded >= limit:
                        return

    def _get_xml(self, client: httpx.Client, url: str) -> ET.Element:
        current = url
        for _ in range(self.policy.max_redirects + 1):
            _validate_iherb_https(current)
            response = self._request(client, current)
            if response.is_redirect:
                current = _redirect_target(current, response)
                response.close()
                continue
            chunks: list[bytes] = []
            size = 0
            try:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_sitemap_bytes:
                        raise ValueError("iHerb sitemap exceeds 25 MB")
                    chunks.append(chunk)
            finally:
                response.close()
            payload = b"".join(chunks)
            break
        else:
            raise RuntimeError("Too many iHerb sitemap redirects")
        if (
            payload.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff"))
            or b"\x00" in payload
        ):
            raise ValueError("iHerb sitemap must use UTF-8 XML")
        try:
            xml = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("iHerb sitemap must use UTF-8 XML") from error
        uppercase = xml.upper()
        if "<!DOCTYPE" in uppercase or "<!ENTITY" in uppercase:
            raise ValueError("iHerb sitemap contains unsupported XML declarations")
        return ET.fromstring(xml)

    def _request(self, client: httpx.Client, url: str) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(self.policy.max_retries + 1):
            retry_delay = (
                min(2**attempt, 8) + random.uniform(0, 0.25)  # noqa: S311
            )
            self._wait()
            try:
                request = client.build_request("GET", url)
                response = client.send(request, stream=True)
            except httpx.HTTPError as error:
                last_error = error
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                last_error = httpx.HTTPStatusError(
                    f"retryable iHerb response: {response.status_code}",
                    request=response.request,
                    response=response,
                )
                retry_delay = _retry_delay(response, attempt)
                response.close()
            if attempt < self.policy.max_retries:
                time.sleep(retry_delay)
        raise RuntimeError(f"iHerb request failed: {url}") from last_error

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.policy.delay_seconds:
            time.sleep(self.policy.delay_seconds - elapsed)
        self._last_request_at = time.monotonic()


class IHerbClient:
    """Conservative single-page client with no anti-bot bypass behavior."""

    def __init__(
        self,
        policy: CrawlPolicy | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.policy = policy or CrawlPolicy()
        self.transport = transport
        self._last_request_at = 0.0
        self._robots_by_origin: dict[str, _RobotsRules] = {}

    def fetch_product(self, url: str) -> str:
        return self.fetch_product_page(url).html

    def fetch_product_page(self, url: str) -> FetchedProductPage:
        self._validate_url(url)
        with httpx.Client(
            transport=self.transport,
            headers={"User-Agent": self.policy.user_agent},
            timeout=self.policy.timeout_seconds,
            follow_redirects=False,
        ) as client:
            current = url
            for _ in range(self.policy.max_redirects + 1):
                self._validate_url(current)
                self._check_robots(current, client)
                response = self._request(client, current)
                if response.is_redirect:
                    current = _redirect_target(current, response)
                    continue
                if response.status_code in {401, 403}:
                    raise PermissionError(
                        "iHerb rejected automated product access. "
                        "Use an authorized affiliate feed, API, or saved HTML input."
                    )
                response.raise_for_status()
                return FetchedProductPage(url=current, html=response.text)
            raise RuntimeError("Too many iHerb product redirects")

    @staticmethod
    def _validate_url(url: str) -> None:
        _validate_iherb_https(url)
        parsed = urlparse(url)
        if "/pr/" not in parsed.path:
            raise ValueError("Expected an iHerb product URL containing /pr/")

    def _check_robots(self, url: str, client: httpx.Client) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots_by_origin.get(origin)
        if cached is not None:
            if not cached.can_fetch(self.policy.user_agent, url):
                raise PermissionError(f"robots.txt does not permit fetching {url}")
            return

        robots_url = f"{origin}/robots.txt"
        try:
            response = self._request_with_redirects(client, robots_url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise RuntimeError(f"Could not verify robots.txt: {error}") from error
        parser = _RobotsRules.parse(response.text)
        self._robots_by_origin[origin] = parser
        if not parser.can_fetch(self.policy.user_agent, url):
            raise PermissionError(f"robots.txt does not permit fetching {url}")

    def _request_with_redirects(
        self,
        client: httpx.Client,
        url: str,
    ) -> httpx.Response:
        current = url
        for _ in range(self.policy.max_redirects + 1):
            _validate_iherb_https(current)
            response = self._request(client, current)
            if response.is_redirect:
                current = _redirect_target(current, response)
                continue
            return response
        raise RuntimeError("Too many iHerb redirects")

    def _request(self, client: httpx.Client, url: str) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(self.policy.max_retries + 1):
            retry_delay = (
                min(2**attempt, 8) + random.uniform(0, 0.25)  # noqa: S311
            )
            self._wait()
            try:
                response = client.get(url)
            except httpx.HTTPError as error:
                last_error = error
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                last_error = httpx.HTTPStatusError(
                    f"retryable iHerb response: {response.status_code}",
                    request=response.request,
                    response=response,
                )
                retry_delay = _retry_delay(response, attempt)
            if attempt < self.policy.max_retries:
                time.sleep(retry_delay)
        raise RuntimeError(f"iHerb request failed: {url}") from last_error

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.policy.delay_seconds:
            time.sleep(self.policy.delay_seconds - elapsed)
        self._last_request_at = time.monotonic()


def _parse_sitemap_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _validate_iherb_https(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (
        host == "iherb.com" or host.endswith(".iherb.com")
    ):
        raise ValueError("Only HTTPS iHerb URLs are supported")


def _redirect_target(current_url: str, response: httpx.Response) -> str:
    location = response.headers.get("Location")
    if not location:
        raise RuntimeError("iHerb redirect has no Location header")
    target = urljoin(current_url, location)
    _validate_iherb_https(target)
    return target


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(2**attempt, 8) + random.uniform(0, 0.25)  # noqa: S311
