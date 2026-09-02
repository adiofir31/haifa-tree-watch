"""Orchestration — bug 1 (persist only after a verified send) and bug 2.

These are the two defects that could not be caught by testing a source in
isolation, so they are tested through ``main.run`` with fake geo/state/transport.
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tree_watch import config, main as main_module, telegram
from tree_watch.geo import Match
from tree_watch.models import Licence, Source, Stage


class FakeGeo:
    def __init__(self, neighborhood="הדר מרכז"):
        self.neighborhood = neighborhood
        self.unmatched = []

    def lookup(self, street, house=None, full_text="", block=None, parcel=None):
        return Match(self.neighborhood, 1.0, "exact_house_range")

    def record_unmatched(self, street, house, full_text):
        self.unmatched.append((street, house, full_text))


class FakeState:
    def __init__(self, sent=(), first_seen=None, stages=None):
        self._sent = set(sent)
        self._first_seen = first_seen or {}
        self._stages = stages or {}
        self.written = []

    def already_sent(self, licence):
        return licence.key in self._sent

    def first_seen(self, licence):
        return self._first_seen.get(licence.key)

    def stage_of(self, licence):
        return self._stages.get(licence.key)

    def record(self, licence, when=None):
        self.written.append(licence.key)


def licence(source=Source.YEELA, source_id="1", **kwargs):
    defaults = dict(
        street="הולנד",
        house="36",
        fell_count=3,
        species={"אורן": 3},
        appeal_last=date(2026, 9, 10),
        published=date(2026, 9, 1),
        stage=Stage.LICENSED,
    )
    defaults.update(kwargs)
    return Licence(source=source, source_id=source_id, **defaults)


def args(**kwargs):
    base = dict(dry_run=False, skip_pdf=False, skip_yeela=False, today=date(2026, 9, 1),
                log_level="ERROR")
    base.update(kwargs)
    return argparse.Namespace(**base)


class MainHarness(unittest.TestCase):
    def setUp(self):
        self._env = {
            k: os.environ.get(k)
            for k in ("TELEGRAM_TOKEN", "CHAT_ID", "OPERATOR_CHAT_ID", "REPORT_PENDING")
        }
        os.environ["TELEGRAM_TOKEN"] = "tok"
        os.environ["CHAT_ID"] = "-100"
        os.environ["OPERATOR_CHAT_ID"] = "42"
        os.environ["REPORT_PENDING"] = "false"
        self.sent = []
        self.fail_on = None
        self._original_send = telegram.send
        self._original_notify = telegram.notify_operator
        telegram.send = self._send
        telegram.notify_operator = self._notify
        main_module.telegram.send = self._send
        main_module.telegram.notify_operator = self._notify
        self.notified = []

    def tearDown(self):
        telegram.send = self._original_send
        telegram.notify_operator = self._original_notify
        main_module.telegram.send = self._original_send
        main_module.telegram.notify_operator = self._original_notify
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _send(self, text, *, settings, chat_id=None, **kwargs):
        self.sent.append(text)
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise telegram.SendFailed("simulated failure")
        return [{"ok": True}]

    def _notify(self, text, *, settings):
        self.notified.append(text)


class TestPersistOrdering(MainHarness):
    """Bug 1: history was written before the message was built or sent."""

    def test_state_is_written_after_a_successful_send(self):
        state = FakeState()
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([licence(source_id="16191")], [], []),
        )
        self.assertEqual(0, code)
        self.assertEqual(["yeela:16191"], state.written)
        self.assertGreaterEqual(len(self.sent), 1)

    def test_nothing_is_persisted_when_the_send_fails(self):
        state = FakeState()
        self.fail_on = 1
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([licence(source_id="16191")], [], []),
        )
        self.assertEqual(1, code, "a failed run must exit non-zero")
        self.assertEqual([], state.written, "a licence nobody received must stay unsent")
        self.assertEqual(1, len(self.notified))

    def test_a_failure_partway_through_does_not_persist_the_rest(self):
        state = FakeState()
        self.fail_on = 1
        main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([licence(source_id="a"), licence(source_id="b")], [], []),
        )
        self.assertEqual([], state.written)

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        state = FakeState()
        code = main_module.run(
            args(dry_run=True), geo_index=FakeGeo(), state_store=state,
            collected=([licence(source_id="16191")], [], []),
        )
        self.assertEqual(0, code)
        self.assertEqual([], self.sent)
        self.assertEqual([], state.written)

    def test_already_sent_licences_are_skipped(self):
        state = FakeState(sent={"yeela:16191"})
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([licence(source_id="16191")], [], []),
        )
        self.assertEqual(0, code)
        self.assertEqual([], self.sent)
        self.assertEqual([], state.written)


class TestBackdatedAlert(MainHarness):
    """Bug 2: every alert printed the same licence number."""

    def test_each_alert_names_its_own_licence(self):
        records = [
            licence(source_id="1", licence_id="1007001", street="הולנד"),
            licence(source_id="2", licence_id="1007002", street="מוריה"),
            licence(source_id="3", licence_id="1007003", street="הנשיא"),
        ]
        text = main_module.build_backdated_message(records, date(2026, 9, 1))
        for expected in ("1007001", "1007002", "1007003"):
            self.assertIn(expected, text)
        self.assertEqual(1, text.count("1007001"))

    def test_falls_back_to_the_request_id_when_there_is_no_licence_id(self):
        text = main_module.build_backdated_message(
            [licence(source_id="16253", licence_id=None)], date(2026, 9, 1)
        )
        self.assertIn("16253", text)
        self.assertNotIn("None", text)

    def test_backdated_only_when_old_and_unseen(self):
        old = licence(published=date(2026, 8, 1))
        recent = licence(published=date(2026, 8, 30))
        today = date(2026, 9, 1)
        self.assertTrue(main_module.is_backdated(old, today, seen_before=False))
        self.assertFalse(main_module.is_backdated(old, today, seen_before=True))
        self.assertFalse(main_module.is_backdated(recent, today, seen_before=False))


class TestMessageContent(MainHarness):
    def test_species_and_count_appear(self):
        text = main_module.build_main_message(
            [licence(species={"אורן": 2, "ברוש מצוי": 1}, fell_count=3)], date(2026, 9, 1)
        )
        self.assertIn("3 עצים", text)
        self.assertIn("אורן x2", text)
        self.assertIn("ברוש מצוי", text)

    def test_unappealable_licence_is_marked(self):
        text = main_module.build_main_message(
            [licence(can_appeal=False)], date(2026, 9, 1)
        )
        self.assertIn("לא ניתן להגיש השגה", text)

    def test_fuzzy_neighborhood_is_flagged_as_approximate(self):
        record = licence()
        record.neighborhood = "אחוזה"
        record.geo_method = "fuzzy_street_level_p2"
        record.geo_approximate = True
        self.assertIn("שכונה משוערת", main_module.format_licence_line(record))

    def test_grouping_is_by_neighbourhood(self):
        a, b = licence(source_id="1"), licence(source_id="2")
        a.neighborhood, b.neighborhood = "הדר", "אחוזה"
        grouped = main_module.group_by_neighborhood([a, b])
        self.assertEqual({"הדר", "אחוזה"}, set(grouped))


class TestPendingRequests(MainHarness):
    """Yeela requests with no licenceId — early warning, behind a config flag."""

    def pending_licence(self, source_id="16253"):
        return licence(
            source_id=source_id, stage=Stage.PENDING, licence_id=None,
            species={}, fell_count=14, appeal_last=None, published=None,
            block="12508", parcel="11", applicant="ברקן בוטיק בע\"מ",
        )

    def test_not_reported_when_the_flag_is_off(self):
        os.environ["REPORT_PENDING"] = "false"
        state = FakeState()
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([], [self.pending_licence()], []),
        )
        self.assertEqual(0, code)
        self.assertEqual([], self.sent)
        self.assertEqual([], state.written)

    def test_reported_when_the_flag_is_on(self):
        os.environ["REPORT_PENDING"] = "true"
        state = FakeState()
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([], [self.pending_licence()], []),
        )
        self.assertEqual(0, code)
        self.assertEqual(1, len(self.sent))
        self.assertEqual(["yeela:16253"], state.written)

    def test_message_has_no_call_to_action(self):
        text = main_module.build_pending_message(
            [self.pending_licence()], date(2026, 9, 1)
        )
        self.assertIn("טרם אושרו", text)
        self.assertIn("14 עצים", text)
        self.assertIn("גוש 12508", text)
        self.assertNotIn("תאריך אחרון לערעור", text)
        self.assertNotIn("טופס", text)

    def test_a_pending_request_is_only_reported_once(self):
        os.environ["REPORT_PENDING"] = "true"
        state = FakeState(sent={"yeela:16253"})
        main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([], [self.pending_licence()], []),
        )
        self.assertEqual([], self.sent)


class TestPendingToLicensedUpdate(MainHarness):
    """The second message, sent when the appeal clock actually starts."""

    def licensed_now(self, source_id="16253"):
        return licence(source_id=source_id, licence_id="1007999", stage=Stage.LICENSED)

    def test_a_previously_pending_request_gets_an_update(self):
        state = FakeState(
            sent={"yeela:16253"}, stages={"yeela:16253": "pending"},
            first_seen={"yeela:16253": date(2026, 8, 20)},
        )
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([self.licensed_now()], [], []),
        )
        self.assertEqual(0, code)
        self.assertEqual(["yeela:16253"], state.written)
        self.assertTrue(any("קיבלו רישיון" in t for t in self.sent))

    def test_an_already_licensed_request_is_not_resent(self):
        state = FakeState(sent={"yeela:16253"}, stages={"yeela:16253": "licensed"})
        main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([self.licensed_now()], [], []),
        )
        self.assertEqual([], self.sent)
        self.assertEqual([], state.written)

    def test_the_update_names_the_appeal_window(self):
        text = main_module.build_update_message([self.licensed_now()], date(2026, 9, 1))
        self.assertIn("14 יום", text)
        self.assertIn("10/09/2026", text)

    def test_the_update_is_not_flagged_as_backdated(self):
        # We have seen it before, so the back-dated alert must not fire.
        state = FakeState(
            sent={"yeela:16253"}, stages={"yeela:16253": "pending"},
            first_seen={"yeela:16253": date(2026, 7, 1)},
        )
        main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([self.licensed_now()], [], []),
        )
        self.assertFalse(any("בדיעבד" in t for t in self.sent))

    def test_the_update_is_persisted_only_after_the_send(self):
        state = FakeState(sent={"yeela:16253"}, stages={"yeela:16253": "pending"})
        self.fail_on = 1
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([self.licensed_now()], [], []),
        )
        self.assertEqual(1, code)
        self.assertEqual([], state.written)


class TestUnknownNeighbourhoodIsLogged(MainHarness):
    def test_a_miss_is_recorded_for_curation(self):
        class MissingGeo(FakeGeo):
            def lookup(self, street, house=None, full_text="", block=None, parcel=None):
                from tree_watch import config as cfg

                return Match(cfg.UNKNOWN_NEIGHBORHOOD, 0.0, "unknown")

        geo = MissingGeo()
        main_module.run(
            args(), geo_index=geo, state_store=FakeState(),
            collected=([licence(source_id="1")], [], []),
        )
        self.assertEqual(1, len(geo.unmatched))

    def test_a_dry_run_records_nothing(self):
        class MissingGeo(FakeGeo):
            def lookup(self, street, house=None, full_text="", block=None, parcel=None):
                from tree_watch import config as cfg

                return Match(cfg.UNKNOWN_NEIGHBORHOOD, 0.0, "unknown")

        geo = MissingGeo()
        main_module.run(
            args(dry_run=True), geo_index=geo, state_store=FakeState(),
            collected=([licence(source_id="1")], [], []),
        )
        self.assertEqual([], geo.unmatched, "--dry-run must leave no trace")


class TestUnmigratedStateGuard(MainHarness):
    """An empty state file next to a populated legacy file is a mass re-send."""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="treewatch-guard-"))
        self._state_file = config.STATE_FILE
        config.STATE_FILE = self.dir / "state.jsonl"

    def tearDown(self):
        config.STATE_FILE = self._state_file
        shutil.rmtree(self.dir, ignore_errors=True)
        super().tearDown()

    def test_the_guard_runs_before_any_collection(self):
        # A real run would otherwise spend minutes on 432 PDF pages and 18 API
        # pages before refusing.
        called = []

        def tripwire(*a, **kw):
            called.append(a)
            raise AssertionError("collect_all must not run before the guard")

        original = main_module.collect_all
        main_module.collect_all = tripwire
        try:
            code = main_module.run(args())
        finally:
            main_module.collect_all = original
        self.assertEqual(1, code)
        self.assertEqual([], called)

    def test_refuses_to_run_and_tells_the_operator(self):
        self.assertTrue(config.LEGACY_STATE.exists(), "the legacy file is the trigger")
        code = main_module.run(
            args(), collected=([licence(source_id="1")], [], []),
        )
        self.assertEqual(1, code)
        self.assertEqual([], self.sent, "nothing may reach the public channel")
        self.assertEqual(1, len(self.notified))
        self.assertIn("--migrate", self.notified[0])

    def test_dry_run_warns_but_continues(self):
        code = main_module.run(
            args(dry_run=True), geo_index=FakeGeo(), state_store=FakeState(),
            collected=([licence(source_id="1")], [], []),
        )
        self.assertEqual(0, code)

    def test_no_guard_once_the_state_file_exists(self):
        config.STATE_FILE.write_text("", encoding="utf-8")
        state = FakeState()
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([licence(source_id="1")], [], []),
        )
        self.assertEqual(0, code)
        self.assertEqual(["yeela:1"], state.written)


class TestSourceFailure(MainHarness):
    def test_both_sources_failing_alerts_the_operator_and_exits_non_zero(self):
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=FakeState(),
            collected=([], [], ["municipal PDF: boom", "Yeela: boom"]),
        )
        self.assertEqual(1, code)
        self.assertEqual([], self.sent, "nothing goes to the public channel")
        self.assertEqual(1, len(self.notified))

    def test_one_source_failing_still_reports_the_other(self):
        state = FakeState()
        code = main_module.run(
            args(), geo_index=FakeGeo(), state_store=state,
            collected=([licence(source_id="16191")], [], ["Yeela: boom"]),
        )
        self.assertEqual(0, code)
        self.assertEqual(["yeela:16191"], state.written)
        self.assertEqual(1, len(self.notified), "operator still told about the partial run")


if __name__ == "__main__":
    unittest.main()
