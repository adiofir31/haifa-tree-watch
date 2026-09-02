"""Haifa municipality's published request table (PDF).

BiDi
----
``pdfplumber`` returns this PDF's text in **visual** order — the first header
cell arrives as ``'תורעה\\nםיצעל'``. Every Hebrew cell from this source must go
through ``normalize.fix_bidi`` (``get_display``) to become logical text. This
was checked against all 3,466 street cells in the captured fixture. The Yeela
API is the opposite case; do not unify the two paths.

Table layout (15 columns, verified against ``tests/fixtures/diag_pdf_raw.json``,
432 pages / 3,479 data rows)::

    [ 0] הערות לעצים    per-tree action    <- authoritative
    [ 1] מספר עצים      trees on this row
    [ 2] שם עץ          species
    [ 3] סוג עץ         genus
    [ 4] הערות לבקשה    notes
    [ 5] בית            house number
    [ 6] רח             street
    [ 7] מקום הבקשה     city
    [ 8] סיבה2          free-text note, carries the "cannot appeal" wording
    [ 9] סיבה           reason, controlled vocabulary
    [10] שם             applicant
    [11] פעולה          per-request action  <- fallback only
    [12] תאריך אחרון לערעור
    [13] תאריך בקשה
    [14] מספר בקשה      request number, the state key for this source
"""

from __future__ import annotations

import io
import logging
from datetime import date, datetime

import pdfplumber

from .. import config, net
from ..models import Action, Licence, Source, SourceStats, Stage
from ..normalize import clean_text, fix_bidi, split_street_house

log = logging.getLogger(__name__)

# Column indices, named once.
C_TREE_ACTION = 0
C_COUNT = 1
C_SPECIES = 2
C_HOUSE = 5
C_STREET = 6
C_NOTE2 = 8
C_REASON = 9
C_APPLICANT = 10
C_REQ_ACTION = 11
C_APPEAL_DATE = 12
C_REQUEST_DATE = 13
C_REQUEST_NO = 14

#: Expected header, in logical order. Validated on every run — if the
#: municipality changes the table we must fail loudly, not silently report the
#: wrong columns to a public channel.
EXPECTED_HEADER = [
    "הערות לעצים",
    "מספר עצים",
    "שם עץ",
    "סוג עץ",
    "הערות לבקשה",
    "בית",
    "רח",
    "מקום הבקשה",
    "סיבה2",
    "סיבה",
    "שם",
    "פעולה",
    "תאריך אחרון לערעור",
    "תאריך בקשה",
    "מספר בקשה",
]

#: Variant spellings seen in column 0 of the real table. The municipality types
#: these by hand, so the set grows; anything unrecognised becomes OTHER and is
#: logged rather than silently counted as felling.
ACTION_WORDS: dict[str, Action] = {
    "כריתה": Action.FELL,
    "הכריתה": Action.FELL,
    "כרתה": Action.FELL,
    "כריתב": Action.FELL,  # typo, 1 row
    "כריתה .": Action.FELL,  # stray period, 1 row
    "העתקה": Action.RELOCATE,
    "שימור": Action.PRESERVE,
    "שמירה": Action.PRESERVE,  # synonym of שימור, used interchangeably here
    "נדחה": Action.OTHER,
}

CANNOT_APPEAL_MARKER = "לא ניתן לערר"


class HeaderMismatch(RuntimeError):
    """The PDF table is not the shape we parse. Refuse to guess."""


def classify_action(tree_cell: str, request_cell: str) -> Action:
    """Column 0 wins; column 11 is the fallback for the ~42 rows where 0 is blank.

    The two columns disagree on 174 rows in the captured table — 149 of them are
    relocations filed under a felling request. Reading column 11, as the old bot
    effectively did, reports trees as doomed that are only being moved.
    """
    for cell in (tree_cell, request_cell):
        text = " ".join(clean_text(cell).split())
        if not text:
            continue
        if text in ACTION_WORDS:
            return ACTION_WORDS[text]
        # Multi-line free text such as "שתילת עצים לפי ערך" — look for a known
        # word inside it before giving up.
        for word, action in ACTION_WORDS.items():
            if word in text:
                return action
        log.info("unrecognised action cell %r — treating as OTHER", text)
        return Action.OTHER
    return Action.OTHER


def heb(raw_cell: str) -> str:
    """Visual-order cell -> readable logical text, on one line.

    Order matters and is the whole point: ``fix_bidi`` first, so each physical
    line is reordered on its own and the lines stay in their original sequence;
    whitespace collapsing only afterwards. Doing it the other way round reverses
    the word order of every multi-line cell.
    """
    return clean_text(fix_bidi(raw_cell))


def _parse_date(cell: str) -> date | None:
    text = clean_text(cell)
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        return None


def _to_int(cell: str, default: int = 1) -> int:
    text = clean_text(cell)
    return int(text) if text.isdigit() else default


