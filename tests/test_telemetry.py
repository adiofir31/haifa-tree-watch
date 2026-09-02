"""Per-source funnel counters.

"nothing new" on its own cannot tell a source that returned nothing from a
filter that discarded everything, and that distinction is the whole point of the
dry-run comparison against the legacy bot.
"""

from __future__ import annotations

import logging
import unittest

from tests import pdf_pages, yeela_rows
from tree_watch.models import SourceStats
from tree_watch.sources import haifa_pdf
from tree_watch.sources import yeela as yeela_source


class TestFunnel(unittest.TestCase):
    def test_stage_order_is_the_pipeline_order(self):
        labels = [label for label, _ in SourceStats(source="x").stages]
        self.assertEqual(
            [
                "pages fetched",
                "raw rows",
                "requests after aggregation",
                "still open (date filter)",
                "new against state",
            ],
            labels,
        )

    def test_no_zero_when_everything_survives(self):
        stats = SourceStats(
            source="x", pages=1, raw_rows=10, requests=5, passed_date_filter=3, new=2
        )
        self.assertIsNone(stats.first_zero())

    def test_zero_at_the_source_is_reported_as_such(self):
        stats = SourceStats(source="x", pages=0)
        label, previous_label, previous_value = stats.first_zero()
        self.assertEqual("pages fetched", label)
        self.assertEqual("start", previous_label)

    def test_zero_at_the_date_filter_names_what_fed_it(self):
        stats = SourceStats(
            source="x", pages=432, raw_rows=3479, requests=2286, passed_date_filter=0
        )
        label, previous_label, previous_value = stats.first_zero()
        self.assertEqual("still open (date filter)", label)
        self.assertEqual("requests after aggregation", previous_label)
        self.assertEqual(2286, previous_value)

    def test_zero_at_the_state_filter_is_distinguishable(self):
        stats = SourceStats(
            source="x", pages=432, raw_rows=3479, requests=2286,
            passed_date_filter=40, new=0,
        )
        label, previous_label, previous_value = stats.first_zero()
        self.assertEqual("new against state", label)
        self.assertEqual(40, previous_value)

    def test_summary_lists_every_stage(self):
        summary = SourceStats(source="x", pages=1, raw_rows=2).summary()
        for fragment in ("pages fetched: 1", "raw rows: 2", "new against state: 0"):
            self.assertIn(fragment, summary)


class TestSourcesReportTheirCounters(unittest.TestCase):
    """The counts the sources emit must match what the parsers actually did."""

    def test_pdf_counts_line_up_with_the_fixture(self):
        tables = pdf_pages()
        stats = SourceStats(
            source="haifa_pdf",
            pages=len(tables),
            raw_rows=max(sum(len(t) for t in tables) - 1, 0),
        )
        records = haifa_pdf.parse_tables(tables)
        stats.requests = len(records)
        # FIXTURE-LOCKED: 432 pages / 3479 data rows / 2286 requests in the
        # 2026-08-10 capture. Refreshing diag_pdf_raw.json changes all three.
        self.assertEqual(432, stats.pages)
        self.assertEqual(3479, stats.raw_rows)
        self.assertEqual(2286, stats.requests)
        self.assertIsNone(
            SourceStats(
                source="haifa_pdf", pages=stats.pages, raw_rows=stats.raw_rows,
                requests=stats.requests, passed_date_filter=1, new=1,
            ).first_zero()
        )

    def test_raw_rows_excludes_the_single_header_row(self):
        tables = pdf_pages()
        total_cells = sum(len(t) for t in tables)
        self.assertEqual(3480, total_cells, "3479 data rows plus one header")

    def test_yeela_counts_line_up_with_the_fixture(self):
        rows = yeela_rows()
        records = yeela_source.parse_rows(rows)
        # FIXTURE-LOCKED: 1745 rows collapsing to 476 requests (2026-08-11).
        self.assertEqual(1745, len(rows))
        self.assertEqual(476, len(records))

    def test_collect_reports_pages_actually_read(self):
        pages_requested = []

        def fake_fetch(page, city_id=None):
            pages_requested.append(page)
            return {
                "pagination": {"totalPages": 3, "totalCount": 12},
                "result": [
                    {
                        "requestId": page * 10 + i,
                        "licenseId": 1000000 + page * 10 + i,
                        "unproot": 1,
                        "treeName": "אורן",
                        "approvedDate": "2026-08-10T00:00:00",
                        "appealLastDate": "2026-12-31T00:00:00",
                        "expandRows": [{"street": "הולנד 36"}],
                    }
                    for i in range(4)
                ],
            }

        original = yeela_source.fetch_page
        yeela_source.fetch_page = fake_fetch
        try:
            open_now, waiting, stats = yeela_source.collect()
        finally:
            yeela_source.fetch_page = original

        self.assertEqual([1, 2, 3], pages_requested)
        self.assertEqual(3, stats.pages)
        self.assertEqual(12, stats.raw_rows)
        self.assertEqual(12, stats.requests)
        self.assertEqual(12, stats.passed_date_filter)
        self.assertEqual(0, stats.pending_requests)
        # `new` is filled in by main.run against the state, not by collect().
        self.assertEqual(0, stats.new)


