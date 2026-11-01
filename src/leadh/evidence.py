"""Evidence check: a signal survives only if its quote is on the page it cites, in the text the model was given."""

import re
import unicodedata

from leadh.models import Signal

QUOTE_MARKS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "«": '"',
                             "»": '"', "–": "-", "—": "-", " ": " ", " ": " "})


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(QUOTE_MARKS).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .…\"'")


def check(signals: list[Signal], page_text: dict[str, str], max_words: int) -> tuple[list[Signal], int]:
    """(kept signals marked checked, number dropped as unsupported). Only case, spacing and typographic quotes
    may differ; duplicates are merged."""
    pages = {url: normalise(text) for url, text in page_text.items()}
    kept: list[Signal] = []
    dropped = 0
    seen: set[tuple[str, str]] = set()
    for s in signals:
        quote = normalise(s.quote)
        text = pages.get(s.url)
        if not quote or text is None or len(quote.split()) > max_words or quote not in text:
            dropped += 1
            continue
        if (s.type, quote) in seen:
            continue
        seen.add((s.type, quote))
        kept.append(s.model_copy(update={"checked": True}))
    return kept, dropped
