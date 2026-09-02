"""Municipal PDF source — bugs 3, 7, 8, 9, 12 and the BiDi rule.

All of these run against the real captured table, not mocks.
"""

from __future__ import annotations

import unittest
from datetime import date

from bidi.algorithm import get_display

from tests import pdf_data_rows, pdf_pages
from tree_watch.models import Action, Source
from tree_watch.normalize import fix_bidi
from tree_watch.sources import haifa_pdf


def visual(text: str) -> str:
    """Logical Hebrew -> the visual order pdfplumber hands us."""
    return get_display(text)


def make_row(**cells: str) -> list[str]:
    """A 15-column data row, given by column name, already in visual order."""
    row = [""] * 15
    names = {
        "tree_action": haifa_pdf.C_TREE_ACTION,
        "count": haifa_pdf.C_COUNT,
        "species": haifa_pdf.C_SPECIES,
        "house": haifa_pdf.C_HOUSE,
        "street": haifa_pdf.C_STREET,
        "note2": haifa_pdf.C_NOTE2,
        "reason": haifa_pdf.C_REASON,
        "applicant": haifa_pdf.C_APPLICANT,
        "req_action": haifa_pdf.C_REQ_ACTION,
        "appeal": haifa_pdf.C_APPEAL_DATE,
        "requested": haifa_pdf.C_REQUEST_DATE,
        "request_no": haifa_pdf.C_REQUEST_NO,
    }
    for name, value in cells.items():
        row[names[name]] = value
    return row


HEADER = pdf_pages()[0][0]


def table(*rows: list[str]) -> list[list[list[str]]]:
    return [[HEADER, *rows]]


class TestBiDi(unittest.TestCase):
    """pdfplumber returns this source in VISUAL order; get_display is required."""

    def test_header_arrives_reversed(self):
        raw = HEADER[haifa_pdf.C_TREE_ACTION]
        self.assertIn("תורעה", raw, "fixture should be visually ordered")
        self.assertEqual("הערות לעצים", " ".join(fix_bidi(raw).split()))

    def test_street_cells_become_readable(self):
        pages = pdf_pages()
        row = pages[0][1]
        self.assertEqual("גן מרסיי", " ".join(fix_bidi(row[haifa_pdf.C_STREET]).split()))

    def test_multi_line_cell_keeps_its_line_order(self):
        # The regression that shipped: cleaning before fix_bidi collapses the
        # newline, so get_display reverses the whole cell including word order.
        # 2,006 species cells and 984 street cells in the table are multi-line.
        self.assertEqual("פיקוס השדרות", haifa_pdf.heb("סוקיפ\nתורדשה"))
        self.assertEqual("איקליפטוס המקור", haifa_pdf.heb("סוטפילקיא\nרוקמה"))
        self.assertEqual("אזדרכת מצויה", haifa_pdf.heb("תכרדזא\nהיוצמ"))

    def test_cleaning_before_fix_bidi_is_what_reverses_it(self):
        from tree_watch.normalize import clean_text

        wrong = clean_text(fix_bidi(clean_text("סוקיפ\nתורדשה")))
        self.assertEqual("השדרות פיקוס", wrong)
        self.assertNotEqual(wrong, haifa_pdf.heb("סוקיפ\nתורדשה"))

    def test_real_multi_line_species_cells_from_the_fixture(self):
        # FIXTURE-LOCKED: these exact cells are in the 2026-08-10 capture.
        expected = {
            "ןולפלפ\nהלא-יומד": "פלפלון דמוי-אלה",
            "סוקיפ\nתורדשה": "פיקוס השדרות",
        }
        seen = {}
        for row in pdf_data_rows():
            cell = row[haifa_pdf.C_SPECIES]
            if cell in expected:
                seen[cell] = haifa_pdf.heb(cell)
        self.assertTrue(seen, "no multi-line species cell found in the fixture")
        for cell, text in seen.items():
            self.assertEqual(expected[cell], text)

    def test_multi_line_cells_parse_into_records_in_the_right_order(self):
        records = haifa_pdf.parse_tables(pdf_pages())
        species = {name for r in records for name in r.species}
        multiword = [s for s in species if " " in s]
        self.assertTrue(multiword, "expected multi-word species names")
        # A reversed cell would produce these; the correct order is the other way.
        for reversed_form in ("השדרות פיקוס", "המקור איקליפטוס", "מצויה אזדרכת"):
            self.assertNotIn(reversed_form, species)
        self.assertIn("פיקוס השדרות", species)

    def test_multi_line_street_cells_parse_in_the_right_order(self):
        multi = [
            row for row in pdf_data_rows() if "\n" in (row[haifa_pdf.C_STREET] or "")
        ]
        self.assertGreater(len(multi), 500, "984 multi-line street cells expected")
        sample = multi[0][haifa_pdf.C_STREET]
        lines = [fix_bidi(line) for line in sample.split("\n")]
        self.assertEqual(" ".join(" ".join(lines).split()), haifa_pdf.heb(sample))

    def test_every_street_cell_round_trips(self):
        # The claim the brief rests on, checked across the whole table.
        checked = 0
        for row in pdf_data_rows():
            cell = row[haifa_pdf.C_STREET]
            if cell.strip():
                self.assertEqual(fix_bidi(fix_bidi(cell)), cell)
                checked += 1
        self.assertGreater(checked, 3000)


