import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


@dataclass(slots=True)
class CrawlPolicy:
    user_agent: str = "Suparch/0.1 (+https://github.com/suparch)"
    timeout_seconds: float = 30
    delay_seconds: float = 2


class IHerbClient:
    """Conservative single-page client with no anti-bot bypass behavior."""

    def __init__(self, policy: CrawlPolicy | None = None) -> None:
        self.policy = policy or CrawlPolicy()
        self._last_request_at = 0.0

    def fetch_product(self, url: str) -> str:
        self._validate_url(url)
        self._check_robots(url)
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.policy.delay_seconds:
            time.sleep(self.policy.delay_seconds - elapsed)
        with httpx.Client(
            headers={"User-Agent": self.policy.user_agent},
            timeout=self.policy.timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = client.get(url)
            self._last_request_at = time.monotonic()
            response.raise_for_status()
            return response.text

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (
            host == "iherb.com" or host.endswith(".iherb.com")
        ):
            raise ValueError("Only HTTPS iHerb URLs are supported")
        if "/pr/" not in parsed.path:
            raise ValueError("Expected an iHerb product URL containing /pr/")

    def _check_robots(self, url: str) -> None:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.read()
        except OSError as error:
            raise RuntimeError(f"Could not verify robots.txt: {error}") from error
        if not parser.can_fetch(self.policy.user_agent, url):
            raise PermissionError(f"robots.txt does not permit fetching {url}")
