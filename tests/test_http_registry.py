import httpx
import pytest

from leadh.config import Config
from leadh.http import Blocked, CircuitOpen, FetchError
from leadh.registry import Registry, to_lead

from .conftest import SIREN_OK, fake_web, make_http, registry_row


def counting(statuses: list[int]) -> tuple[list[str], object]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(str(request.url))
        return httpx.Response(statuses[min(len(seen), len(statuses)) - 1], text="ok")

    return seen, handler


def test_retries_5xx_then_succeeds(cfg: Config) -> None:
    seen, handler = counting([503, 502, 200])
    assert make_http(cfg, handler).get("https://a.fr/").text == "ok"  # type: ignore[arg-type]
    assert len(seen) == 3


def test_gives_up_after_max_tries_and_never_retries_4xx(cfg: Config) -> None:
    seen, handler = counting([500])
    with pytest.raises(FetchError):
        make_http(cfg, handler).get("https://a.fr/")  # type: ignore[arg-type]
    assert len(seen) == cfg.crawler.max_tries
    seen, handler = counting([404])
    with pytest.raises(FetchError):
        make_http(cfg, handler).get("https://a.fr/")  # type: ignore[arg-type]
    assert len(seen) == 1


def test_breaker_opens_on_repeated_429(cfg: Config) -> None:
    seen, handler = counting([429])
    http = make_http(cfg, handler)  # type: ignore[arg-type]
    with pytest.raises(CircuitOpen):
        http.get("https://a.fr/")
    assert len(seen) == cfg.crawler.breaker_threshold
    with pytest.raises(CircuitOpen):
        http.get("https://a.fr/other")
    assert len(seen) == cfg.crawler.breaker_threshold


def test_robots_and_pacing(cfg: Config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="ok")

    http = make_http(cfg, handler)
    with pytest.raises(Blocked):
        http.get("https://a.fr/private/x")
    start = http.clock()
    http.get("https://a.fr/1")
    http.get("https://a.fr/2")
    assert http.clock() - start >= cfg.crawler.request_interval_s


def test_scout_drops_partial_diffusion_and_opposition(cfg: Config) -> None:
    sirens = [r["siren"] for r in Registry(cfg, make_http(cfg, fake_web())).candidates()]
    assert "845678907" not in sirens and "856789018" not in sirens
    assert sirens.count(SIREN_OK) == len(cfg.registry.naf_codes)  # the fake answers every NAF query


def test_scout_paginates(cfg: Config) -> None:
    cfg.registry.naf_codes = ["70.22Z"]
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        pages.append(page)
        return httpx.Response(200, json={"results": [registry_row(f"81234567{page}", "X")], "total_pages": 3})

    assert len(list(Registry(cfg, make_http(cfg, handler)).candidates())) == 3 and pages == ["1", "2", "3"]


def test_to_lead_maps_registry_fields() -> None:
    lead = to_lead(registry_row(SIREN_OK, "EXEMPLE CONSEIL", etat_administratif="C"), "L-000007", "r1")
    assert (lead.lead_id, lead.naf, lead.size_band, lead.city, lead.region, lead.active) == \
        ("L-000007", "70.22Z", "11", "PARIS", "11", False)


def test_dotenv_fills_only_unset_variables(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from leadh.config import load_dotenv

    (tmp_path / ".env").write_text("# comment\nLEADH_T1='from file'\nLEADH_T2=from file\nEMPTY_LINE\n")
    monkeypatch.setenv("LEADH_T2", "from env")
    monkeypatch.delenv("LEADH_T1", raising=False)
    load_dotenv(tmp_path / ".env")
    import os

    assert os.environ["LEADH_T1"] == "from file" and os.environ["LEADH_T2"] == "from env"
    monkeypatch.delenv("LEADH_T1")


def test_scout_sends_band_and_sole_trader_filters(cfg: Config) -> None:
    cfg.registry.naf_codes = ["70.22Z"]
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"results": [], "total_pages": 1})

    list(Registry(cfg, make_http(cfg, handler)).candidates())
    assert seen[0].params["tranche_effectif_salarie"] == "01,02,03,11,12"
    assert seen[0].params["est_entrepreneur_individuel"] == "false"
