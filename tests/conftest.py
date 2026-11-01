from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from leadh.config import Config, load_config
from leadh.http import Http
from leadh.store import Store

ROOT = Path(__file__).parent.parent
SITES = Path(__file__).parent / "fixtures" / "sites"

SIREN_OK = "812345674"
SIREN_TOOL = "823456785"
SIREN_NO_SITE = "834567896"


def registry_row(siren: str, name: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "siren": siren, "nom_complet": name, "nom_raison_sociale": name, "activite_principale": "70.22Z",
        "categorie_entreprise": "PME", "tranche_effectif_salarie": "11", "date_creation": "2025-09-14",
        "etat_administratif": "A", "statut_diffusion": "O", "opposition_diffusion_commerciale": False,
        "siege": {"libelle_commune": "PARIS", "code_postal": "75011", "region": "11"},
    }
    row.update(over)
    return row


REGISTRY_ROWS = [
    registry_row(SIREN_OK, "EXEMPLE CONSEIL"),
    registry_row(SIREN_TOOL, "OUTILLE STUDIO", activite_principale="73.11Z", date_creation="2012-01-01"),
    registry_row(SIREN_NO_SITE, "SANS SITE SARL"),
    registry_row("845678907", "DIFFUSION PARTIELLE", statut_diffusion="P"),
    registry_row("856789018", "OPPOSITION SAS", opposition_diffusion_commerciale=True),
]

# Site fixtures: path on disk per URL served by the fake web.
PAGES = {
    "https://exemple-conseil.fr/": "exemple-conseil/home.html",
    "https://exemple-conseil.fr/mentions-legales": "exemple-conseil/mentions-legales.html",
    "https://exemple-conseil.fr/faq": "exemple-conseil/faq.html",
    "https://exemple-conseil.fr/contact": "exemple-conseil/contact.html",
    "https://exemple-conseil.fr/en/": "exemple-conseil/en.html",
    "https://exemple-conseil.fr/recrutement": "exemple-conseil/recrutement.html",
    "https://outille-studio.fr/": "outille-studio/home.html",
    "https://outille-studio.fr/legal": "outille-studio/legal.html",
}

SEARCH = {
    f'"{SIREN_OK}"': ["https://www.societe.com/societe/exemple-conseil-812345674.html",
                      "https://exemple-conseil.fr/mentions-legales"],
    f'"{SIREN_TOOL}"': [],
    '"OUTILLE STUDIO" PARIS': ["https://outille-studio.fr/"],
}


def fake_web(registry_rows: list[dict[str, Any]] | None = None,
             extra: dict[str, str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    rows = REGISTRY_ROWS if registry_rows is None else registry_rows

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.host == "recherche-entreprises.api.gouv.fr":
            q = request.url.params.get("q")
            found = [r for r in rows if r["siren"] == q] if q else rows
            return httpx.Response(200, json={"results": found, "total_pages": 1})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        base = url.split("?")[0]
        if extra and base in extra:
            return httpx.Response(200, text=extra[base], headers={"content-type": "text/html"})
        if base in PAGES:
            return httpx.Response(200, text=(SITES / PAGES[base]).read_text(), headers={"content-type": "text/html"})
        return httpx.Response(404)

    return handler


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = load_config(ROOT / "config" / "leadh.yaml")
    c.data_dir = tmp_path
    c.llm.base_url = "mock"
    return c


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


def make_http(cfg: Config, handler: Callable[[httpx.Request], httpx.Response]) -> Http:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    clock = [0.0]

    def sleep(s: float) -> None:
        clock[0] += s

    return Http(cfg.crawler, client=client, sleep=sleep, clock=lambda: clock[0])
