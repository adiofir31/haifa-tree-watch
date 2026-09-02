"""Yeela source — bugs 4, 5, 6 and the opposite BiDi rule."""

from __future__ import annotations

import unittest
from datetime import date

from tests import yeela_envelope, yeela_rows
from tree_watch import config
from tree_watch.models import Source, Stage
from tree_watch.sources import yeela as yeela_source


class TestBiDi(unittest.TestCase):
    """This source is already logical; get_display would REVERSE it."""

    def test_streets_are_already_logical(self):
        streets = [
            (r.get("expandRows") or [{}])[0].get("street") or "" for r in yeela_rows()
        ]
        joined = " ".join(streets)
        # "מוריה" reads correctly here; a visually-ordered feed would show "הירומ".
        self.assertIn("מוריה", joined)
        self.assertNotIn("הירומ", joined)

    def test_parser_does_not_reverse(self):
        records = yeela_source.parse_rows(
            [_row(requestId=1, licenseId=100, street="יותם 11", treeName="אורן", unproot=1)]
        )
        self.assertEqual("יותם", records[0].street)
        self.assertEqual("11", records[0].house)


def _row(**kwargs):
    row = {
        "requestId": None,
        "licenseId": None,
        "licenseStatusDesc": "מושהה ופתוח להגשת השגה",
        "approvedDate": "2026-08-10T10:30:05",
        "appealLastDate": "2026-08-24T10:30:05",
        "treeName": None,
        "unproot": 0,
        "copying": 0,
        "conservation": 0,
        "expandRows": [{"street": "", "customerName": "", "block": "", "parcel": ""}],
    }
    street = kwargs.pop("street", None)
    if street is not None:
        row["expandRows"] = [{"street": street, "customerName": "", "block": "", "parcel": ""}]
    row.update(kwargs)
    return row


class TestNullLicenceId(unittest.TestCase):
    """Bug 4: str(item.get('licenseId','')) yielded the string 'None'."""

    def test_the_old_expression_really_produces_None(self):
        item = {"licenseId": None}
        self.assertEqual("None", str(item.get("licenseId", "")))

    def test_state_key_is_the_request_id(self):
        records = yeela_source.parse_rows(
            [_row(requestId=16253, licenseId=None, unproot=14, street="שדרות מוריה 1")]
        )
        self.assertEqual("16253", records[0].source_id)
        self.assertEqual("yeela:16253", records[0].key)
        self.assertIsNone(records[0].licence_id)

    def test_pending_requests_do_not_collapse_into_one_key(self):
        records = yeela_source.parse_rows(
            [
                _row(requestId=1, licenseId=None, unproot=1),
                _row(requestId=2, licenseId=None, unproot=1),
                _row(requestId=3, licenseId=None, unproot=1),
            ]
        )
        self.assertEqual(3, len({r.key for r in records}))

    def test_no_key_ever_contains_the_string_None(self):
        for record in yeela_source.parse_rows(yeela_rows()):
            self.assertNotIn("None", record.key)
            self.assertNotEqual("", record.source_id)

    def test_row_without_request_id_is_skipped_not_written(self):
        records = yeela_source.parse_rows([_row(requestId=None, licenseId=999, unproot=1)])
        self.assertEqual([], records)

    def test_request_id_survives_the_status_transition(self):
        before = yeela_source.parse_rows([_row(requestId=16253, licenseId=None, unproot=14)])
        after = yeela_source.parse_rows(
            [_row(requestId=16253, licenseId=1007665, unproot=14, treeName="אורן")]
        )
        self.assertEqual(before[0].key, after[0].key)
        self.assertEqual(Stage.PENDING, before[0].stage)
        self.assertEqual(Stage.LICENSED, after[0].stage)


class TestPagination(unittest.TestCase):
    """Bug 5: the bot read page 1 of 18."""

    def test_fixture_reports_eighteen_pages(self):
        # FIXTURE-LOCKED: 18 pages / 1738 rows as of the 2026-08-10 capture in
        # tests/fixtures/yeela_raw.json. The point of the test is that it is
        # more than one page; update both numbers if the fixture is refreshed.
        pagination = yeela_envelope()[0]["pagination"]
        self.assertEqual(18, pagination["totalPages"])
        self.assertEqual(1738, pagination["totalCount"])

    def test_fetch_all_walks_every_page(self):
        requested = []

        def fake_fetch(page, city_id=None):
            requested.append(page)
            return {
                "pagination": {"totalPages": 18, "totalCount": 1738},
                "result": [_row(requestId=page * 1000 + i, licenseId=None) for i in range(5)],
            }

        original_fetch = yeela_source.fetch_page
        original_delay = config.YEELA_PAGE_DELAY
        yeela_source.fetch_page = fake_fetch
        config.YEELA_PAGE_DELAY = 0
        try:
            rows = yeela_source.fetch_all()
        finally:
            yeela_source.fetch_page = original_fetch
            config.YEELA_PAGE_DELAY = original_delay

        self.assertEqual(list(range(1, 19)), requested)
        self.assertEqual(90, len(rows))

    def test_page_size_of_one_hundred_would_have_missed_most_rows(self):
        self.assertGreater(len(yeela_rows()), 1000)


