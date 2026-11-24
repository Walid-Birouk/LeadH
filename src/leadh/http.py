"""The one door to the network: retries with backoff, a circuit breaker on rate limits, per-domain pacing and
robots.txt. Every fetch in the pipeline goes through here."""

import random
import time
from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from leadh.config import CrawlerConfig


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CircuitOpen(FetchError):
    pass


class Blocked(FetchError):
    """robots.txt disallows the URL."""


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.ends = clock() + seconds

    def check(self) -> None:
        if self.clock() > self.ends:
            raise TimeoutError("lead time limit reached")


class Http:
    def __init__(self, cfg: CrawlerConfig, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.client = client or httpx.Client(timeout=cfg.timeout_s, follow_redirects=True)
        self.client.headers["User-Agent"] = cfg.user_agent
        self.sleep = sleep
        self.clock = clock
        self.last_hit: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser | None] = {}
        self.consecutive_429 = 0
        self.open_until = 0.0

    def get(self, url: str, *, params: dict[str, str] | None = None, headers: dict[str, str] | None = None,
            interval_s: float | None = None, respect_robots: bool = True, tries: int | None = None) -> httpx.Response:
        host = urlsplit(url).netloc.lower()
        if respect_robots and not self.allowed(url):
            raise Blocked(url)
        return self._get(url, host, params, headers, self.cfg.request_interval_s if interval_s is None else interval_s,
                         tries or self.cfg.max_tries)

    def _get(self, url: str, host: str, params: dict[str, str] | None, headers: dict[str, str] | None,
             interval_s: float, tries: int) -> httpx.Response:
        if self.clock() < self.open_until:
            raise CircuitOpen(host)
        for attempt in range(tries):
            self._pace(host, interval_s)
            try:
                resp = self.client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as e:
                if attempt == tries - 1:
                    raise FetchError(f"{type(e).__name__} on {host}") from e
                self._backoff(attempt)
                continue
            except httpx.TransportError as e:  # DNS failure, refused connection, bad TLS: not worth retrying
                raise FetchError(f"{type(e).__name__} on {host}") from e
            if resp.status_code == 429:
                self.consecutive_429 += 1
                if self.consecutive_429 >= self.cfg.breaker_threshold:
                    self.open_until = self.clock() + self.cfg.breaker_pause_s
                    self.consecutive_429 = 0
                    raise CircuitOpen(host)
                self._backoff(attempt)
                continue
            self.consecutive_429 = 0
            if resp.status_code >= 500:
                if attempt == tries - 1:
                    raise FetchError(f"HTTP {resp.status_code} on {host}", resp.status_code)
                self._backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} on {host}", resp.status_code)
            return resp
        raise FetchError(f"gave up on {host}")

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = self._get(f"{origin}/robots.txt", parts.netloc.lower(), None, None, self.cfg.request_interval_s,
                                 2)
                assert parser is not None
                parser.parse(resp.text.splitlines())
            except CircuitOpen:
                raise
            except FetchError:
                parser = None  # no readable robots.txt means no restrictions
            self.robots[origin] = parser
        robots = self.robots[origin]
        return robots is None or robots.can_fetch(self.cfg.user_agent, url)

    def _pace(self, host: str, interval_s: float) -> None:
        wait = self.last_hit.get(host, -1e9) + interval_s - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.last_hit[host] = self.clock()

    def _backoff(self, attempt: int) -> None:
        self.sleep(min(30.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.5))
