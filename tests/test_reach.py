import json
from datetime import date
from typing import Any

import httpx

from leadh.config import Config, ReachConfig
from leadh.http import Deadline
from leadh.models import Lead, Status
from leadh.pipeline import Pipeline
from leadh.reach import AgentReach, Item, ReachSearch, job_ads, markdown_to_html
from leadh.registry import Registry
from leadh.search import ChainedSearch, StaticSearch, make_search
from leadh.signals import Budget, SignalReader, mock_complete
from leadh.site import SiteMatcher
from leadh.store import Store

from .conftest import SEARCH, SIREN_OK, fake_web, make_http, registry_row

JOB_URL = "https://fr.linkedin.com/jobs/view/assistant-comptable-4012345678"


def ok(items: list[dict[str, Any]]) -> str:
    return json.dumps({"schema_version": 1, "ok": True, "items": items, "error": None})


def fail(code: str) -> str:
    return json.dumps({"schema_version": 1, "ok": False, "items": [], "error": {"code": code, "message": "x"}})


class FakeReach:
    """Answers per (channel, action) and records every command."""

    def __init__(self, answers: dict[tuple[str, str], str]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float) -> str:
        self.calls.append(args)
        return self.answers.get((args[1], args[2]), fail("NOT_FOUND"))


def reach(answers: dict[tuple[str, str], str], **over: Any) -> tuple[AgentReach, FakeReach]:
    fake = FakeReach(answers)
    cfg = ReachConfig(enabled=True, read_fallback=True, linkedin_jobs=True, **over)
    return AgentReach(cfg, runner=fake), fake


def test_markdown_becomes_html_with_links() -> None:
    page = markdown_to_html("Mentions", "# Mentions légales\n\nSIREN 812 345 674 [Contact](https://a.fr/contact)\n"
                                        "![logo](https://a.fr/logo.png) <script>")
    assert '<a href="https://a.fr/contact">Contact</a>' in page
    assert "logo.png" not in page and "<script>" not in page and "SIREN 812 345 674" in page


def test_search_returns_urls_and_pauses_a_channel_that_says_stop() -> None:
    r, fake = reach({("web", "search"): ok([{"title": "A", "url": "https://a.fr/"}, {"title": "no url"}])})
    assert r.search('"812345674"') == ["https://a.fr/"]
    assert fake.calls[0][:4] == ["agent-reach", "web", "search", '"812345674"']

    r, fake = reach({("linkedin", "jobs"): fail("RATE_LIMITED")})
    assert r.linkedin_jobs("X", None) == [] and r.linkedin_jobs("Y", None) == []
    assert len(fake.calls) == 1  # paused after the first answer


def test_missing_command_or_bad_output_gives_nothing() -> None:
    r = AgentReach(ReachConfig(enabled=True, command="agent-reach-not-installed"))
    assert r.search("q") == []
    r, _ = reach({("web", "search"): "not json"})
    assert r.search("q") == []
    r = AgentReach(ReachConfig(enabled=False), runner=FakeReach({("web", "search"): ok([{"url": "https://a.fr"}])}))
    assert r.search("q") == []


def test_agentreach_search_provider(cfg: Config) -> None:
    r, _ = reach({("web", "search"): ok([{"url": "https://a.fr/"}])})
    cfg.search.provider = "agentreach+guess"
    search = make_search(cfg.search, make_http(cfg, fake_web()), r)
    assert isinstance(search, ChainedSearch)
    urls = search.search('"EXEMPLE CONSEIL" PARIS')
    assert urls[0] == "https://a.fr/" and "https://exemple-conseil.fr/" in urls


def lead(siren: str = SIREN_OK, name: str = "EXEMPLE CONSEIL") -> Lead:
    return Lead(lead_id="L-1", siren=siren, name=name, city="PARIS", postal_code="75011")


