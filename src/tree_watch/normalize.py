"""Hebrew address normalisation shared by the runtime bot and the index builder.

Both sides of the lookup (the address layer and the scraped licence text) must be
normalised with exactly the same code, otherwise the keys will never meet.
"""

from __future__ import annotations

import re
import unicodedata

# Prefixes stripped only when they are the FIRST word of the name.
# Stripping them anywhere would turn "נתיב אליעזר" into "אליעזר" and collide
# with "קריית אליעזר".
STREET_PREFIXES = (
    "רחוב", "רח", "שדרות", "שדרה", "שד", "דרך", "סמטת", "סמטה", "משעול",
    "נתיב", "מעלה", "מורד", "כיכר", "ככר", "כיכר", "טיילת", "מדרגות",
)

# Characters that appear inconsistently between sources: gershayim, apostrophes,
# maqaf, commas, periods.
_PUNCT = dict.fromkeys(map(ord, "\"'\u05f3\u05f4\u2018\u2019\u201c\u201d,.\u05be\u2013\u2014"), None)

_HOUSE_TOKEN = re.compile(
    r"""^(
        \d+[א-ת]?                 # 12, 12א
        (\s*[-/,]\s*\d+[א-ת]?)*   # 14/16/18, 55-61, 14,16
    )$""",
    re.VERBOSE,
)

_LEADING_NUM = re.compile(r"\d+")


def fix_bidi(text: str) -> str:
    """Convert visually-ordered text (as pdfplumber returns it) to logical order.

    Applied per line: a multi-line cell is a separate BiDi paragraph per line.
    """
    from bidi.algorithm import get_display  # imported lazily, optional dependency

    if text is None:
        return ""
    return "\n".join(get_display(line) for line in str(text).split("\n"))


def clean_text(text: object) -> str:
    """Collapse whitespace and normalise unicode, without touching word order."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = s.replace("\u00a0", " ").replace("\n", " ").replace("\t", " ")
    return " ".join(s.split())


def split_street_house(text: object) -> tuple[str, str]:
    """Split "ציון 22" or "הרב אריה לוין 14/16/18" into (street, house).

    Yeela puts the house number inside the street string; the municipal PDF keeps
    it in its own column. Both end up here.
    """
    s = clean_text(text)
    if not s:
        return "", ""
    words = s.split()
    house_parts: list[str] = []
    while words and _HOUSE_TOKEN.match(words[-1]):
        house_parts.insert(0, words.pop())
    return " ".join(words), " ".join(house_parts)


def parse_house_number(house: object) -> int | None:
    """First integer in a house string. "14/16/18" -> 14, "55-61" -> 55, "1א" -> 1."""
    m = _LEADING_NUM.search(clean_text(house))
    return int(m.group()) if m else None


def normalize_street(name: object) -> str:
    """Canonical key for a street name.

    Drops punctuation, a leading street-type word, and any trailing house number.
    """
    s = clean_text(name)
    if not s:
        return ""
    # "(החיד"א) ר' חיים יוסף" -> "ר חיים יוסף"; the parenthesised form is kept
    # as a separate alias by street_keys().
    stripped = re.sub(r"\([^)]*\)", " ", s)
    if stripped.strip():
        s = stripped
    s = s.translate(_PUNCT).replace("(", " ").replace(")", " ")
    s = " ".join(s.split())

    words = s.split()
    if len(words) > 1 and words[0] in STREET_PREFIXES:
        words = words[1:]

    while len(words) > 1 and _HOUSE_TOKEN.match(words[-1]):
        words.pop()

    return " ".join(words)


def street_keys(name: object) -> list[str]:
    """Primary key plus safe aliases, ordered from most to least specific.

    Handles the two shapes that actually appear in the Haifa street table:
      "(החידא) ר חיים יוסף"  -> also keyed as "החידא" and as "ר חיים יוסף"
      "הגליל"                -> also keyed as "גליל" (definite-article variance)
    """
    s = clean_text(name)
    keys: list[str] = []

    def add(value: str) -> None:
        value = normalize_street(value)
        if value and value not in keys:
            keys.append(value)

    add(s)

    inner = re.findall(r"\(([^)]*)\)", s)
    outside = re.sub(r"\([^)]*\)", " ", s)
    if inner:
        add(outside)
        for part in inner:
            add(part)

    for key in list(keys):
        if len(key) > 3 and key.startswith("ה"):
            add(key[1:])

    return keys
