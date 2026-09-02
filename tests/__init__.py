"""Test support: put ``src`` on the path and cache the real fixtures.

Run with the production venv, no installs required:

    venv\\Scripts\\python.exe -m unittest discover -s tests -t .

pytest, once installed, collects these unchanged.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def legacy_lines() -> list[str]:
    """Non-empty lines of the live ``sent_licenses.txt``.

    This file is production state and **grows every evening** while the legacy
    bot is still the scheduled job. Tests must therefore derive their expected
    counts from it rather than hardcoding, and assert invariants (how the ids
    split, that nothing is lost) instead of totals.
    """
    return [
        line.strip()
        for line in (ROOT / "sent_licenses.txt").read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


@lru_cache(maxsize=None)
def pdf_pages() -> list[list[list[str]]]:
    """Every table from the captured municipal PDF, in visual order."""
    raw = json.loads((FIXTURES / "diag_pdf_raw.json").read_text(encoding="utf-8"))
    return [page["table"] for page in raw if page.get("table")]


@lru_cache(maxsize=None)
def pdf_data_rows() -> list[list[str]]:
    """Every data row of the municipal table.

    Only page 0 carries a header row; slicing ``page[1:]`` on every page would
    silently drop one real row per page.
    """
    pages = pdf_pages()
    header = pages[0][0]
    return [row for page in pages for row in page if row is not header]


@lru_cache(maxsize=None)
def yeela_rows() -> list[dict]:
    """The full Yeela pull, flat list of licence x species rows."""
    return json.loads((FIXTURES / "yeela_full.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def yeela_envelope() -> list[dict]:
    """Pages 1-2 with the response envelope intact, including ``pagination``."""
    return json.loads((FIXTURES / "yeela_raw.json").read_text(encoding="utf-8"))
