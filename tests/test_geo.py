"""The street -> neighbourhood lookup chain, against the real street index."""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests import ROOT
from tree_watch import config
from tree_watch.geo import GeoIndex

INDEX = ROOT / "data" / "street_index.csv"
ALIASES = ROOT / "data" / "neighborhood_aliases.csv"


class GeoHarness(unittest.TestCase):
    """One shared index for the whole class — loading it is the slow part."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="treewatch-geo-"))
        cls.landmarks = cls.dir / "landmarks.csv"
        cls.landmarks.write_text(
            "pattern,neighborhood,display_name\n"
            "בית חולים רמבם,בת גלים,מרכז רפואי רמב\"ם\n"
            "גן הבהאים,הדר עליון,הגנים הבהאיים\n",
            encoding="utf-8",
        )
        cls.unmatched = cls.dir / "unmatched.csv"
        cls.geo = GeoIndex(INDEX, ALIASES, cls.landmarks, cls.unmatched)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)


class TestLoading(GeoHarness):
    def test_index_loaded(self):
        self.assertGreater(len(self.geo.streets), 1500)
        self.assertIn("אבא חושי", self.geo.streets)

    def test_no_geometry_library_is_imported(self):
        # geo.py must answer from a dict; the spatial work happened offline.
        for module in ("geopandas", "shapely", "pandas", "fiona", "pyproj"):
            self.assertNotIn(module, sys.modules, f"geo.py must not pull in {module}")

    def test_aliases_are_collapsed(self):
        self.assertEqual("הדר מרכז", self.geo.canonical("הדר"))
        self.assertEqual("קריית אליעזר", self.geo.canonical("קרית אליעזר"))
        self.assertEqual(
            "עיר תחתית מזרח, ואדי סאליב", self.geo.canonical("ואדי סאליב")
        )

    def test_road_corridors_are_dropped_not_reported_as_places(self):
        self.assertIn("ציר ורדיה - דרך רופין", self.geo.dropped)
        for entries in self.geo.streets.values():
            for row in entries:
                self.assertNotIn(row.neighborhood, self.geo.dropped)

    def test_landmarks_loaded(self):
        self.assertEqual(2, len(self.geo.landmarks))


class TestHouseRangeTier(GeoHarness):
    """Tier 1 — the reason the index carries house ranges at all.

    אבא חושי crosses four neighbourhoods; a street-level answer is wrong for
    three quarters of it.
    """

    def test_bottom_of_the_street(self):
        match = self.geo.lookup("אבא חושי", "14")
        self.assertEqual("רמת בגין", match.neighborhood)
        self.assertTrue(match.method.startswith("house_range"))
        self.assertFalse(match.approximate)

    def test_middle_of_the_street(self):
        self.assertEqual("רמת גולדה", self.geo.lookup("אבא חושי", "50").neighborhood)
        self.assertEqual("סביוני הכרמל", self.geo.lookup("אבא חושי", "100").neighborhood)

    def test_open_ended_top_range_is_a_range_not_a_street_level_row(self):
        # 150-99999 is priority 1 and a real house range. Treating any row with
        # house_to = 99999 as street-level would resolve 200 to רמת בגין.
        match = self.geo.lookup("אבא חושי", "200")
        self.assertEqual("הוד הכרמל", match.neighborhood)
        self.assertTrue(match.method.startswith("house_range"))

    def test_house_number_is_parsed_from_messy_forms(self):
        for house in ("14", "14/16/18", "14א", "14-18"):
            with self.subTest(house=house):
                self.assertEqual("רמת בגין", self.geo.lookup("אבא חושי", house).neighborhood)

    def test_house_number_inside_the_street_string(self):
        self.assertEqual("רמת בגין", self.geo.lookup("אבא חושי 14").neighborhood)


class TestStreetLevelTier(GeoHarness):
    """Tier 2 — no house number, so answer for the street as a whole."""

    def test_falls_back_to_the_street_level_row(self):
        match = self.geo.lookup("אבא חושי")
        self.assertEqual("הוד הכרמל", match.neighborhood)
        self.assertTrue(match.method.startswith("street_level"))

    def test_not_the_narrowest_range(self):
        self.assertNotEqual(
            "רמת בגין",
            self.geo.lookup("אבא חושי").neighborhood,
            "an unknown house number must not resolve to one specific stretch",
        )

    def test_street_prefix_is_stripped(self):
        for name in ("אבא חושי", "רחוב אבא חושי", "שדרות אבא חושי"):
            with self.subTest(name=name):
                self.assertEqual("הוד הכרמל", self.geo.lookup(name).neighborhood)


class TestTokenSubsetTier(GeoHarness):
    """Tier 2b — a surname alone is a normal way to name a street in Hebrew."""

    def test_surname_only_resolves(self):
        # "כאהן 4" against the indexed "כאהן יעקב".
        match = self.geo.lookup("כאהן", "4")
        self.assertEqual("קריית חיים מזרחית", match.neighborhood)
        self.assertTrue(
            match.method.startswith("token_subset"), f"got {match.method}"
        )

    def test_house_number_inside_the_string_also_works(self):
        self.assertEqual("קריית חיים מזרחית", self.geo.lookup("כאהן 4").neighborhood)

    def test_confidence_is_below_an_exact_match(self):
        subset = self.geo.lookup("כאהן", "4")
        exact = self.geo.lookup("אבא חושי", "14")
        self.assertLess(subset.confidence, exact.confidence)

    def test_an_exact_match_still_wins(self):
        # "הרצל" is contained in five indexed streets but is itself a key, so
        # the exact tier must answer first and this tier never runs.
        match = self.geo.lookup("הרצל")
        self.assertTrue(match.method.startswith(("house_range", "street_level")))
        self.assertFalse(match.method.startswith("token_subset"))

    def test_ambiguous_containment_is_not_guessed(self):
        # "יעקב" is contained in 32 indexed streets and is NOT itself a key, so
        # this really does reach the containment tier and really must decline.
        # ("דוד" would not test anything — it is an exact key, so tier 1 answers.)
        self.assertNotIn("יעקב", self.geo.streets)
        contained = [
            key for key, tokens in self.geo._key_tokens if {"יעקב"} <= tokens
        ]
        self.assertGreater(len(contained), 5, "should be genuinely ambiguous")

        match = self.geo.lookup("יעקב")
        self.assertFalse(
            match.method.startswith("token_subset"),
            f"ambiguous query must not resolve by containment, got {match.method}",
        )
        self.assertEqual(config.UNKNOWN_NEIGHBORHOOD, match.neighborhood)

    def test_a_query_that_is_itself_a_key_never_reaches_this_tier(self):
        self.assertIn("דוד", self.geo.streets)
        match = self.geo.lookup("דוד")
        self.assertTrue(match.method.startswith(("house_range", "street_level")))

    def test_the_tier_respects_house_ranges_of_the_matched_street(self):
        match = self.geo.lookup("כאהן", "4")
        self.assertTrue(match.known)
        self.assertGreater(match.confidence, 0.0)


class TestFuzzyEngine(GeoHarness):
    def test_engine_is_reported(self):
        from tree_watch.geo import fuzzy_engine

        self.assertIn(fuzzy_engine(), {"rapidfuzz", "difflib"})

    def test_engine_is_logged_at_load(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("tree_watch.geo")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            GeoIndex(INDEX, ALIASES, self.landmarks, self.dir / "u2.csv")
        finally:
            logger.removeHandler(handler)
        text = "\n".join(r.getMessage() for r in records)
        self.assertIn("fuzzy engine =", text)


class TestLandmarkTier(GeoHarness):
    """Tier 3 — matched against the whole licence text, not just the street."""

    def test_landmark_in_the_full_text(self):
        match = self.geo.lookup(
            "לא ידוע", "0", full_text="כריתת עצים בחצר בית חולים רמבם"
        )
        self.assertEqual("בת גלים", match.neighborhood)
        self.assertEqual("landmark", match.method)

    def test_landmark_not_matched_when_absent(self):
        match = self.geo.lookup("זזזזזז", "0", full_text="משהו אחר לגמרי")
        self.assertNotEqual("landmark", match.method)

    def test_a_real_street_still_wins_over_a_landmark(self):
        match = self.geo.lookup("אבא חושי", "14", full_text="ליד גן הבהאים")
        self.assertEqual("רמת בגין", match.neighborhood)


class TestFuzzyTier(GeoHarness):
    """Tier 4 — approximate, and the message must say so."""

    def test_close_misspelling_is_matched(self):
        match = self.geo.lookup("אבא חושיי")
        self.assertTrue(match.approximate, f"expected a fuzzy match, got {match.method}")
        self.assertEqual("הוד הכרמל", match.neighborhood)
        self.assertGreaterEqual(match.confidence * 100, config.FUZZY_THRESHOLD)

    def test_nonsense_is_not_forced_into_a_match(self):
        match = self.geo.lookup("זזזזזז חחחחחח טטטטטט")
        self.assertEqual(config.UNKNOWN_NEIGHBORHOOD, match.neighborhood)
        self.assertEqual("unknown", match.method)


class TestUnknownTier(GeoHarness):
    """Tier 5 — give up, but leave a trail so landmarks can be extended."""

    def setUp(self):
        if self.unmatched.exists():
            self.unmatched.unlink()
        self.geo._unmatched_seen.clear()

    def test_unknown_match_is_flagged(self):
        match = self.geo.lookup("זזזזזז חחחחחח טטטטטט")
        self.assertFalse(match.known)
        self.assertEqual(0.0, match.confidence)

    def test_miss_is_appended_with_a_header(self):
        self.geo.record_unmatched("זזזזזז", "5", "כריתה ברחוב זזזזזז 5")
        text = self.unmatched.read_text(encoding="utf-8-sig")
        self.assertIn("first_seen,street,house,full_text", text)
        self.assertIn("זזזזזז", text)

    def test_the_same_miss_is_only_recorded_once_per_run(self):
        for _ in range(4):
            self.geo.record_unmatched("זזזזזז", "5", "כריתה")
        lines = self.unmatched.read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertEqual(2, len(lines), "header plus one row")

    def test_misses_are_not_re_appended_on_the_next_run(self):
        self.geo.record_unmatched("זזזזזז", "5", "כריתה")
        fresh = GeoIndex(INDEX, ALIASES, self.landmarks, self.unmatched)
        fresh.record_unmatched("זזזזזז", "5", "כריתה")
        lines = self.unmatched.read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertEqual(2, len(lines), "the file must not grow every day")

    def test_a_blank_address_is_not_recorded(self):
        self.geo.record_unmatched("", "", "")
        self.assertFalse(
            self.unmatched.exists(), "an empty row helps nobody curate landmarks"
        )

    def test_recording_never_raises_even_if_the_path_is_unwritable(self):
        geo = GeoIndex(INDEX, ALIASES, self.landmarks, Path("Q:/nope/unmatched.csv"))
        geo.record_unmatched("זזזזזז", "5", "כריתה")  # must not raise


class TestStreetLevelFallbackWarning(GeoHarness):
    """Some streets have no whole-street row; that guess deserves a warning.

    12 of the 1,853 keys in the current index are in this state — all of them
    streets that cross a neighbourhood boundary and were built purely from
    house ranges. Without a house number the answer is one specific stretch of
    road, which is the same weak guess the house-range fix removed elsewhere.
    """

    def setUp(self):
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        self.handler = handler
        logging.getLogger("tree_watch.geo").addHandler(handler)
        logging.getLogger("tree_watch.geo").setLevel(logging.WARNING)

    def tearDown(self):
        logging.getLogger("tree_watch.geo").removeHandler(self.handler)

    def keys_without_a_street_level_row(self):
        return [
            key
            for key, entries in self.geo.streets.items()
            if not any(row.is_street_level for row in entries)
        ]

    def test_such_streets_exist_in_the_real_index(self):
        missing = self.keys_without_a_street_level_row()
        self.assertIn("לאון בלום", missing)
        self.assertGreater(len(missing), 0)
        self.assertLess(len(missing), 50, "if this grows, fix the index builder")

    def test_the_fallback_warns(self):
        self.geo.lookup("לאון בלום")
        text = "\n".join(r.getMessage() for r in self.records)
        self.assertIn("no street-level row", text)
        self.assertIn("לאון בלום", text)

    def test_a_normal_street_does_not_warn(self):
        self.geo.lookup("אבא חושי")
        self.assertEqual([], self.records)

    def test_a_house_number_still_resolves_without_warning(self):
        match = self.geo.lookup("לאון בלום", "50")
        self.assertEqual("המיימוני", match.neighborhood)
        self.assertTrue(match.method.startswith("house_range"))
        self.assertEqual([], self.records)


class TestChainOrder(GeoHarness):
    def test_house_range_beats_street_level(self):
        with_house = self.geo.lookup("אבא חושי", "14")
        without = self.geo.lookup("אבא חושי")
        self.assertNotEqual(with_house.neighborhood, without.neighborhood)
        self.assertGreater(with_house.confidence, without.confidence)

    def test_exact_beats_fuzzy(self):
        exact = self.geo.lookup("אבא חושי")
        fuzzy = self.geo.lookup("אבא חושיי")
        self.assertFalse(exact.approximate)
        self.assertTrue(fuzzy.approximate)


if __name__ == "__main__":
    unittest.main()
