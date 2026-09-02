"""Cadastral block/parcel placement.

The only way to locate the many Yeela licences that carry no street at all.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import ROOT
from tree_watch import config
from tree_watch.geo import GeoIndex, parse_id_list
from tree_watch.models import Licence, Source

INDEX = ROOT / "data" / "street_index.csv"
ALIASES = ROOT / "data" / "neighborhood_aliases.csv"
BLOCKS = ROOT / "data" / "block_index.csv"


class TestParseIdList(unittest.TestCase):
    """Every one of these shapes is real, taken from production logs."""

    def test_single_value(self):
        self.assertEqual(["11697"], parse_id_list("11697"))

    def test_comma_separated(self):
        self.assertEqual(["10841", "10840"], parse_id_list("10841,10840"))
        self.assertEqual(["1", "2", "7", "72"], parse_id_list("1,2,7,72"))
        self.assertEqual(["86", "114"], parse_id_list("86,114"))

    def test_hyphen_is_an_inclusive_range(self):
        self.assertEqual(["10", "11", "12"], parse_id_list("10-12"))
        self.assertEqual(["337", "338"], parse_id_list("337-338"))

    def test_ranges_and_singles_mixed(self):
        self.assertEqual(["1", "2", "3", "4", "74"], parse_id_list("1-4,74"))

    def test_order_is_preserved_and_duplicates_dropped(self):
        self.assertEqual(["5", "3", "9"], parse_id_list("5,3,5,9,3"))

    def test_blank_and_none(self):
        for value in ("", "   ", None):
            self.assertEqual([], parse_id_list(value))

    def test_leading_zeros_are_stripped_so_keys_meet(self):
        self.assertEqual(["7"], parse_id_list("007"))

    def test_a_runaway_range_is_refused_not_expanded(self):
        result = parse_id_list("1-999999")
        self.assertEqual(["1"], result)

    def test_the_limit_is_respected_exactly(self):
        self.assertEqual(config.MAX_PARCEL_RANGE, len(parse_id_list("1-200")))
        self.assertEqual(1, len(parse_id_list("1-201")))

    def test_reversed_range_still_works(self):
        self.assertEqual(["10", "11", "12"], parse_id_list("12-10"))


class BlockHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="treewatch-block-"))
        cls.landmarks = cls.dir / "landmarks.csv"
        cls.landmarks.write_text("pattern,neighborhood,display_name\n", encoding="utf-8")
        cls.geo = GeoIndex(
            INDEX, ALIASES, cls.landmarks, cls.dir / "unmatched.csv", BLOCKS
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def place(self, block, parcel="", street=""):
        return self.geo.lookup(street, None, block=block, parcel=parcel)


class TestIndexLoaded(BlockHarness):
    def test_the_index_is_loaded(self):
        # FIXTURE-LOCKED: 520 blocks / 26,165 parcel rows in the build of
        # 2026-09-01. Rebuilding data/block_index.csv changes these.
        self.assertEqual(520, len(self.geo.block_majority))
        self.assertGreater(len(self.geo.block_parcels), 25000)

    def test_weak_blocks_are_a_real_population_not_an_edge_case(self):
        weak = [
            block
            for block, (_n, support) in self.geo.block_majority.items()
            if support < config.BLOCK_SUPPORT_THRESHOLD
        ]
        self.assertEqual(116, len(weak))


class TestRealYeelaFormats(BlockHarness):
    """The five block/parcel pairs seen in the logs, end to end."""

    PAIRS = [
        ("10841,10840", "1,2,7,72"),
        ("10841,11115", "1-4,74"),
        ("11697", "10-12"),
        ("12255", "337-338"),
        ("10903", "86,114"),
    ]

    def test_every_real_pair_resolves_to_a_neighbourhood(self):
        for block, parcel in self.PAIRS:
            with self.subTest(block=block, parcel=parcel):
                match = self.place(block, parcel)
                self.assertTrue(
                    match.known, f"{block}/{parcel} did not resolve: {match}"
                )

    def test_11697_parcels_10_12_is_an_exact_parcel_hit(self):
        match = self.place("11697", "10-12")
        self.assertEqual("block_parcel", match.method)
        self.assertAlmostEqual(0.95, match.confidence)
        self.assertFalse(match.approximate)

    def test_12255_parcels_337_338(self):
        self.assertEqual("block_parcel", self.place("12255", "337-338").method)

    def test_10903_parcels_86_114(self):
        self.assertEqual("block_parcel", self.place("10903", "86,114").method)

    def test_a_range_and_a_single_together(self):
        self.assertTrue(self.place("10841,11115", "1-4,74").known)


class TestSupportThreshold(BlockHarness):
    def test_a_weak_block_is_flagged_approximate(self):
        # Block 11190's dominant neighbourhood covers only 34% of its area.
        match = self.place("11190")
        self.assertEqual("block_majority_weak", match.method)
        self.assertTrue(match.approximate)
        self.assertLess(match.confidence, config.BLOCK_SUPPORT_THRESHOLD)
        self.assertAlmostEqual(0.343, match.confidence, places=3)

    def test_a_weak_block_still_names_a_neighbourhood(self):
        self.assertTrue(self.place("11190").known)

    def test_a_strong_block_is_not_flagged(self):
        strong = next(
            block
            for block, (_n, support) in self.geo.block_majority.items()
            if support >= config.BLOCK_SUPPORT_THRESHOLD
        )
        match = self.place(strong)
        self.assertEqual("block_majority", match.method)
        self.assertFalse(match.approximate)
        self.assertAlmostEqual(0.8, match.confidence)

    def test_an_exact_parcel_beats_the_weak_block_majority(self):
        weak = self.place("11190")
        exact = self.place("11190", "12")
        self.assertEqual("block_majority_weak", weak.method)
        self.assertEqual("block_parcel", exact.method)
        self.assertGreater(exact.confidence, weak.confidence)


class TestDivergentCombinations(BlockHarness):
    def test_a_multi_block_split_across_neighbourhoods_is_not_guessed(self):
        match = self.place("10841,10840", "1,2,7,72")
        self.assertNotEqual("block_parcel", match.method)
        self.assertTrue(
            match.method.startswith("block_majority"),
            f"expected a majority fallback, got {match.method}",
        )
        self.assertTrue(match.approximate, "a divergent match must be flagged")

    def test_the_divergence_is_logged(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("tree_watch.geo")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            self.place("10841,10840", "1,2,7,72")
        finally:
            logger.removeHandler(handler)
        self.assertIn("spans", "\n".join(r.getMessage() for r in records))

    def test_agreeing_parcels_are_accepted(self):
        match = self.place("11697", "10-12")
        self.assertEqual("block_parcel", match.method)
        self.assertFalse(match.approximate)


class TestChainPrecedence(BlockHarness):
    def test_a_resolved_street_beats_the_block(self):
        # A street with a house number is more precise than a block, and a block
        # can span several neighbourhoods.
        match = self.geo.lookup("אבא חושי", "14", block="11190", parcel="12")
        self.assertEqual("רמת בגין", match.neighborhood)
        self.assertTrue(match.method.startswith("house_range"))

    def test_the_block_is_used_when_the_street_does_not_resolve(self):
        match = self.geo.lookup("זזזזזז חחחחחח", None, block="11697", parcel="10")
        self.assertEqual("block_parcel", match.method)

    def test_no_street_and_no_block_is_no_address(self):
        match = self.geo.lookup("", None)
        self.assertEqual(config.NO_ADDRESS, match.neighborhood)
        self.assertEqual("no_address", match.method)
        self.assertFalse(match.known)
        self.assertFalse(match.has_address)

    def test_an_unknown_block_with_no_street_is_no_address(self):
        match = self.geo.lookup("", None, block="999999", parcel="1")
        self.assertEqual(config.NO_ADDRESS, match.neighborhood)

    def test_an_unplaceable_street_is_unknown_not_no_address(self):
        # Two different failures: an index gap versus a source-data gap.
        match = self.geo.lookup("זזזזזז חחחחחח טטטטטט", None)
        self.assertEqual(config.UNKNOWN_NEIGHBORHOOD, match.neighborhood)
        self.assertTrue(match.has_address)


class TestMessageFormatting(unittest.TestCase):
    def licence(self, **kwargs):
        base = dict(
            source=Source.YEELA,
            source_id="1",
            fell_count=12,
            species={"ברוש מצוי": 8, "אורן ירושלים": 4},
            applicant="יפה נוף",
            block="11539",
            parcel="2038",
        )
        base.update(kwargs)
        return Licence(**base)

    def test_a_block_only_licence_prints_its_block(self):
        from tree_watch.main import format_licence_line

        line = format_licence_line(self.licence())
        self.assertIn("גוש 11539 חלקה 2038", line)
        self.assertIn("12 עצים", line)
        self.assertIn("ברוש מצוי x8", line)
        self.assertIn("אורן ירושלים x4", line)
        self.assertIn("מבקש: יפה נוף", line)

    def test_nothing_is_invented_about_what_the_project_is(self):
        from tree_watch.main import format_licence_line

        line = format_licence_line(self.licence())
        # There is no such field in the data, and the applicants range from a
        # university to a burial society to private individuals.
        for invented in ("פרויקט", "תשתית", "בנייה ופיתוח"):
            self.assertNotIn(invented, line)

    def test_a_street_licence_does_not_show_the_applicant_or_block(self):
        from tree_watch.main import format_licence_line

        line = format_licence_line(self.licence(street="הולנד", house="36"))
        self.assertIn("הולנד 36", line)
        self.assertNotIn("מבקש", line)
        self.assertNotIn("גוש", line)

    def test_a_block_with_no_parcel(self):
        self.assertEqual("גוש 11539", self.licence(parcel="").block_text)

    def test_location_prefers_the_street(self):
        record = self.licence(street="הולנד", house="36")
        self.assertEqual("הולנד 36", record.location_text)

    def test_no_address_is_a_different_heading_from_unknown_neighbourhood(self):
        from tree_watch.main import group_by_neighborhood

        no_address = self.licence(source_id="a")
        no_address.neighborhood = config.NO_ADDRESS
        unplaceable = self.licence(source_id="b", street="זזזזזז")
        unplaceable.neighborhood = config.UNKNOWN_NEIGHBORHOOD

        grouped = group_by_neighborhood([no_address, unplaceable])
        self.assertEqual({config.NO_ADDRESS, config.UNKNOWN_NEIGHBORHOOD}, set(grouped))
        self.assertNotEqual(config.NO_ADDRESS, config.UNKNOWN_NEIGHBORHOOD)


if __name__ == "__main__":
    unittest.main()
