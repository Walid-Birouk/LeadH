import json
from datetime import date, timedelta

from leadh.config import Config
from leadh.models import Status, now
from leadh.pipeline import Pipeline
from leadh.registry import Registry
from leadh.report import daily_report, review_list
from leadh.search import StaticSearch
from leadh.signals import Budget, SignalReader, mock_complete
from leadh.site import SiteMatcher
from leadh.store import Store

from .conftest import REGISTRY_ROWS, SEARCH, SIREN_NO_SITE, SIREN_OK, SIREN_TOOL, fake_web, make_http


def pipeline(cfg: Config, store: Store, run_id: str = "2026-11-02", rows: list[dict[str, object]] | None = None,
             budget: int = 1_000_000) -> Pipeline:
    cfg.registry.naf_codes = ["70.22Z"]
    http = make_http(cfg, fake_web(rows))  # type: ignore[arg-type]
    return Pipeline(cfg=cfg, store=store, registry=Registry(cfg, http),
                    matcher=SiteMatcher(http, StaticSearch(SEARCH), 3, cfg.limits.pages_per_site),
                    reader=SignalReader(mock_complete, Budget(budget), 6000), run_id=run_id,
                    mx=lambda d: True, today=lambda: date(2026, 11, 2))


def by_siren(store: Store) -> dict[str, object]:
    return {lead.siren: lead for lead in store.all()}


def test_end_to_end(cfg: Config, store: Store) -> None:
    p = pipeline(cfg, store)
    p.run()
    leads = by_siren(store)
    assert set(leads) == {SIREN_OK, SIREN_TOOL, SIREN_NO_SITE}  # P and opposition never stored

    ok = leads[SIREN_OK]
    assert ok.status == Status.VALIDATED and ok.verdict.status == "APPROVED"  # type: ignore[attr-defined]
    assert ok.site.siren_found_at == "https://exemple-conseil.fr/mentions-legales"  # type: ignore[attr-defined]
    assert ok.contact.email == "contact@exemple-conseil.fr"  # type: ignore[attr-defined]
    assert ok.english_at and len(ok.pages) <= cfg.limits.pages_per_site  # type: ignore[attr-defined]
    assert {s.type for s in ok.signals} >= {"manual_invoicing", "sells_to_businesses"}  # type: ignore[attr-defined]
    assert all(s.checked for s in ok.signals)  # type: ignore[attr-defined]

    tool = leads[SIREN_TOOL]
    assert tool.status == Status.REJECTED and tool.name == "" and tool.site is None  # type: ignore[attr-defined]
    assert "b2c_only" in tool.verdict.reasons  # type: ignore[attr-defined]

    assert leads[SIREN_NO_SITE].status == Status.NO_SITE  # type: ignore[attr-defined]

    report = daily_report(store, cfg, p.run_id)
    assert "would hand off: 1" in report and "EXEMPLE" not in report and "@" not in report
    assert not (cfg.data_dir / "handoff").exists()  # shadow mode


def test_second_run_repeats_nothing(cfg: Config, store: Store) -> None:
    pipeline(cfg, store).run()
    before = {lead.lead_id: lead.status for lead in store.all()}
    p2 = pipeline(cfg, store, run_id="2026-11-02b")
    p2.run(["scout", "match", "read", "check", "score", "validate"])
    assert {lead.lead_id: lead.status for lead in store.all()} == before
    assert [e["kind"] for e in store.events(p2.run_id) if e["kind"] == "out"] == []


def test_handoff_writes_ranked_records(cfg: Config, store: Store) -> None:
    cfg.shadow_mode = False
    pipeline(cfg, store).run()
    lines = (cfg.data_dir / "handoff" / "2026-11-02.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert len(lines) == 1 and record["siren"] == SIREN_OK
    assert record["contact"]["source_url"] == "https://exemple-conseil.fr/contact"
    assert all(s["quote"] for s in record["signals"])
    assert by_siren(store)[SIREN_OK].status == Status.HANDED_OFF  # type: ignore[attr-defined]


def test_revalidation_catches_a_company_that_closed(cfg: Config, store: Store) -> None:
    pipeline(cfg, store).run(["scout", "match", "read", "check", "score", "validate"])
    closed = [r if r["siren"] != SIREN_OK else {**r, "etat_administratif": "C"} for r in REGISTRY_ROWS]
    pipeline(cfg, store, rows=closed).run(["handoff"])
    lead = by_siren(store)[SIREN_OK]
    assert lead.status == Status.REJECTED and lead.verdict.reasons == ["inactive"]  # type: ignore[attr-defined]


def test_stale_pages_are_fetched_again(cfg: Config, store: Store) -> None:
    pipeline(cfg, store).run(["scout", "match", "read", "check", "score", "validate"])
    lead = by_siren(store)[SIREN_OK]
    for ref in lead.pages:  # type: ignore[attr-defined]
        ref.fetched_at = now() - timedelta(days=31)
    store.save(lead)  # type: ignore[arg-type]
    pipeline(cfg, store).run(["handoff"])
    assert by_siren(store)[SIREN_OK].status == Status.FOUND  # type: ignore[attr-defined]


def test_budget_stops_reading_and_leaves_leads_for_next_run(cfg: Config, store: Store) -> None:
    p = pipeline(cfg, store, budget=1)
    p.run(["scout", "match", "read"])
    leads = by_siren(store)
    assert leads[SIREN_OK].status == Status.READ  # type: ignore[attr-defined]
    assert leads[SIREN_TOOL].status == Status.MATCHED  # type: ignore[attr-defined]
    assert "token budget" in daily_report(store, cfg, p.run_id)


def test_new_rules_version_rescores(cfg: Config, store: Store) -> None:
    pipeline(cfg, store).run()
    cfg.rules_version = "v2"
    cfg.scoring.need["manual_invoicing"] = 0
    pipeline(cfg, store, run_id="r2").run(["score", "validate"])
    lead = by_siren(store)[SIREN_OK]
    assert lead.verdict.rules_version == "v2"  # type: ignore[attr-defined]


def test_review_list_shows_quotes_and_links(cfg: Config, store: Store) -> None:
    cfg.scoring.fit["english_pages"] = 0  # fit drops to 40, inside the review band
    cfg.scoring.fit["generic_contact"] = 0
    pipeline(cfg, store).run()
    text = review_list(store)
    assert "score_band" in text and "https://exemple-conseil.fr/faq" in text and '"' in text


def test_directory_pages_never_prove_a_site(cfg: Config, store: Store) -> None:
    from leadh.http import Deadline
    from leadh.models import Lead

    listing = "<p>" + " ".join(f"SIREN 81234{i:04d}" for i in range(6)) + " SIREN 812345674</p>"
    per_company = "<h1>EXEMPLE CONSEIL</h1><p>SIREN : 812 345 674</p>"
    extra = {"https://annuaire-pro.fr/": listing,
             "https://fiches-societes.fr/": "<p>Toutes les fiches</p>",
             "https://fiches-societes.fr/societe/exemple-conseil-812345674/mentions-legales": per_company}
    http = make_http(cfg, fake_web(extra=extra))
    search = StaticSearch({'"812345674"': ["https://annuaire-pro.fr/",
                                           "https://fiches-societes.fr/societe/exemple-conseil-812345674/mentions-legales"]})
    lead = Lead(lead_id="L-1", siren=SIREN_OK, name="EXEMPLE CONSEIL")
    assert SiteMatcher(http, search, 4, 5).match(lead, Deadline(60)) is None