class TestActionCounters(unittest.TestCase):
    """Bug 6: unproot / copying / conservation are separate and not exclusive."""

    def test_preserved_species_is_not_listed(self):
        records = yeela_source.parse_rows(
            [
                _row(requestId=7, licenseId=70, treeName="אורן", unproot=3),
                _row(requestId=7, licenseId=70, treeName="ברוש", conservation=5),
            ]
        )
        record = records[0]
        self.assertEqual(3, record.fell_count)
        self.assertEqual(5, record.preserve_count)
        self.assertEqual({"אורן": 3}, record.species)
        self.assertNotIn("ברוש", record.species_text)

    def test_relocated_species_is_not_listed(self):
        records = yeela_source.parse_rows(
            [_row(requestId=8, licenseId=80, treeName="דקל", copying=2)]
        )
        self.assertEqual(0, records[0].fell_count)
        self.assertEqual(2, records[0].relocate_count)
        self.assertEqual({}, records[0].species)

    def test_row_can_set_two_counters_at_once(self):
        records = yeela_source.parse_rows(
            [_row(requestId=9, licenseId=90, treeName="אורן", unproot=2, conservation=4)]
        )
        self.assertEqual(2, records[0].fell_count)
        self.assertEqual(4, records[0].preserve_count)
        self.assertEqual({"אורן": 2}, records[0].species)

    def test_relocation_only_requests_are_not_reported_as_felling(self):
        records = yeela_source.parse_rows(
            [_row(requestId=10, licenseId=100, treeName="דקל", copying=2)]
        )
        self.assertEqual([], yeela_source.relevant(records, today=date(2026, 8, 20)))

    def test_real_data_has_rows_with_multiple_counters(self):
        # FIXTURE-LOCKED: 294 rows in tests/fixtures/yeela_full.json set both
        # unproot and conservation (captured 2026-08-11). Re-pulling the API
        # changes this; update it from the new pull.
        both = [
            r
            for r in yeela_rows()
            if (r.get("unproot") or 0) > 0 and (r.get("conservation") or 0) > 0
        ]
        self.assertEqual(294, len(both))


class TestPendingSplit(unittest.TestCase):
    def test_real_pending_counts(self):
        # FIXTURE-LOCKED: 178 distinct pending requests / 827 trees flagged for
        # felling, in the 2026-08-11 pull. Both move whenever the fixture is
        # refreshed — read the new numbers off the fresh pull.
        records = yeela_source.parse_rows(yeela_rows())
        waiting = yeela_source.pending(records)
        self.assertEqual(178, len({r.source_id for r in yeela_source.parse_rows(
            [r for r in yeela_rows() if r.get("licenseId") is None])}))
        self.assertEqual(827, sum(r.fell_count for r in waiting))

    def test_pending_cannot_appeal(self):
        records = yeela_source.parse_rows([_row(requestId=11, licenseId=None, unproot=3)])
        self.assertFalse(records[0].can_appeal)


class TestKeyNamespacing(unittest.TestCase):
    """Yeela requestIds and municipal request numbers overlap numerically."""

    def test_same_number_different_source_is_a_different_key(self):
        from tree_watch.models import Licence

        y = Licence(source=Source.YEELA, source_id="2177")
        h = Licence(source=Source.HAIFA_PDF, source_id="2177")
        self.assertNotEqual(y.key, h.key)

    def test_the_overlap_is_real_in_the_fixture(self):
        # FIXTURE-LOCKED: these nine numbers are both live Yeela requestIds in
        # the 2026-08-11 pull and municipal request numbers in sent_licenses.txt.
        # A refreshed fixture will have a different overlap set — recompute it;
        # the test that must keep passing is that namespacing separates them.
        request_ids = {str(r["requestId"]) for r in yeela_rows() if r.get("requestId")}
        legacy_municipal = {"2177", "2180", "2181", "2195", "2201", "2214", "2270", "2280", "2285"}
        self.assertEqual(legacy_municipal, request_ids & legacy_municipal)


if __name__ == "__main__":
    unittest.main()
