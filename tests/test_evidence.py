from leadh.evidence import check
from leadh.models import Signal

PAGE = {"https://a.fr/faq": "Comment suis-je facturé ?\nNos factures vous sont envoyées en PDF par email à la fin."}


def sig(quote: str, url: str = "https://a.fr/faq", type_: str = "manual_invoicing") -> Signal:
    return Signal.model_validate({"type": type_, "quote": quote, "url": url})


def test_exact_and_typographic_variants_kept() -> None:
    kept, dropped = check([sig("nos factures vous sont envoyées en PDF par email"),
                           sig("Comment suis-je facturé ?", type_="sells_to_businesses")], PAGE, 25)
    assert dropped == 0 and all(s.checked for s in kept) and len(kept) == 2


def test_paraphrase_wrong_page_and_long_quote_dropped() -> None:
    long = " ".join(["mot"] * 26)
    kept, dropped = check([sig("Les factures sont envoyées en PDF"), sig("Nos factures", url="https://a.fr/x"),
                           sig(long), sig("")], {**PAGE, "https://a.fr/l": long}, 25)
    assert kept == [] and dropped == 4


def test_duplicates_merged_not_counted_as_dropped() -> None:
    kept, dropped = check([sig("Nos factures vous sont envoyées"), sig("nos factures vous sont envoyées")], PAGE, 25)
    assert len(kept) == 1 and dropped == 0
