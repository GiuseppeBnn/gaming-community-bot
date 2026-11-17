"""Conservative, title-specific spelling variants; no fuzzy automatic wins."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

_ROMAN = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
    "vii": "7", "viii": "8", "ix": "9", "xi": "11", "xii": "12", "xiii": "13",
}


def _words(title: str) -> tuple[str, ...]:
    folded = unicodedata.normalize("NFKD", title).casefold()
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", folded)
    words = re.findall(r"\w+", folded)
    if len(words) > 1:
        words[-1] = _ROMAN.get(words[-1], words[-1])
    return tuple(words)


def matches_title(answer: str, canonical: str, aliases: Iterable[str] = ()) -> bool:
    """Accept formatting, numeral and numbered-title initials without dropping words.

    X is deliberately not a numeral (Mega Man X != Mega Man 10). Never strip
    remake/DLC subtitles or use edit-distance thresholds on a path paying coins.
    Less obvious abbreviations/translations/typos belong to a strict AI judge.
    """
    candidate = _words(answer)
    if not candidate:
        return False
    for title in (canonical, *aliases):
        words = _words(title)
        if candidate == words:
            return True
        if len(words) >= 3 and words[-1].isdigit() and all(w.isalpha() for w in words[:-1]):
            acronym = "".join(w[0] for w in words[:-1])
            if candidate == (acronym, words[-1]):
                return True
    return False
