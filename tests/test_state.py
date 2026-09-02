"""State keying and the one-time migration from sent_licenses.txt.

The migration is the step with the worst failure mode in the whole project: get
it wrong and 268 old licences go out to a public channel again.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests import ROOT, yeela_rows
from tree_watch.models import Licence, Source, Stage
from tree_watch.state import State, classify_legacy_id

LEGACY = ROOT / "sent_licenses.txt"


def legacy_ids() -> list[str]:
    return [l.strip() for l in LEGACY.read_text(encoding="utf-8-sig").splitlines() if l.strip()]


class TempState(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="treewatch-"))
        self.state_path = self.dir / "state.jsonl"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestLegacyClassification(TempState):
    """7 digits is a Yeela licenceId; everything else is a municipal request."""

    def test_seven_digits_is_yeela(self):
        self.assertEqual(Source.YEELA, classify_legacy_id("1005885"))

    def test_four_digits_is_municipal(self):
        self.assertEqual(Source.HAIFA_PDF, classify_legacy_id("2293"))

    def test_none_line_is_not_classified(self):
        self.assertIsNone(classify_legacy_id("None"))
        self.assertIsNone(classify_legacy_id(""))
        self.assertIsNone(classify_legacy_id("abc"))

    def test_the_split_is_unambiguous_in_the_real_data(self):
        # Every Yeela licenceId is 7 digits (min 1000155) and no requestId is
        # (max 16253), so length separates the two spaces with no overlap.
        # FIXTURE-LOCKED: recheck these bounds if yeela_full.json is refreshed.
        licence_ids = [r["licenseId"] for r in yeela_rows() if r.get("licenseId")]
        request_ids = [r["requestId"] for r in yeela_rows() if r.get("requestId")]
        self.assertTrue(all(len(str(x)) == 7 for x in licence_ids))
        self.assertFalse(any(len(str(x)) == 7 for x in request_ids))

    def test_real_legacy_file_splits_cleanly_into_two_spaces(self):
        # sent_licenses.txt is live production state and grows every evening, so
        # the invariants are asserted rather than the totals.
        ids = legacy_ids()
        yeela = [i for i in ids if classify_legacy_id(i) is Source.YEELA]
        muni = [i for i in ids if classify_legacy_id(i) is Source.HAIFA_PDF]
        nulls = [i for i in ids if classify_legacy_id(i) is None]

        self.assertEqual(len(ids), len(yeela) + len(muni) + len(nulls))
        self.assertEqual(["None"], nulls, "exactly one poisoned line, ever")
        self.assertTrue(all(len(i) == 7 for i in yeela))
        self.assertTrue(all(len(i) == 4 for i in muni))
        self.assertGreaterEqual(len(yeela), 134)
        self.assertGreaterEqual(len(muni), 133)


class TestMigration(TempState):
    def test_every_line_is_accounted_for(self):
        ids = legacy_ids()
        expected = sum(1 for i in ids if classify_legacy_id(i) is not None)
        written = State.migrate(LEGACY, self.state_path)
        self.assertEqual(expected, written, "every line except the poisoned 'None'")

        lines = self.state_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(expected, len(lines))
        records = [json.loads(l) for l in lines]
        self.assertEqual(
            sum(1 for i in ids if classify_legacy_id(i) is Source.YEELA),
            sum(1 for r in records if r["source"] == "yeela"),
        )
        self.assertEqual(
            sum(1 for i in ids if classify_legacy_id(i) is Source.HAIFA_PDF),
            sum(1 for r in records if r["source"] == "haifa_pdf"),
        )

    def test_the_None_line_is_skipped(self):
        State.migrate(LEGACY, self.state_path)
        content = self.state_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            self.assertNotIn('"legacy_ids": ["None"]', line)

    def test_first_seen_is_null_so_nothing_looks_backdated(self):
        State.migrate(LEGACY, self.state_path)
        for line in self.state_path.read_text(encoding="utf-8").splitlines():
            self.assertIsNone(json.loads(line)["first_seen"])

    def test_every_record_carries_source_and_legacy_ids(self):
        State.migrate(LEGACY, self.state_path)
        for line in self.state_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            self.assertIn(record["source"], {"yeela", "haifa_pdf"})
            self.assertEqual(1, len(record["legacy_ids"]))

    def test_legacy_file_is_never_modified(self):
        before = LEGACY.read_bytes()
        State.migrate(LEGACY, self.state_path)
        self.assertEqual(before, LEGACY.read_bytes())

    def test_migration_is_idempotent(self):
        expected = sum(1 for i in legacy_ids() if classify_legacy_id(i) is not None)
        first = State.migrate(LEGACY, self.state_path)
        second = State.migrate(LEGACY, self.state_path)
        self.assertEqual(expected, first)
        self.assertEqual(0, second, "re-running must not duplicate anything")
        self.assertEqual(
            expected,
            len(self.state_path.read_text(encoding="utf-8").strip().splitlines()),
        )

    def test_re_running_picks_up_ids_added_since(self):
        # The legacy bot keeps appending while it is still the scheduled job, so
        # --migrate has to be re-runnable and additive.
        partial = self.dir / "partial.txt"
        ids = legacy_ids()[:10]
        partial.write_text("\n".join(ids) + "\n", encoding="utf-8")
        first = State.migrate(partial, self.state_path)

        partial.write_text("\n".join(legacy_ids()[:14]) + "\n", encoding="utf-8")
        second = State.migrate(partial, self.state_path)
        self.assertEqual(4, second, "only the newly appended ids")
        self.assertEqual(first + 4, len(State(self.state_path)))

    def test_no_licence_to_request_map_is_needed(self):
        # The whole point of keying legacy ids by source: a Yeela licence is
        # recognised by its licenceId even when its requestId is unknown to us,
        # including licences that have dropped out of the API window.
        State.migrate(LEGACY, self.state_path)
        state = State(self.state_path)
        stranger = Licence(
            source=Source.YEELA, source_id="999999", licence_id="1005885"
        )
        self.assertTrue(state.already_sent(stranger))


class TestAlreadySentAfterMigration(TempState):
    """The required end-to-end check: none of the 268 comes back as new."""

    def setUp(self):
        super().setUp()
        State.migrate(LEGACY, self.state_path)
        self.state = State(self.state_path)
        self.map = {
            str(r["licenseId"]): str(r["requestId"])
            for r in yeela_rows()
            if r.get("licenseId") and r.get("requestId")
        }

    def as_licences(self) -> list[Licence]:
        out = []
        for identifier in legacy_ids():
            source = classify_legacy_id(identifier)
            if source is Source.YEELA:
                out.append(
                    Licence(
                        source=Source.YEELA,
                        # Unknown for the 14 ids issued after the fixture was
                        # captured; deliberately synthetic here to prove the
                        # match does not depend on knowing the requestId.
                        source_id=self.map.get(identifier, f"unknown-{identifier}"),
                        licence_id=identifier,
                    )
                )
            elif source is Source.HAIFA_PDF:
                out.append(Licence(source=Source.HAIFA_PDF, source_id=identifier))
        return out

    def test_every_real_id_is_recognised_as_already_sent(self):
        licences = self.as_licences()
        expected = sum(1 for i in legacy_ids() if classify_legacy_id(i) is not None)
        self.assertEqual(expected, len(licences))
        missed = [l.key for l in licences if not self.state.already_sent(l)]
        self.assertEqual([], missed)

    def test_including_the_fourteen_absent_from_the_fixture(self):
        absent = [
            "1007735", "1007737", "1007746", "1007827", "1007829", "1007831",
            "1007832", "1007870", "1007876", "1007882", "1007901", "1007944",
            "1007950", "1007960",
        ]
        for identifier in absent:
            self.assertNotIn(identifier, self.map, "should be absent from the fixture")
            licence = Licence(
                source=Source.YEELA, source_id=f"unknown-{identifier}",
                licence_id=identifier,
            )
            self.assertTrue(self.state.already_sent(licence), identifier)

    def test_an_unseen_licence_is_still_new(self):
        self.assertFalse(
            self.state.already_sent(
                Licence(source=Source.YEELA, source_id="16191", licence_id="1099999")
            )
        )
        self.assertFalse(
            self.state.already_sent(Licence(source=Source.HAIFA_PDF, source_id="9999"))
        )


class TestNamespaceIsolation(TempState):
    """yeela:2177 and haifa_pdf:2177 must not hide each other."""

    def setUp(self):
        super().setUp()
        State.migrate(LEGACY, self.state_path)
        self.state = State(self.state_path)

    def test_municipal_2177_is_known_yeela_request_2177_is_not(self):
        municipal = Licence(source=Source.HAIFA_PDF, source_id="2177")
        yeela = Licence(source=Source.YEELA, source_id="2177", licence_id="1099998")
        self.assertTrue(self.state.already_sent(municipal), "2177 is in the legacy file")
        self.assertFalse(
            self.state.already_sent(yeela),
            "a Yeela request that happens to share the number must not be suppressed",
        )

    def test_all_nine_colliding_numbers_stay_separate(self):
        for number in ("2177", "2180", "2181", "2195", "2201", "2214", "2270", "2280", "2285"):
            with self.subTest(number=number):
                self.assertTrue(
                    self.state.already_sent(Licence(source=Source.HAIFA_PDF, source_id=number))
                )
                self.assertFalse(
                    self.state.already_sent(
                        Licence(source=Source.YEELA, source_id=number, licence_id="1099997")
                    )
                )

    def test_recording_one_source_does_not_mark_the_other(self):
        yeela = Licence(source=Source.YEELA, source_id="4242", licence_id="1007777")
        municipal = Licence(source=Source.HAIFA_PDF, source_id="4242")
        self.state.record(yeela, when=date(2026, 9, 1))
        self.assertTrue(self.state.already_sent(yeela))
        self.assertFalse(self.state.already_sent(municipal))


class TestRecording(TempState):
    def test_record_then_reload_is_remembered(self):
        state = State(self.state_path)
        licence = Licence(source=Source.YEELA, source_id="16191", licence_id="1007665")
        state.record(licence, when=date(2026, 9, 1))

        reloaded = State(self.state_path)
        self.assertTrue(reloaded.already_sent(licence))
        self.assertEqual(date(2026, 9, 1), reloaded.first_seen(licence))

    def test_licence_id_is_stored_as_a_legacy_id_for_future_lookups(self):
        state = State(self.state_path)
        state.record(
            Licence(source=Source.YEELA, source_id="16191", licence_id="1007665"),
            when=date(2026, 9, 1),
        )
        # Same licence, requestId unknown to a later run.
        self.assertTrue(
            State(self.state_path).already_sent(
                Licence(source=Source.YEELA, source_id="?", licence_id="1007665")
            )
        )

    def test_pending_to_licensed_keeps_the_original_first_seen(self):
        state = State(self.state_path)
        pending = Licence(source=Source.YEELA, source_id="16253", stage=Stage.PENDING)
        state.record(pending, when=date(2026, 8, 20))

        licensed = Licence(
            source=Source.YEELA, source_id="16253", licence_id="1007999",
            stage=Stage.LICENSED,
        )
        state.record(licensed, when=date(2026, 9, 1))

        reloaded = State(self.state_path)
        self.assertEqual(date(2026, 8, 20), reloaded.first_seen(licensed))
        self.assertEqual("licensed", reloaded.stage_of(licensed))

    def test_stage_transition_is_visible_before_it_is_recorded(self):
        state = State(self.state_path)
        state.record(
            Licence(source=Source.YEELA, source_id="16253", stage=Stage.PENDING),
            when=date(2026, 8, 20),
        )
        licensed = Licence(
            source=Source.YEELA, source_id="16253", licence_id="1007999",
            stage=Stage.LICENSED,
        )
        self.assertEqual("pending", State(self.state_path).stage_of(licensed))

    def test_corrupt_lines_are_skipped_not_fatal(self):
        self.state_path.write_text(
            '{"key": "yeela:1", "source": "yeela", "source_id": "1"}\n'
            "not json at all\n"
            '{"key": "haifa_pdf:2", "source": "haifa_pdf", "source_id": "2"}\n',
            encoding="utf-8",
        )
        state = State(self.state_path)
        self.assertEqual(2, len(state))

    def test_missing_state_file_starts_empty(self):
        self.assertEqual(0, len(State(self.dir / "nope.jsonl")))


if __name__ == "__main__":
    unittest.main()
