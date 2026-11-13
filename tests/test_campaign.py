import csv
from datetime import date
from pathlib import Path

import pytest

from leadh.campaign import compare, load_campaign
from leadh.cli import main
from leadh.outreach import variant_for
from leadh.pipeline import Pipeline
from leadh.registry import Registry
from leadh.search import StaticSearch
from leadh.signals import Budget, SignalReader, mock_complete
from leadh.site import SiteMatcher
from leadh.store import Store

from .conftest import ROOT, SEARCH, SIREN_OK, fake_web, make_http


@pytest.fixture
def campaign_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LLM_BASE_URL", "mock")
    monkeypatch.setenv("LEADH_DATA", str(tmp_path / "data"))
    p = tmp_path / "c.yaml"
    p.write_text(f"""
name: test-campaign
profile: {ROOT / "config" / "leadh.yaml"}
source:
  naf_codes: ["70.22Z"]
templates:
  A: {ROOT / "templates" / "einvoicing-short.txt"}
  B: {ROOT / "templates" / "einvoicing-peer.txt"}
lead_count: 5
""")
    return p


def run_pipeline(campaign_file: Path) -> Store:
    c = load_campaign(campaign_file)
    store = Store(c.cfg.data_dir / "leadh.db", c.cfg.ledger)
    http = make_http(c.cfg, fake_web())
    Pipeline(cfg=c.cfg, store=store, registry=Registry(c.cfg, http),
             matcher=SiteMatcher(http, StaticSearch(SEARCH), 3, c.cfg.limits.pages_per_site),
             reader=SignalReader(mock_complete, Budget(1_000_000), 6000), run_id="r1", campaign=c.name,
             mx=lambda d: True, today=lambda: date(2026, 11, 2)).run()
    return store


def test_campaign_overrides_source_and_isolates_data(campaign_file: Path, tmp_path: Path) -> None:
    c = load_campaign(campaign_file)
    assert c.cfg.registry.naf_codes == ["70.22Z"] and c.cfg.registry.regions == ["11"]
    assert c.cfg.data_dir == tmp_path / "data" / "campaigns" / "test-campaign"
    assert c.variants == ["A", "B"]


def test_bad_campaign_name(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("name: My Campaign\nprofile: x.yaml\ntemplates: {A: t.txt}\nlead_count: 1\n")
    with pytest.raises(ValueError, match="lowercase"):
        load_campaign(p)


def test_draft_export_outcome_compare(campaign_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = run_pipeline(campaign_file)
    main(["-q", "--campaign", str(campaign_file), "draft"])
    lead = next(x for x in store.all() if x.siren == SIREN_OK)
    assert lead.draft and lead.campaign == "test-campaign"
    assert lead.draft.variant == variant_for("test-campaign", SIREN_OK, ["A", "B"])
    assert lead.draft.status == "ready" and "[[" not in lead.draft.body
    assert lead.draft.subject == lead.draft.subject.lower()
    for fill in lead.draft.slots.values():
        assert fill.url.startswith("https://exemple-conseil.fr")

    # A second draft run neither redrafts nor changes the variant.
    main(["-q", "--campaign", str(campaign_file), "draft"])
    assert store.get(lead.lead_id).draft == lead.draft  # type: ignore[union-attr]

    out = load_campaign(campaign_file).cfg.data_dir / "leads.csv"
    main(["-q", "--campaign", str(campaign_file), "export"])
    row = next(r for r in csv.DictReader(out.open()) if r["siren"] == SIREN_OK)
    assert row["variant"] == lead.draft.variant and row["subject"] == lead.draft.subject
    assert row["body"] == lead.draft.body and row["campaign"] == "test-campaign"

    main(["-q", "--campaign", str(campaign_file), "outcome", lead.lead_id, "sent"])
    main(["-q", "--campaign", str(campaign_file), "outcome", lead.lead_id, "replied"])
    assert store.contacted(SIREN_OK)
    rows = {r.variant: r for r in compare([load_campaign(campaign_file)])}
    assert rows[lead.draft.variant].replied == 1 and rows[lead.draft.variant].reply_rate == 1.0
    capsys.readouterr()
    main(["-q", "compare", str(campaign_file)])
    assert "too few to call a winner" in capsys.readouterr().out


def test_lead_count_caps_drafts(campaign_file: Path) -> None:
    campaign_file.write_text(campaign_file.read_text().replace("lead_count: 5", "lead_count: 0"))
    store = run_pipeline(campaign_file)
    main(["-q", "--campaign", str(campaign_file), "draft"])
    assert not any(x.draft for x in store.all())


def test_ledger_is_shared_between_campaigns(campaign_file: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.yaml"
    other.write_text(campaign_file.read_text().replace("name: test-campaign", "name: other-campaign"))
    a, b = load_campaign(campaign_file), load_campaign(other)
    assert a.cfg.ledger == b.cfg.ledger == tmp_path / "data" / "ledger.db"
    Store(a.cfg.data_dir / "leadh.db", a.cfg.ledger).add_contacted(SIREN_OK)
    Store(a.cfg.data_dir / "leadh.db", a.cfg.ledger).add_optout("domain", "example.fr")
    shared = Store(b.cfg.data_dir / "leadh.db", b.cfg.ledger)
    assert shared.contacted(SIREN_OK) and shared.opted_out("000000000", "example.fr")


def test_old_entries_move_into_the_ledger(tmp_path: Path) -> None:
    old = Store(tmp_path / "leadh.db")
    old.add_contacted(SIREN_OK)
    old.close()
    assert Store(tmp_path / "leadh.db", tmp_path / "ledger.db").contacted(SIREN_OK)
    assert Store(tmp_path / "x" / "leadh.db", tmp_path / "ledger.db").contacted(SIREN_OK)


def test_contacted_elsewhere_is_not_drafted(campaign_file: Path) -> None:
    store = run_pipeline(campaign_file)
    store.add_contacted(SIREN_OK)
    main(["-q", "--campaign", str(campaign_file), "draft"])
    assert not any(x.draft for x in store.all())