class TestFunnelLogging(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.handler = logging.Handler()
        self.handler.emit = self.records.append
        logging.getLogger("tree_watch.main").addHandler(self.handler)
        logging.getLogger("tree_watch.main").setLevel(logging.INFO)

    def tearDown(self):
        logging.getLogger("tree_watch.main").removeHandler(self.handler)

    def messages(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)

    def test_each_source_gets_its_own_block(self):
        from tree_watch.main import log_source_funnel

        log_source_funnel(
            {
                "haifa_pdf": SourceStats(
                    source="haifa_pdf", pages=432, raw_rows=3479, requests=2286,
                    passed_date_filter=40, new=3, skipped_already_sent=37,
                ),
                "yeela": SourceStats(
                    source="yeela", pages=18, raw_rows=1745, requests=476,
                    passed_date_filter=0,
                ),
            }
        )
        text = self.messages()
        self.assertIn("haifa_pdf", text)
        self.assertIn("yeela", text)
        self.assertIn("pages fetched: 432", text)
        self.assertIn("skipped as already sent: 37", text)

    def test_the_zero_stage_is_named(self):
        from tree_watch.main import log_source_funnel

        log_source_funnel(
            {
                "yeela": SourceStats(
                    source="yeela", pages=18, raw_rows=1745, requests=476,
                    passed_date_filter=0,
                )
            }
        )
        text = self.messages()
        self.assertIn("dropped to zero at 'still open (date filter)'", text)
        self.assertIn("requests after aggregation was 476", text)

    def test_a_failed_source_says_so_instead_of_reporting_zeros(self):
        from tree_watch.main import log_source_funnel

        log_source_funnel({"haifa_pdf": SourceStats(source="haifa_pdf", failed=True)})
        text = self.messages()
        self.assertIn("FAILED", text)
        self.assertNotIn("dropped to zero", text)

    def test_combined_totals_add_up(self):
        from tree_watch.main import log_combined

        log_combined(
            {
                "haifa_pdf": SourceStats(
                    source="haifa_pdf", pages=432, raw_rows=3479, requests=2286,
                    passed_date_filter=40, new=3, skipped_already_sent=37,
                ),
                "yeela": SourceStats(
                    source="yeela", pages=18, raw_rows=1745, requests=476,
                    passed_date_filter=12, new=2, skipped_already_sent=10,
                ),
            },
            fresh=[1, 2, 3, 4, 5], updates=[], new_pending=[], backdated=[1],
        )
        text = self.messages()
        self.assertIn("TOTAL (2 source(s))", text)
        self.assertIn("pages fetched: 450", text)
        self.assertIn("raw rows: 5224", text)
        self.assertIn("still open (date filter): 52", text)
        self.assertIn("new: 5", text)
        self.assertIn("back-dated: 1", text)

    def test_failed_sources_are_excluded_from_totals(self):
        from tree_watch.main import log_combined

        log_combined(
            {
                "haifa_pdf": SourceStats(source="haifa_pdf", failed=True),
                "yeela": SourceStats(
                    source="yeela", pages=18, raw_rows=1745, requests=476,
                    passed_date_filter=12, new=2,
                ),
            },
            fresh=[1, 2], updates=[], new_pending=[], backdated=[],
        )
        self.assertIn("TOTAL (1 source(s))", self.messages())


if __name__ == "__main__":
    unittest.main()