def validate_header(header: list[str]) -> None:
    """Fail loudly if the table shape changed."""
    got = [heb(c) for c in header]
    if got != EXPECTED_HEADER:
        diff = [
            f"[{i}] expected {exp!r} got {act!r}"
            for i, (exp, act) in enumerate(
                zip(EXPECTED_HEADER, got + [""] * len(EXPECTED_HEADER))
            )
            if exp != act
        ]
        raise HeaderMismatch(
            "municipal PDF table header changed — refusing to parse.\n"
            + "\n".join(diff[:8])
            + f"\n(expected {len(EXPECTED_HEADER)} columns, got {len(got)})"
        )


def parse_tables(pages: list[list[list[str]]], today: date | None = None) -> list[Licence]:
    """Turn extracted tables into one :class:`Licence` per request number.

    ``pages`` is a list of tables, each a list of rows. Taking the already
    extracted rows rather than a PDF keeps this testable against the fixture.
    """
    today = today or date.today()

    header_seen = False
    by_request: dict[str, Licence] = {}
    skipped_no_id = 0

    for table in pages:
        for row in table:
            # Keep the raw cells: fix_bidi MUST see the line breaks. Cleaning
            # first would collapse a two-line cell into one line, and
            # get_display would then reverse the whole thing, word order
            # included — 'סוקיפ\nתורדשה' becoming 'השדרות פיקוס' instead of
            # 'פיקוס השדרות'. 2,006 species cells and 984 street cells in the
            # published table are multi-line.
            raw = [("" if cell is None else str(cell)) for cell in row]
            if len(raw) < len(EXPECTED_HEADER):
                continue
            cells = [clean_text(cell) for cell in raw]  # numeric/date columns

            if not header_seen:
                validate_header(raw[: len(EXPECTED_HEADER)])
                header_seen = True
                continue
            # Repeated header rows on later pages, if the layout ever changes.
            if heb(raw[C_REQUEST_NO]) == EXPECTED_HEADER[-1]:
                continue

            request_no = cells[C_REQUEST_NO]
            if not request_no:
                skipped_no_id += 1
                continue

            appeal = _parse_date(cells[C_APPEAL_DATE])
            published = _parse_date(cells[C_REQUEST_DATE])

            action = classify_action(
                heb(raw[C_TREE_ACTION]), heb(raw[C_REQ_ACTION])
            )
            count = _to_int(cells[C_COUNT])
            species = heb(raw[C_SPECIES])

            street_cell = heb(raw[C_STREET])
            house_cell = cells[C_HOUSE]
            if not house_cell:
                street_cell, house_cell = split_street_house(street_cell)

            note2 = heb(raw[C_NOTE2])

            record = Licence(
                source=Source.HAIFA_PDF,
                source_id=request_no,
                stage=Stage.LICENSED,
                street=street_cell,
                house=house_cell,
                appeal_last=appeal,
                published=published,
                # The old test read column 9, whose vocabulary never contains
                # the word it looked for, so it was always True. The wording
                # actually lives in column 8.
                can_appeal=CANNOT_APPEAL_MARKER not in note2,
                applicant=heb(raw[C_APPLICANT]),
                reason=heb(raw[C_REASON]),
            )
            if action is Action.FELL:
                record.fell_count = count
                if species:
                    record.species[species] = count
            elif action is Action.RELOCATE:
                record.relocate_count = count
            elif action is Action.PRESERVE:
                record.preserve_count = count

            existing = by_request.get(request_no)
            if existing is None:
                by_request[request_no] = record
            else:
                # sum, not max: one row per species, 485 requests have several
                existing.merge(record)

    if skipped_no_id:
        log.warning("skipped %d PDF rows with no request number", skipped_no_id)
    if not header_seen:
        raise HeaderMismatch("no header row found in the municipal PDF")

    return list(by_request.values())


def relevant(records: list[Licence], today: date | None = None) -> list[Licence]:
    """Requests whose appeal window has not closed.

    ``today`` is a ``date``. The old code compared against ``datetime.now()``,
    which carries a time, while PDF dates parse to midnight — so on the 18:00
    run a licence whose deadline is *today* compared as "already past" and was
    dropped. Those are the most urgent ones.
    """
    today = today or date.today()
    return [r for r in records if r.appeal_last and r.appeal_last >= today]


def fetch(url: str | None = None) -> list[list[list[str]]]:
    """Download the PDF and extract every page's table."""
    url = url or config.PDF_URL
    log.info("downloading municipal PDF")
    response = net.get(url)
    log.info("municipal PDF: %d bytes", len(response.content))

    tables: list[list[list[str]]] = []
    with pdfplumber.open(io.BytesIO(response.content)) as pdf:
        log.info("municipal PDF: %d pages", len(pdf.pages))
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                tables.append([["" if c is None else str(c) for c in row] for row in table])
    return tables


def collect(
    today: date | None = None, url: str | None = None
) -> tuple[list[Licence], SourceStats]:
    tables = fetch(url)
    stats = SourceStats(
        source=Source.HAIFA_PDF.value,
        pages=len(tables),
        # minus the single header row, which only appears on the first page
        raw_rows=max(sum(len(table) for table in tables) - 1, 0),
    )
    records = parse_tables(tables, today=today)
    stats.requests = len(records)
    keep = relevant(records, today=today)
    stats.passed_date_filter = len(keep)
    return keep, stats