class TestHeaderValidation(unittest.TestCase):
    """Bug 12: hardcoded column indices with no validation."""

    def test_real_header_passes(self):
        haifa_pdf.validate_header(HEADER)

    def test_changed_header_fails_loudly(self):
        broken = list(HEADER)
        broken[haifa_pdf.C_STREET] = visual("כתובת")
        with self.assertRaises(haifa_pdf.HeaderMismatch):
            haifa_pdf.validate_header(broken)

    def test_missing_header_fails(self):
        with self.assertRaises(haifa_pdf.HeaderMismatch):
            haifa_pdf.parse_tables([])


class TestActionClassification(unittest.TestCase):
    """Bug 7: column 0 is authoritative, column 11 is only a fallback."""

    def test_column_zero_wins_over_column_eleven(self):
        records = haifa_pdf.parse_tables(
            table(
                make_row(
                    tree_action=visual("העתקה"),
                    req_action=visual("כריתה"),
                    count="4",
                    species=visual("אורן"),
                    street=visual("הולנד"),
                    house="36",
                    appeal="01/09/2026",
                    requested="20/08/2026",
                    request_no="9001",
                )
            )
        )
        self.assertEqual(1, len(records))
        self.assertEqual(0, records[0].fell_count)
        self.assertEqual(4, records[0].relocate_count)
        self.assertEqual({}, records[0].species)

    def test_column_eleven_used_when_column_zero_blank(self):
        records = haifa_pdf.parse_tables(
            table(
                make_row(
                    tree_action="",
                    req_action=visual("כריתה"),
                    count="2",
                    species=visual("ברוש"),
                    street=visual("הולנד"),
                    appeal="01/09/2026",
                    request_no="9002",
                )
            )
        )
        self.assertEqual(2, records[0].fell_count)

    def test_variant_spellings_normalise_to_felling(self):
        for variant in ("כריתה", "הכריתה", "כרתה", "כריתב", "כריתה ."):
            with self.subTest(variant=variant):
                self.assertEqual(
                    Action.FELL, haifa_pdf.classify_action(variant, "")
                )

    def test_preservation_is_not_felling(self):
        self.assertEqual(Action.PRESERVE, haifa_pdf.classify_action("שימור", "כריתה"))

    def test_shmira_is_a_synonym_of_preservation(self):
        self.assertEqual(Action.PRESERVE, haifa_pdf.classify_action("שמירה", "כריתה"))

    def test_a_preserved_tree_is_not_counted_as_felled(self):
        records = haifa_pdf.parse_tables(
            table(
                make_row(
                    tree_action=visual("שמירה"), req_action=visual("כריתה"),
                    count="7", species=visual("אורן"), street=visual("הולנד"),
                    appeal="01/09/2026", request_no="9300",
                )
            )
        )
        self.assertEqual(0, records[0].fell_count)
        self.assertEqual(7, records[0].preserve_count)
        self.assertEqual({}, records[0].species)

    def test_real_table_has_relocations_hidden_under_felling(self):
        # 149 rows where column 11 says felling and column 0 says relocation.
        # FIXTURE-LOCKED: 149 counts rows in tests/fixtures/diag_pdf_raw.json
        # (captured 2026-08-10). Re-capturing the fixture changes this number;
        # update it from the new capture rather than loosening the assertion.
        hidden = 0
        for row in pdf_data_rows():
            c0 = " ".join(fix_bidi(row[haifa_pdf.C_TREE_ACTION]).split())
            c11 = " ".join(fix_bidi(row[haifa_pdf.C_REQ_ACTION]).split())
            if c0 == "העתקה" and c11 == "כריתה":
                hidden += 1
        self.assertEqual(149, hidden)


