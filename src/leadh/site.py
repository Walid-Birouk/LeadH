"""Site matcher: finds the company's own website and proves it by finding the SIREN on it, then picks the pages
the later steps read. Fetches stay on the candidate's own domain."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from leadh.http import CircuitOpen, Deadline, FetchError, Http
from leadh.models import Lead
from leadh.pages import domain_of, full_text, links, same_site, siren_in
from leadh.search import WebSearch, is_directory

LEGAL = re.compile(r"mentions|legal|l[ée]gales|imprint|impressum|cgv|cgu|conditions|cgs|privacy|confidentialit", re.I)
# Pages worth reading after the match, best first.
PRIORITY = [
    re.compile(p, re.I) for p in (
        r"factur|invoice|billing|paiement|payment|r[eè]glement",
        r"faq|questions",
        r"contact",
        r"tarif|pricing|prix|devis|quote",
        r"recrut|carri[eè]re|careers|jobs|emploi|join",
        r"services|offre|solutions|prestations|what-we-do",
        r"a-propos|about|qui-sommes|equipe|team|agence",
        r"^/en/?$|/en/|english",
    )
]


# A SIREN, SIRET or RCS number written next to its label. A page listing several is a directory, not a company.
REGISTRATION = re.compile(r"(?:siren|siret|rcs|r\.c\.s\.?)\D{0,25}(\d{3}[\s.\u00a0]?\d{3}[\s.\u00a0]?\d{3})", re.I)
MAX_OWN_NUMBERS = 3


def looks_like_directory(text: str) -> bool:
    return len({re.sub(r"\D", "", n) for n in REGISTRATION.findall(text)}) > MAX_OWN_NUMBERS


@dataclass
class Fetched:
    url: str
    html: str


def proves(page: Fetched, siren: str) -> bool:
    text = full_text(page.html)
    return siren_in(text, siren) and not looks_like_directory(text) and siren not in re.sub(r"\D", "", page.url)


@dataclass
class Match:
    site_url: str
    siren_found_at: str
    pages: list[Fetched] = field(default_factory=list)


class SiteMatcher:
    def __init__(self, http: Http, search: WebSearch, max_candidates: int, pages_per_site: int) -> None:
        self.http = http
        self.search = search
        self.max_candidates = max_candidates
        self.pages_per_site = pages_per_site

    def candidates(self, lead: Lead) -> list[tuple[str, list[str]]]:
        """(domain, result urls on that domain) from a SIREN search, then a name and city search."""
        queries = [f'"{lead.siren}"', f'"{lead.name}" {lead.city or ""}'.strip()]
        found: dict[str, list[str]] = {}
        for q in queries:
            for url in self.search.search(q):
                domain = domain_of(url)
                if not domain or is_directory(domain):
                    continue
                found.setdefault(domain, []).append(url)
        return list(found.items())[: self.max_candidates]

    def fetch(self, url: str, domain: str, probe: bool = False) -> Fetched | None:
        """A page on `domain`. A probe (a candidate's home page) gets one try, since most guesses do not exist, and
        may redirect to another domain: the company's site is wherever it lands."""
        if not same_site(url, domain):
            return None
        try:
            resp = self.http.get(url, tries=1 if probe else None)
        except CircuitOpen:
            raise
        except FetchError:
            return None
        if "html" not in resp.headers.get("content-type", "html"):
            return None
        if not probe and not same_site(str(resp.url), domain):
            return None
        return Fetched(str(resp.url), resp.text)

    def match(self, lead: Lead, deadline: Deadline) -> Match | None:
        for domain, hits in self.candidates(lead):
            deadline.check()
            m = self._try(lead, domain, hits, deadline)
            if m:
                return m
        return None

    def _try(self, lead: Lead, domain: str, hits: list[str], deadline: Deadline) -> Match | None:
        fetched: list[Fetched] = []
        home = next((h for scheme in ("https", "http")
                     if (h := self.fetch(f"{scheme}://{domain}/", domain, probe=True))), None)
        if home is None or is_directory(domain_of(home.url)):
            return None
        domain = domain_of(home.url)
        fetched.append(home)
        if looks_like_directory(full_text(home.html)):
            return None
        # Only the home page or a legal notice can prove ownership: a directory's per-company page (one URL per
        # SIREN) shows the number too. Search hits first, since the SIREN query often lands on the legal notice.
        to_check = [u for u in hits if u.rstrip("/") != home.url.rstrip("/") and LEGAL.search(urlsplit(u).path)]
        to_check += [u for u, text in links(home.html, home.url) if same_site(u, domain)
                     and (LEGAL.search(urlsplit(u).path) or LEGAL.search(text))]
        to_check = [u for u in to_check if lead.siren not in re.sub(r"\D", "", u)]
        proof = home if proves(home, lead.siren) else None
        seen = {home.url}
        for url in to_check:
            if proof or len(fetched) >= 3:
                break
            if url in seen:
                continue
            seen.add(url)
            deadline.check()
            page = self.fetch(url, domain)
            if page is None:
                continue
            fetched.append(page)
            if proves(page, lead.siren):
                proof = page
        if proof is None:
            return None
        match = Match(site_url=home.url, siren_found_at=proof.url, pages=fetched)
        self._fill(match, domain, deadline)
        return match

    def _fill(self, match: Match, domain: str, deadline: Deadline) -> None:
        have = {p.url for p in match.pages}
        ranked: list[tuple[int, str]] = []
        for page in list(match.pages):
            for url, text in links(page.html, page.url):
                if url in have or not same_site(url, domain):
                    continue
                path = url.split(domain, 1)[-1]
                for rank, pattern in enumerate(PRIORITY):
                    if pattern.search(path) or pattern.search(text):
                        ranked.append((rank, url))
                        break
        for _, url in sorted(set(ranked)):
            if len(match.pages) >= self.pages_per_site:
                break
            if url in have:
                continue
            have.add(url)
            deadline.check()
            extra = self.fetch(url, domain)
            if extra and extra.url not in {p.url for p in match.pages}:
                match.pages.append(extra)
