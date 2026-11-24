import os
import re
import unicodedata
from typing import Any, Protocol

from leadh.config import SearchConfig
from leadh.http import Http
from leadh.reach import AgentReach, ReachSearch

# Directories, registries and social networks list the company but are not its own site.
NOT_OWN_SITE = frozenset({
    "societe.com", "pappers.fr", "infogreffe.fr", "verif.com", "manageo.fr", "annuaire-entreprises.data.gouv.fr",
    "entreprises.lefigaro.fr", "kompass.com", "pagesjaunes.fr", "linkedin.com", "facebook.com", "instagram.com",
    "x.com", "twitter.com", "youtube.com", "indeed.com", "welcometothejungle.com", "glassdoor.fr", "google.com",
    "wikipedia.org", "bodacc.fr", "inpi.fr", "sirene.fr", "dnb.com", "corporama.com", "lagazettefrance.fr",
    "score3.fr", "infonet.fr", "societe.ninja", "entreprise.data.gouv.fr", "impots.gouv.fr", "rubypayeur.com",
    "dataprospects.fr", "annuairefrancais.fr", "eterritoire.fr", "apify.com", "northdata.fr", "northdata.com",
    "lefigaro.fr", "pappers.com", "firmania.fr", "118000.fr", "annuaire-mairie.fr",
    "b-reputation.com", "doctrine.fr", "ellisphere.com", "creditsafe.com", "data.gouv.fr",
    "opencorporates.com", "bilansgratuits.fr", "societeinfo.com", "lagazettedescommunes.com",
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
    # The biggest directories crowd the first page of a SIREN query; excluding them leaves room for the real site.
    EXCLUDED = ["societe.com", "pappers.fr", "infogreffe.fr", "annuaire-entreprises.data.gouv.fr", "verif.com",
                "linkedin.com", "manageo.fr"]

    def __init__(self, http: Http, api_key: str, interval_s: float = 1.0) -> None:
        self.http = http
        self.api_key = api_key
        self.interval_s = interval_s

    def search(self, query: str) -> list[str]:
        q = query + "".join(f" -site:{d}" for d in self.EXCLUDED)
        # No spellcheck: it "corrects" company names into common words.
        params = {"q": q, "country": "FR", "search_lang": "fr", "count": "10", "result_filter": "web",
                  "spellcheck": "false", "extra_snippets": "true", "text_decorations": "false"}
        resp = self.http.get(self.URL, params=params, interval_s=self.interval_s, respect_robots=False,
                             headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"})
        results: list[dict[str, Any]] = [r for r in resp.json().get("web", {}).get("results", []) if r.get("url")]
        siren = re.fullmatch(r'"(\d{9})"', query)
        if siren:
            # A snippet that already shows the SIREN usually is the legal notice: try that site first.
            results.sort(key=lambda r: siren.group(1) not in re.sub(r"[\s. ]", "", snippet_text(r)))
        return [str(r["url"]) for r in results]


def snippet_text(result: dict[str, Any]) -> str:
    return " ".join(str(x) for x in [result.get("title"), result.get("description"),
                                     *(result.get("extra_snippets") or [])] if x)


class ChainedSearch:
    """Results of each search in turn, so guessing only adds candidates the search API did not find."""

    def __init__(self, searches: list[WebSearch]) -> None:
        self.searches = searches

    def search(self, query: str) -> list[str]:
        return list(dict.fromkeys(url for s in self.searches for url in s.search(query)))


def make_search(cfg: SearchConfig, http: Http, reach: "AgentReach | None" = None) -> WebSearch:
    """`provider` names one search or several joined by "+", e.g. "brave+agentreach+guess"."""
    searches: list[WebSearch] = []
    for name in cfg.provider.split("+"):
        if name == "brave":
            key = os.environ.get(cfg.api_key_env) or os.environ.get("BRAVE_API_KEY", "")
            if not key:
                raise SystemExit(f"search provider brave needs {cfg.api_key_env}")
            searches.append(BraveSearch(http, key, cfg.interval_s))
        elif name == "agentreach":
            if reach is None:
                raise SystemExit("search provider agentreach needs the reach section enabled")
            searches.append(ReachSearch(reach))
        elif name == "guess":
            searches.append(GuessDomains())
        elif name == "none":
            searches.append(NoSearch())
        else:
            raise SystemExit(f"unknown search provider {name!r}")
    return searches[0] if len(searches) == 1 else ChainedSearch(searches)
