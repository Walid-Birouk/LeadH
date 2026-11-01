from datetime import date

from leadh.config import Config
from leadh.models import Contact, Lead, Scores, Signal, Site, Tool
from leadh.scorer import score
from leadh.store import Store
from leadh.validator import validate

TODAY = date(2026, 11, 2)


def lead(**over: object) -> Lead:
    base = Lead(
        lead_id="L-000001", siren="812345674", name="Exemple", naf="70.22Z", category="PME", size_band="11",
        created_on="2025-09-14", region="11", site=Site(url="https://a.fr/", siren_found_at="https://a.fr/legal"),
        english_at="https://a.fr/en/",
        signals=[Signal(type="sells_to_businesses", quote="q", url="https://a.fr/", checked=True),
                 Signal(type="manual_invoicing", quote="q", url="https://a.fr/faq", checked=True)],
        contact=Contact(email="contact@a.fr", kind="generic", source_url="https://a.fr/contact"))
    return base.model_copy(update=over)


def mx_ok(domain: str) -> bool:
    return True


def test_full_need_and_fit(cfg: Config) -> None:
    s = score(lead(), cfg, TODAY)
    # 25 b2b + 25 manual + 15 band + 15 no tool + 10 recent; 40 english + 20 IdF + 20 NAF + 20 generic
    assert (s.need, s.fit) == (90, 100)
    assert {b["signal"]: b["source"] for b in s.breakdown}["manual_invoicing"] == "https://a.fr/faq"


def test_tool_lowers_need_and_unchecked_signals_count_for_nothing(cfg: Config) -> None:
    unchecked = [Signal(type="manual_invoicing", quote="q", url="u", checked=False)]
    s = score(lead(signals=unchecked, tools=[Tool(name="Sellsy", url="u")], created_on="2010-01-01"), cfg, TODAY)
    assert s.need == 0  # 15 band - 30 tool, floored at 0


def test_approved(cfg: Config, store: Store) -> None:
    lead_ = lead(scores=Scores(need=90, fit=100))
    v = validate(lead_, cfg, store, mx_ok)
    assert (v.status, v.reasons, v.rules_version) == ("APPROVED", [], "v1")


def test_each_hard_rule_rejects(cfg: Config, store: Store) -> None:
    good = {"scores": Scores(need=90, fit=100)}
    b2c = [Signal(type="sells_to_consumers", quote="q", url="u", checked=True)]
    cases = {
        "inactive": lead(active=False, **good),
        "not_sme": lead(category="ETI", **good),
        "diffusion_partial": lead(diffusion="P", **good),
        "commercial_opposition": lead(commercial_opposition=True, **good),
        "no_site_match": lead(site=None, **good),
        "b2c_only": lead(signals=b2c, **good),
        "no_published_contact": lead(contact=None, **good),
    }
    for code, case in cases.items():
        v = validate(case, cfg, store, mx_ok)
        assert v.status == "REJECTED" and code in v.reasons, code
    assert validate(lead(**good), cfg, store, lambda d: False).reasons == ["no_mx"]
    store.add_optout("domain", "A.fr")
    assert "opted_out" in validate(lead(**good), cfg, store, mx_ok).reasons
    store.add_contacted("812345674")
    assert "already_contacted" in validate(lead(**good), cfg, store, mx_ok).reasons


def test_unknown_category_falls_back_to_employee_band(cfg: Config, store: Store) -> None:
    good = {"scores": Scores(need=90, fit=100)}
    assert validate(lead(category=None, size_band="NN", **good), cfg, store, mx_ok).status == "APPROVED"
    assert "not_sme" in validate(lead(category=None, size_band="41", **good), cfg, store, mx_ok).reasons


def test_review_flags_and_score_bands(cfg: Config, store: Store) -> None:
    nominative = Contact(email="marie.durand@a.fr", kind="nominative", source_url="u")
    webmail = Contact(email="contact@gmail.com", kind="generic", free_mail=True, source_url="u")
    mixed = [*lead().signals, Signal(type="sells_to_consumers", quote="q", url="u", checked=True)]
    good = Scores(need=90, fit=100)
    assert validate(lead(contact=nominative, scores=good), cfg, store, mx_ok).reasons == ["nominative_address"]
    assert validate(lead(contact=webmail, scores=good), cfg, store, mx_ok).reasons == ["free_mail_domain"]
    assert validate(lead(signals=mixed, scores=good), cfg, store, mx_ok).reasons == ["mixed_customers"]
    assert validate(lead(scores=Scores(need=40, fit=100)), cfg, store, mx_ok).reasons == ["score_band"]
    assert validate(lead(scores=Scores(need=90, fit=50)), cfg, store, mx_ok).status == "CONDITIONAL"
    assert validate(lead(scores=Scores(need=30, fit=100)), cfg, store, mx_ok).reasons == ["low_score"]
    assert validate(lead(contact=nominative, scores=Scores(need=20, fit=20)), cfg, store, mx_ok).status == "REJECTED"


def test_unproven_site_needs_review(cfg: Config, store: Store) -> None:
    site = Site(url="https://a.fr/", siren_found_at="https://a.fr/contact", proof="name_address")
    assert validate(lead(site=site, scores=Scores(need=90, fit=100)), cfg, store, mx_ok).reasons == ["site_unproven"]
