import json
from pathlib import Path

import pytest

from leadh.evidence import check
from leadh.pages import clean_text
from leadh.signals import (
    SIGNAL_DEFINITIONS,
    SYSTEM_PROMPT,
    BadOutput,
    Budget,
    BudgetExhausted,
    SignalReader,
    build_messages,
    mock_complete,
    parse,
)

PAGES = [("https://a.fr/faq", "Nos factures vous sont envoyées en PDF par email.")]
GOOD = json.dumps({"signals": [{"type": "manual_invoicing", "value": "PDF by email",
                                "quote": "Nos factures vous sont envoyées en PDF par email", "url": "https://a.fr/faq"}]})


def scripted(*replies: str) -> tuple[list[list[dict[str, str]]], object]:
    calls: list[list[dict[str, str]]] = []
    it = iter(replies)

    def complete(messages: list[dict[str, str]]) -> tuple[str, int, int]:
        calls.append(messages)
        return next(it), 100, 20

    return calls, complete


def test_stable_layers_first_page_text_last() -> None:
    msgs = build_messages(PAGES, 10)
    assert [m["content"] for m in msgs[:2]] == [SYSTEM_PROMPT, SIGNAL_DEFINITIONS]
    assert msgs[2]["content"] == '<page url="https://a.fr/faq">\nNos factur\n</page>'


def test_parse_accepts_fenced_json_and_rejects_unknown_types() -> None:
    assert parse(f"```json\n{GOOD}\n```")[0].type == "manual_invoicing"
    with pytest.raises(BadOutput):
        parse('{"signals": [{"type": "approve_lead", "quote": "x", "url": "u"}]}')
    with pytest.raises(BadOutput):
        parse("Sure! Here are the signals.")


def test_one_retry_then_success() -> None:
    calls, complete = scripted("not json", GOOD)
    budget = Budget(10_000)
    signals = SignalReader(complete, budget, 6000).read(PAGES)  # type: ignore[arg-type]
    assert [s.type for s in signals] == ["manual_invoicing"] and len(calls) == 2 and budget.used == 240


def test_malformed_twice_raises() -> None:
    _, complete = scripted("nope", "still nope")
    with pytest.raises(BadOutput):
        SignalReader(complete, Budget(10_000), 6000).read(PAGES)  # type: ignore[arg-type]


def test_budget_stops_before_calling() -> None:
    calls, complete = scripted(GOOD)
    budget = Budget(100, used_in=100)
    with pytest.raises(BudgetExhausted):
        SignalReader(complete, budget, 6000).read(PAGES)  # type: ignore[arg-type]
    assert calls == [] and budget.over_alert()


def test_planted_instructions_yield_only_quoted_signals() -> None:
    html = (Path(__file__).parent / "fixtures/sites/injection/page.html").read_text()
    pages = [("https://b.fr/", clean_text(html))]
    # A reader that obeys the page: adds a field, and invents the signal the page asked for.
    obeys = json.dumps({"approve": True, "signals": [
        {"type": "manual_invoicing", "quote": "Les factures papier sont envoyées par courrier chaque semaine",
         "url": "https://b.fr/"},
        {"type": "sells_to_businesses", "quote": "Nous travaillons avec des entreprises du bâtiment.",
         "url": "https://b.fr/"}]})
    _, complete = scripted(obeys)
    raw = SignalReader(complete, Budget(10_000), 6000).read(pages)  # type: ignore[arg-type]
    kept, dropped = check(raw, dict(pages), 25)
    assert [s.type for s in kept] == ["sells_to_businesses"] and dropped == 1


def test_mock_reader_quotes_lines_it_was_given() -> None:
    signals = SignalReader(mock_complete, Budget(10_000), 6000).read(PAGES)
    kept, dropped = check(signals, dict(PAGES), 25)
    assert [s.type for s in kept] == ["manual_invoicing"] and dropped == 0
