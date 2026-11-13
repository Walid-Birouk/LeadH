import json
from pathlib import Path

import pytest

from leadh.models import Contact, Lead, Site, SlotFill
from leadh.outreach import (
    Template,
    TemplateError,
    fill_slots,
    load_template,
    mock_slots,
    render,
    slot_messages,
    variant_for,
)
from leadh.signals import BadOutput, Budget

ROOT = Path(__file__).parent.parent
PAGES = {"https://a.fr/": "Nos clients : Maisons Verdier et Atelier Lumen.\n"
                          "Sophie Martin, directrice associée, pilote chaque mission.\n"
                          "12 ans d'accompagnement de PME en Île-de-France."}
LEAD = Lead(lead_id="L-000001", siren="812345674", name="EXEMPLE CONSEIL", city="PARIS",
            site=Site(url="https://a.fr/", siren_found_at="https://a.fr/legal"),
            contact=Contact(email="contact@a.fr", kind="generic", source_url="https://a.fr/contact"))


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "t.txt"
    p.write_text(text)
    return p


def scripted(*replies: str) -> object:
    it = iter(replies)
    return lambda messages: (next(it), 10, 5)


def test_example_templates_load() -> None:
    for p in (ROOT / "templates").glob("*.txt"):
        assert load_template(p).subject == load_template(p).subject.lower()


def test_template_rules(tmp_path: Path) -> None:
    with pytest.raises(TemplateError, match="lowercase"):
        load_template(write(tmp_path, "subject: Quick Question\n---\nHi"))
    with pytest.raises(TemplateError, match="unknown slots"):
        load_template(write(tmp_path, "subject: hi\n---\nHi {first_name}"))
    with pytest.raises(TemplateError, match="subject"):
        load_template(write(tmp_path, "Hi there"))
    with pytest.raises(TemplateError, match="under"):
        load_template(write(tmp_path, "subject: one two three four five six seven eight nine\n---\nHi"))
    t = load_template(write(tmp_path, "subject: about {company}\n---\nSaw {peer_company|your clients}."))
    assert t.slots == ["company", "peer_company"] and t.model_slots == ["peer_company"]


def test_variant_is_stable_and_splits_evenly() -> None:
    sirens = [f"{n:09d}" for n in range(2000)]
    first = [variant_for("c1", s, ["A", "B"]) for s in sirens]
    assert first == [variant_for("c1", s, ["A", "B"]) for s in reversed(sirens)][::-1]
    assert 900 < first.count("A") < 1100
    assert variant_for("c1", "812345674", ["A"]) == "A"
    # Another campaign draws its own split, so one company is not always in the same arm.
    assert first != [variant_for("c2", s, ["A", "B"]) for s in sirens]


def test_only_quoted_fills_survive() -> None:
    answer = json.dumps({"slots": {
        "peer_company": {"value": "Maisons Verdier", "quote": "Nos clients : Maisons Verdier et Atelier Lumen",
                         "url": "https://a.fr/"},
        "second_decision_maker": {"value": "Paul Durand, CEO", "quote": "Sophie Martin, directrice associée",
                                  "url": "https://a.fr/"},
        "specific_fact": {"value": "you won an award", "quote": "Prix de l'innovation 2025", "url": "https://a.fr/"}}})
    fills = fill_slots(scripted(answer), Budget(1000), ["peer_company", "second_decision_maker", "specific_fact"],  # type: ignore[arg-type]
                       PAGES, 6000, 25)
    assert list(fills) == ["peer_company"]


def test_malformed_twice_raises() -> None:
    with pytest.raises(BadOutput):
        fill_slots(scripted("nope", "still nope"), Budget(1000), ["peer_company"], PAGES, 6000, 25)  # type: ignore[arg-type]


def test_no_model_call_without_model_slots() -> None:
    assert fill_slots(scripted(), Budget(0), [], PAGES, 6000, 25) == {}  # type: ignore[arg-type]


def test_render_uses_fills_record_fallbacks_and_marks_gaps() -> None:
    t = Template("t", "about {company}", "Saw {peer_company}. {specific_fact|Hi.} {second_decision_maker}\n"
                                         "Found on {email_source}.\n")
    fill = SlotFill(value="Maisons Verdier", quote="Nos clients : Maisons Verdier", url="https://a.fr/")
    d = render(t, LEAD, "B", {"peer_company": fill})
    assert d.subject == "about exemple conseil" and d.variant == "B"
    assert d.body == "Saw Maisons Verdier. Hi. [[second_decision_maker]]\nFound on https://a.fr/contact.\n"
    assert d.status == "needs_edit"


def test_mock_slot_filler_quotes_the_page() -> None:
    slots = ["second_decision_maker", "peer_company", "specific_fact"]
    fills = fill_slots(mock_slots, Budget(10_000), slots, PAGES, 6000, 25)
    assert fills["peer_company"].value == "Maisons Verdier"
    assert fills["second_decision_maker"].value == "Sophie Martin, directrice associée"
    assert fills["specific_fact"].value.startswith("12 ans")
    assert "peer_company" in slot_messages(["peer_company"], list(PAGES.items()), 100)[1]["content"]