class TestTreeCounts(unittest.TestCase):
    """Bug 8: multi-species requests must sum, not max."""

    def test_request_2273_sums_to_three(self):
        # FIXTURE-LOCKED: request 2273 (הולנד 36) exists in the 2026-08-10
        # capture as 2 pines + 1 cypress. A refreshed fixture may drop or change
        # it; re-pick an equivalent multi-species request if so.
        records = haifa_pdf.parse_tables(pdf_pages())
        by_id = {r.source_id: r for r in records}
        record = by_id["2273"]
        self.assertEqual(3, record.fell_count)
        self.assertEqual({"אורן": 2, "ברוש מצוי": 1}, record.species)

    def test_synthetic_two_species_sum(self):
        records = haifa_pdf.parse_tables(
            table(
                make_row(
                    tree_action=visual("כריתה"), count="2", species=visual("אורן"),
                    street=visual("הולנד"), house="36", appeal="01/09/2026",
                    request_no="2273",
                ),
                make_row(
                    tree_action=visual("כריתה"), count="1", species=visual("ברוש מצוי"),
                    street=visual("הולנד"), house="36", appeal="01/09/2026",
                    request_no="2273",
                ),
            )
        )
        self.assertEqual(1, len(records))
        self.assertEqual(3, records[0].fell_count)
        self.assertNotEqual(2, records[0].fell_count, "max() would give 2")

    def test_one_request_per_id(self):
        records = haifa_pdf.parse_tables(pdf_pages())
        self.assertEqual(len(records), len({r.source_id for r in records}))
        # FIXTURE-LOCKED: 2286 distinct requests in the 2026-08-10 capture.
        # Refreshing tests/fixtures/diag_pdf_raw.json requires updating this.
        self.assertEqual(2286, len(records))


class TestDateBoundary(unittest.TestCase):
    """Bug 3: a deadline of *today* must still be included."""

    def _one(self, appeal: str):
        return haifa_pdf.parse_tables(
            table(
                make_row(
                    tree_action=visual("כריתה"), count="1", species=visual("אורן"),
                    street=visual("הולנד"), appeal=appeal, requested="20/08/2026",
                    request_no="9100",
                )
            )
        )

    def test_deadline_today_is_kept(self):
        today = date(2026, 9, 1)
        records = self._one("01/09/2026")
        self.assertEqual(1, len(haifa_pdf.relevant(records, today=today)))

    def test_deadline_yesterday_is_dropped(self):
        today = date(2026, 9, 1)
        records = self._one("31/08/2026")
        self.assertEqual(0, len(haifa_pdf.relevant(records, today=today)))

    def test_deadline_tomorrow_is_kept(self):
        today = date(2026, 9, 1)
        records = self._one("02/09/2026")
        self.assertEqual(1, len(haifa_pdf.relevant(records, today=today)))

    def test_old_datetime_comparison_would_have_dropped_today(self):
        # Documents the original defect: datetime.now() at 18:00 is greater than
        # the same date parsed to midnight, so ">= today" excluded it.
        from datetime import datetime

        run_time = datetime(2026, 9, 1, 18, 0)
        parsed = datetime(2026, 9, 1, 0, 0)
        self.assertFalse(parsed >= run_time)
        self.assertTrue(parsed.date() >= run_time.date())


class TestAppealFlag(unittest.TestCase):
    """Bug 9: the test read the wrong column and was always True."""

    def test_cannot_appeal_detected_from_column_eight(self):
        records = haifa_pdf.parse_tables(
            table(
                make_row(
                    tree_action=visual("כריתה"), count="1", species=visual("אורן"),
                    street=visual("הולנד"), appeal="01/09/2026", request_no="9200",
                    note2=visual("לא ניתן לערר עקב מסוכנות העץ"),
                )
            )
        )
        self.assertFalse(records[0].can_appeal)

    def test_normal_row_can_appeal(self):
        records = haifa_pdf.parse_tables(
            table(
                make_row(
                    tree_action=visual("כריתה"), count="1", species=visual("אורן"),
                    street=visual("הולנד"), appeal="01/09/2026", request_no="9201",
                    note2=visual("בשטח הפיתוח"),
                )
            )
        )
        self.assertTrue(records[0].can_appeal)

    def test_old_check_against_column_nine_never_fires(self):
        # Column 9's vocabulary does not contain the word the old code looked for.
        vocabulary = {
            " ".join(fix_bidi(row[haifa_pdf.C_REASON]).split())
            for row in pdf_data_rows()
        }
        self.assertNotIn("מסוכן", " ".join(vocabulary))


class TestSourceTagging(unittest.TestCase):
    def test_records_are_tagged_and_namespaced(self):
        records = haifa_pdf.parse_tables(
            table(
                make_row(
                    tree_action=visual("כריתה"), count="1", species=visual("אורן"),
                    street=visual("הולנד"), appeal="01/09/2026", request_no="2177",
                )
            )
        )
        self.assertEqual(Source.HAIFA_PDF, records[0].source)
        self.assertEqual("haifa_pdf:2177", records[0].key)


if __name__ == "__main__":
    unittest.main()
