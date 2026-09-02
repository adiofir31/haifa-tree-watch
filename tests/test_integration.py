"""End-to-end: migrate the real sent_licenses.txt, then prove a dry run is quiet.

This is the check that matters before cutover. If it fails, the new bot would
re-post months of old licences to a public Telegram channel.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests import ROOT, yeela_rows
from tree_watch import main as main_module
from tree_watch.geo import Match
from tree_watch.models import Licence, Source
from tree_watch.state import State, classify_legacy_id

LEGACY = ROOT / "sent_licenses.txt"


class FlatGeo:
    def lookup(self, street, house=None, full_text="", block=None, parcel=None):
        return Match("הדר מרכז", 1.0, "house_range_p1")

    def record_unmatched(self, street, house, full_text):
        pass


class TestMigrationThenDryRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="treewatch-e2e-"))
        cls.state_path = cls.dir / "state.jsonl"
        cls.written = State.migrate(LEGACY, cls.state_path)
        cls.map = {
            str(r["licenseId"]): str(r["requestId"])
            for r in yeela_rows()
            if r.get("licenseId") and r.get("requestId")
        }

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self._env = {
            k: os.environ.get(k)
            for k in ("TELEGRAM_TOKEN", "CHAT_ID", "OPERATOR_CHAT_ID", "REPORT_PENDING")
        }
        os.environ["REPORT_PENDING"] = "false"
        os.environ.pop("TELEGRAM_TOKEN", None)
        os.environ.pop("CHAT_ID", None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def legacy_licences(self) -> list[Licence]:
        """Every id in sent_licenses.txt, as the record the new code would build."""
        out = []
        for raw in LEGACY.read_text(encoding="utf-8-sig").splitlines():
            identifier = raw.strip()
            source = classify_legacy_id(identifier)
            if source is Source.YEELA:
                out.append(
                    Licence(
                        source=Source.YEELA,
                        source_id=self.map.get(identifier, f"unknown-{identifier}"),
                        licence_id=identifier,
                        street="הולנד",
                        house="36",
                        fell_count=1,
                        species={"אורן": 1},
                        appeal_last=date(2026, 9, 30),
                        published=date(2026, 9, 1),
                    )
                )
            elif source is Source.HAIFA_PDF:
                out.append(
                    Licence(
                        source=Source.HAIFA_PDF,
                        source_id=identifier,
                        street="הולנד",
                        house="36",
                        fell_count=1,
                        species={"אורן": 1},
                        appeal_last=date(2026, 9, 30),
                        published=date(2026, 9, 1),
                    )
                )
        return out

    def dry_run(self, licences) -> str:
        args = argparse.Namespace(
            dry_run=True, skip_pdf=False, skip_yeela=False,
            today=date(2026, 9, 1), log_level="ERROR",
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main_module.run(
                args,
                geo_index=FlatGeo(),
                state_store=State(self.state_path),
                collected=(licences, [], []),
            )
        self.assertEqual(0, code)
        return buffer.getvalue()

    def expected_records(self) -> int:
        return sum(
            1
            for line in LEGACY.read_text(encoding="utf-8-sig").splitlines()
            if classify_legacy_id(line.strip()) is not None
        )

    def test_migration_wrote_every_classifiable_id(self):
        self.assertEqual(self.expected_records(), self.written)

    def test_dry_run_finds_none_of_the_legacy_ids_as_new(self):
        licences = self.legacy_licences()
        self.assertEqual(self.expected_records(), len(licences))
        output = self.dry_run(licences)
        self.assertNotIn("--- message", output, "nothing should have been built")
        self.assertEqual("", output.strip())

    def test_the_check_is_not_vacuous(self):
        # The same harness must produce a message for a licence we have not seen.
        stranger = Licence(
            source=Source.YEELA, source_id="99999", licence_id="1099999",
            street="הולנד", house="36", fell_count=1, species={"אורן": 1},
            appeal_last=date(2026, 9, 30), published=date(2026, 9, 1),
        )
        output = self.dry_run([stranger])
        self.assertIn("--- message", output)
        self.assertIn("הולנד 36", output)

    def test_one_new_licence_among_the_old_is_still_found(self):
        stranger = Licence(
            source=Source.HAIFA_PDF, source_id="9998",
            street="מוריה", house="10", fell_count=2, species={"ברוש": 2},
            appeal_last=date(2026, 9, 30), published=date(2026, 9, 1),
        )
        output = self.dry_run(self.legacy_licences() + [stranger])
        self.assertIn("--- message", output)
        self.assertIn("מוריה 10", output)
        self.assertNotIn("הולנד", output, "none of the old ones may appear")

    def test_dry_run_wrote_nothing_to_the_state_file(self):
        before = self.state_path.read_bytes()
        self.dry_run(self.legacy_licences())
        self.assertEqual(before, self.state_path.read_bytes())

    def test_dry_run_did_not_touch_the_legacy_file(self):
        before = LEGACY.read_bytes()
        self.dry_run(self.legacy_licences())
        self.assertEqual(before, LEGACY.read_bytes())


if __name__ == "__main__":
    unittest.main()
