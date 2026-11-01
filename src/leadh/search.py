import os
import re
import unicodedata
from typing import Protocol

from leadh.config import SearchConfig
from leadh.http import Http

# Directories, registries and social networks list the company but are not its own site.
NOT_OWN_SITE = frozenset({
    "societe.com", "pappers.fr", "infogreffe.fr", "verif.com", "manageo.fr", "annuaire-entreprises.data.gouv.fr",
    "entreprises.lefigaro.fr", "kompass.com", "pagesjaunes.fr", "linkedin.com", "facebook.com", "instagram.com",
    "x.com", "twitter.com", "youtube.com", "indeed.com", "welcometothejungle.com", "glassdoor.fr", "google.com",
    "wikipedia.org", "bodacc.fr", "inpi.fr", "sirene.fr", "dnb.com", "corporama.com", "lagazettefrance.fr",
    "score3.fr", "infonet.fr", "societe.ninja", "entreprise.data.gouv.fr", "impots.gouv.fr",
})


def is_directory(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in NOT_OWN_SITE)


class WebSearch(Protocol):
    def search(self, query: str) -> list[str]: ...


class NoSearch:
    def search(self, query: str) -> list[str]:
        return []


class StaticSearch:
    """Answers from a fixed map, for tests and labelled replays."""

    def __init__(self, results: dict[str, list[str]]) -> None:
        self.results = results

    def search(self, query: str) -> list[str]:
        return self.results.get(query, [])


LEGAL_FORMS = re.compile(r"\b(sas|sasu|sarl|eurl|sa|sci|snc|selarl|scop|groupe|france)\b")


class GuessDomains:
    """No search API: tries the company name as a .fr or .com domain. Safe only because the SIREN check decides;
    a guessed domain that does not show the SIREN is never matched."""

    def search(self, query: str) -> list[str]:
        m = re.match(r'"([^"]+)"', query)
        if not m or m.group(1).isdigit():
            return []
        name = unicodedata.normalize("NFKD", m.group(1)).encode("ascii", "ignore").decode().lower()
        urls = [f"https://{d}/" for d in re.findall(r"\b[a-z0-9-]+\.(?:fr|com|eu|io|net|org|co|paris)\b", name)]
        # "LEGAL NAME (TRADE NAME)": the trade name is the likelier domain.
        names = [*re.findall(r"\(([^)]*)\)", name), re.sub(r"\(.*?\)", " ", name)]
        for part in names:
            words = re.findall(r"[a-z0-9]+", LEGAL_FORMS.sub(" ", part.replace("&", " et ")))
            if words:
                urls += [f"https://{slug}.{tld}/" for slug in dict.fromkeys(["".join(words), "-".join(words)])
                         for tld in ("fr", "com")]
        return list(dict.fromkeys(urls))


class BraveSearch:
    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, http: Http, api_key: str) -> None:
        self.http = http
        self.api_key = api_key

    def search(self, query: str) -> list[str]:
        resp = self.http.get(self.URL, params={"q": query, "country": "FR", "count": "10"},
                             headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                             respect_robots=False)
        return [r["url"] for r in resp.json().get("web", {}).get("results", []) if r.get("url")]


def make_search(cfg: SearchConfig, http: Http) -> WebSearch:
    if cfg.provider == "brave":
        key = os.environ.get(cfg.api_key_env, "")
        if not key:
            raise SystemExit(f"search provider brave needs {cfg.api_key_env}")
        return BraveSearch(http, key)
    if cfg.provider == "guess":
        return GuessDomains()
    if cfg.provider == "none":
        return NoSearch()
    raise SystemExit(f"unknown search provider {cfg.provider!r}")