def test_refused_site_is_read_through_agent_reach(cfg: Config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bloque.fr" and request.url.path != "/robots.txt":
            return httpx.Response(403)
        return fake_web()(request)

    legal = "Mentions légales\nEXEMPLE CONSEIL SAS, SIREN 812 345 674, 75011 Paris."
    r, fake = reach({("web", "read"): ok([{"title": "Bloqué", "url": "https://bloque.fr/", "text": legal}])})
    http = make_http(cfg, handler)
    matcher = SiteMatcher(http, StaticSearch({f'"{SIREN_OK}"': ["https://bloque.fr/"]}), 3, 5, r)
    m = matcher.match(lead(), Deadline(60))
    assert m is not None and m.proof == "siren" and m.pages[0].via == "reach"
    assert fake.calls[0][1:4] == ["web", "read", "https://bloque.fr/"]

    # Without the fallback the same site is simply not found.
    r.cfg.read_fallback = False
    assert SiteMatcher(http, StaticSearch({f'"{SIREN_OK}"': ["https://bloque.fr/"]}), 3, 5, r) \
        .match(lead(), Deadline(60)) is None


def test_robots_disallow_is_never_read_through_agent_reach(cfg: Config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(403)

    r, fake = reach({("web", "read"): ok([{"title": "x", "url": "https://prive.fr/", "text": "SIREN 812345674"}])})
    matcher = SiteMatcher(make_http(cfg, handler), StaticSearch({f'"{SIREN_OK}"': ["https://prive.fr/"]}), 3, 5, r)
    assert matcher.match(lead(), Deadline(60)) is None
    assert fake.calls == []


def test_job_ads_keep_only_the_company_s_admin_roles() -> None:
    items = [
        Item("Assistant comptable H/F", JOB_URL, "Exemple Conseil", "CDI à Paris", {}),
        Item("Développeur Python", "https://fr.linkedin.com/jobs/view/2", "Exemple Conseil", "", {}),
        Item("Comptable", "https://fr.linkedin.com/jobs/view/3", "Autre Société", "", {}),
        Item("Gestionnaire facturation", "https://fr.linkedin.com/jobs/view/4", "",
             "", {"company": {"name": "EXEMPLE CONSEIL SAS"}}),
    ]
    assert [i.url for i in job_ads(items, "EXEMPLE CONSEIL")] == [JOB_URL, "https://fr.linkedin.com/jobs/view/4"]


def test_pipeline_reads_a_linkedin_job_ad_as_evidence(cfg: Config, store: Store) -> None:
    ad = {"title": "Assistant(e) comptable H/F", "url": JOB_URL, "author": "Exemple Conseil",
          "text": "Nous recrutons un(e) assistant(e) comptable en CDI pour gérer la facturation clients."}
    r, fake = reach({("linkedin", "jobs"): ok([ad])})
    cfg.registry.naf_codes = ["70.22Z"]
    http = make_http(cfg, fake_web([registry_row(SIREN_OK, "EXEMPLE CONSEIL")]))
    p = Pipeline(cfg=cfg, store=store, registry=Registry(cfg, http),
                 matcher=SiteMatcher(http, StaticSearch(SEARCH), 3, cfg.limits.pages_per_site, r),
                 reader=SignalReader(mock_complete, Budget(1_000_000), 6000), run_id="r1",
                 mx=lambda d: True, today=lambda: date(2026, 11, 2))
    p.run(["scout", "match", "read", "check"])
    got = next(iter(store.all()))
    assert got.status == Status.CHECKED
    job_pages = [ref for ref in got.pages if ref.via == "job_ad"]
    assert [ref.url for ref in job_pages] == [JOB_URL]
    assert any(s.type == "admin_job_ad" and s.url == JOB_URL and s.checked for s in got.signals)
    assert got.contact and "linkedin" not in got.contact.source_url
    assert fake.calls[0][1:4] == ["linkedin", "jobs", "EXEMPLE CONSEIL"]


def test_company_comes_from_the_url_slug_or_the_title() -> None:
    from leadh.reach import company_of

    def item(title: str, url: str, text: str = "") -> Item:
        return Item(title, url, "", text, {"source": {"author": "N/A"}})

    assert company_of(item("Comptable H/F", "https://fr.linkedin.com/jobs/view/comptable-at-doctolib-4413970983")) \
        == "doctolib"
    assert company_of(item("Doctolib recrute pour des postes de comptable", "https://x/jobs/view/1")) == "Doctolib"
    assert company_of(item("Accountant at Capgemini — Singapore", "https://x/jobs/view/2")) == "Capgemini"
    assert company_of(item("Comptable", "https://x/jobs/view/3", "# Comptable\n\n Exemple Conseil Paris")) \
        == "Exemple Conseil Paris"


def test_job_ads_need_the_exact_company_and_france() -> None:
    def item(title: str, slug: str, text: str = "") -> Item:
        return Item(title, f"https://fr.linkedin.com/jobs/view/{slug}", "", text, {})

    items = [
        item("Assistant comptable H/F", "assistant-comptable-at-exemple-conseil-11"),
        item("Comptable", "comptable-at-exemple-conseil-invent-12"),  # another division
        item("Accountant in London", "accountant-at-exemple-conseil-13"),  # abroad
        item("Développeur", "developpeur-at-exemple-conseil-14"),  # not an admin role
    ]
    assert [i.url.rsplit("-", 1)[1] for i in job_ads(items, "EXEMPLE CONSEIL", "PARIS")] == ["11"]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def limited(after: float) -> str:
    return json.dumps({"ok": False, "items": [], "error": {"code": "RATE_LIMITED", "message": "x"},
                       "meta": {"attempts": [{"backend": "exa", "retry_after_seconds": after}]}})


def test_rate_limit_rests_the_channel_then_tries_again() -> None:
    answers = iter([limited(60), ok([{"url": "https://a.fr/"}])])
    clock = Clock()
    r = AgentReach(ReachConfig(enabled=True, min_interval_s=0), runner=lambda a, t: next(answers),
                   sleep=clock.sleep, clock=clock)
    assert r.search("q1") == [] and r.misses == 1
    assert r.search("q2") == [] and r.misses == 2  # still resting: skipped, not run
    clock.t += 61
    assert r.search("q3") == ["https://a.fr/"] and r.misses == 2
    assert "web" not in r.stopped


def test_repeated_rate_limits_turn_the_channel_off_and_login_stops_it_at_once() -> None:
    clock = Clock()
    r = AgentReach(ReachConfig(enabled=True, min_interval_s=0, max_strikes=2), runner=lambda a, t: limited(1),
                   sleep=clock.sleep, clock=clock)
    r.search("q1")
    clock.t += 2
    r.search("q2")
    assert "web" in r.stopped

    r, fake = reach({("linkedin", "jobs"): fail("NEED_LOGIN")})
    r.linkedin_jobs("X", None)
    r.linkedin_jobs("Y", None)
    assert "linkedin" in r.stopped and len(fake.calls) == 1


def test_lead_whose_search_was_rate_limited_stays_found(cfg: Config, store: Store) -> None:
    clock = Clock()
    r = AgentReach(ReachConfig(enabled=True, min_interval_s=0), runner=lambda a, t: limited(600),
                   sleep=clock.sleep, clock=clock)
    cfg.registry.naf_codes = ["70.22Z"]
    http = make_http(cfg, fake_web([registry_row("834567896", "SANS SITE SARL")]))
    p = Pipeline(cfg=cfg, store=store, registry=Registry(cfg, http),
                 matcher=SiteMatcher(http, ChainedSearch([ReachSearch(r), StaticSearch({})]), 3, 5, r),
                 reader=SignalReader(mock_complete, Budget(1_000_000), 6000), run_id="r1",
                 mx=lambda d: True, today=lambda: date(2026, 11, 2))
    p.run(["scout", "match"])
    assert next(iter(store.all())).status == Status.FOUND


def test_a_directory_site_matched_under_old_rules_is_rejected(cfg: Config, store: Store) -> None:
    from leadh.models import Contact, Scores, Site
    from leadh.validator import validate

    stale = lead()
    stale.category, stale.active = "PME", True
    stale.site = Site(url="https://apify.com/", siren_found_at="https://apify.com/someone/france-sirene-api")
    stale.contact = Contact(email="hello@apify.com", kind="generic", source_url="https://apify.com/contact")
    stale.scores = Scores(need=90, fit=100)
    v = validate(stale, cfg, store, lambda d: True)
    assert v.status == "REJECTED" and "directory_site" in v.reasons
