"""
robots.txt compliance.

Parses each host's robots.txt once and exposes can_fetch() so the crawler
can ask "am I allowed to request this URL?" before every HTTP call.

Uses the standard library's urllib.robotparser, which implements the
Robots Exclusion Standard (RFC 9309 informal).
"""
from __future__ import annotations
from urllib.parse import urlparse
from urllib import robotparser
import urllib.request
import time

USER_AGENT = "IR-Project/1.0 (+meal-planner academic project; contact: student@example.edu)"


class RobotsGuard:
    """One instance per crawl session. Caches one RobotFileParser per host."""

    def __init__(self, user_agent: str = USER_AGENT) -> None:
        self.user_agent = user_agent
        self._parsers: dict[str, robotparser.RobotFileParser] = {}
        # Per-host crawl-delay overrides (None = use default)
        self._crawl_delays: dict[str, float | None] = {}

    def _get_parser(self, host: str) -> robotparser.RobotFileParser:
        if host in self._parsers:
            return self._parsers[host]

        rp = robotparser.RobotFileParser()
        robots_url = f"https://{host}/robots.txt"
        try:
            req = urllib.request.Request(
                robots_url, headers={"User-Agent": self.user_agent}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            rp.parse(content.splitlines())
            print(f"[robots] loaded {robots_url}")
        except Exception as e:
            # Conservative default: if we can't read robots.txt, assume
            # everything is disallowed for this host. Better safe than sorry.
            print(f"[robots] could not load {robots_url}: {e} — denying all")
            rp.parse(["User-agent: *", "Disallow: /"])

        self._parsers[host] = rp
        # Record the site's requested crawl-delay (if any)
        delay = rp.crawl_delay(self.user_agent) or rp.crawl_delay("*")
        self._crawl_delays[host] = float(delay) if delay else None
        return rp

    def can_fetch(self, url: str) -> bool:
        host = urlparse(url).netloc
        parser = self._get_parser(host)
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        host = urlparse(url).netloc
        # ensure parser loaded
        self._get_parser(host)
        return self._crawl_delays.get(host)


if __name__ == "__main__":
    # Smoke test
    g = RobotsGuard()
    test_urls = [
        "https://www.allrecipes.com/recipes/",
        "https://www.allrecipes.com/account/",
        "https://www.bbc.co.uk/food/recipes/",
    ]
    for u in test_urls:
        print(f"  {u}: {'ALLOWED' if g.can_fetch(u) else 'BLOCKED'}")
