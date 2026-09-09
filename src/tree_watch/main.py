"""Orchestration: collect, match, build, send, verify, then persist.

The order in that sentence is the point. The old ``main()`` called
``save_to_history(lic_id)`` inside the neighbourhood-grouping loop — before the
message was built and before it was sent — while ``send_telegram_msg`` swallowed
every exception and never checked the response. A Telegram failure, a
4096-character overflow, or a crash between the two therefore marked licences as
sent that nobody ever received, permanently.

Nothing here writes state until ``telegram.send`` has returned a verified ok.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from . import config, logging_setup, telegram
from .geo import GeoIndex
from .models import Licence, Source, SourceStats, Stage
from .sources import haifa_pdf
from .sources import yeela as yeela_source
from .state import State

log = logging.getLogger(__name__)

ACTION_LINKS = (
    "🔗 <b>קישורים ומידע נוסף:</b>\n\n"
    "📊 <a href='https://adiofir31.github.io/haifa-tree-watch/'>"
    "כל הנתונים ההיסטוריים — לפי שכונה, רחוב ושנה</a>\n\n"
    "📋 <a href='{pdf}'>לטבלה המלאה באתר העירייה</a>\n\n"
    "🌳 <a href='https://yeela-trees.moag.gov.il/FoPublic/FoLicence'>"
    "מערכת יעלה (משרד החקלאות)</a>\n"
    "<i>(באתר יעלה יש לבחור 'חיפה' בשם הישוב)</i>\n\n"
    "📄 <b>טופס הגשת השגה:</b>\n"
    "<a href='https://yeela-trees.moag.gov.il/api/Fo/doc/documents/"
    "GetFileForPublic?folder=documents&name=%D7%94%D7%92%D7%A9%D7%AA%20"
    "%D7%94%D7%A9%D7%92%D7%94%20%D7%9C%D7%A4%D7%A7%D7%99%D7%93%20"
    "%D7%94%D7%99%D7%A2%D7%A8%D7%95%D7%AA.docx'>"
    "לחצו כאן להורדת טופס ההשגה הרשמי (Word)</a>\n\n"
    "📧 <b>לאן שולחים?</b>\n"
    "יש לשלוח את הטופס המלא למייל פקיד היערות הארצי:\n"
    "<code>trees@moag.gov.il</code>\n\n"
    "יש להגיש את ההשגה תוך 14 יום ממועד פרסום הרישיון.\n"
).format(pdf=config.PDF_URL)


# --------------------------------------------------------------------------
# message building
# --------------------------------------------------------------------------

def group_by_neighborhood(records: list[Licence]) -> dict[str, list[Licence]]:
    grouped: dict[str, list[Licence]] = {}
    for record in records:
        grouped.setdefault(record.neighborhood or config.UNKNOWN_NEIGHBORHOOD, []).append(
            record
        )
    return dict(sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def format_licence_line(record: Licence) -> str:
    count = record.fell_count or sum(record.species.values())
    count_txt = f"<b>{count} עצים</b>" if count != 1 else "עץ אחד"

    parts = [f"• {record.location_text or 'כתובת לא צוינה'} | {count_txt}"]
    if record.species:
        parts.append(f"({record.species_text})")
    # Only when there is no street: the applicant is the one field that gives a
    # block-and-parcel line any human context. It is never a classification —
    # the data has no such field, and the applicants range from a university to
    # a burial society to private individuals.
    if not record.address and record.applicant:
        parts.append(f"| מבקש: {record.applicant}")
    if record.appeal_last:
        parts.append(
            f"| תאריך אחרון לערעור: <b>{record.appeal_last.strftime('%d/%m/%Y')}</b>"
        )
    if not record.can_appeal:
        parts.append("| <i>לא ניתן להגיש השגה</i>")
    if record.geo_approximate:
        parts.append("| <i>שכונה משוערת</i>")
    parts.append(f"| <i>מקור: {record.source.label}</i>")
    return " ".join(parts)


def build_main_message(records: list[Licence], today: date) -> str:
    lines = ["🌳 <b>עדכון רישיונות כריתה חדשים בחיפה</b> 🌳\n"]
    for neighborhood, items in group_by_neighborhood(records).items():
        lines.append(f"📍 <b>{neighborhood}</b>:")
        lines.extend(format_licence_line(item) for item in items)
        lines.append("")
    lines.append(f"📅 <i>עדכון: {today.strftime('%d/%m/%Y')}</i>")
    return "\n".join(lines)


def build_backdated_message(records: list[Licence], today: date) -> str:
    """Licences that appeared in the source dated well before we first saw them."""
    lines = ["🚨 <b>זוהו רישיונות שהוכנסו למערכת בדיעבד!</b>\n"]
    for record in records:
        # The old code interpolated `lic_id`, a variable left over from an
        # earlier loop, so every alert printed the same licence number. The id
        # travels inside the record now.
        identifier = record.licence_id or record.source_id
        published = record.published.strftime("%d/%m/%Y") if record.published else "לא ידוע"
        lines.append(
            f"🚩 רישיון מספר {identifier} בכתובת {record.address}\n"
            f"עלה היום עם התאריך {published}\n"
        )
    lines.append(f"📅 <i>עדכון: {today.strftime('%d/%m/%Y')}</i>")
    return "\n".join(lines)


def build_update_message(records: list[Licence], today: date) -> str:
    """Requests we already reported as pending that have now been licensed.

    This is the message that matters legally: the 14-day objection window opens
    when the licence is issued, not when the request was filed.
    """
    lines = [
        "🔔 <b>עדכון: בקשות שדווחו כממתינות קיבלו רישיון</b>",
        "<i>מרגע פרסום הרישיון נפתח חלון של 14 יום להגשת השגה.</i>\n",
    ]
    for neighborhood, items in group_by_neighborhood(records).items():
        lines.append(f"📍 <b>{neighborhood}</b>:")
        lines.extend(format_licence_line(item) for item in items)
        lines.append("")
    lines.append(f"📅 <i>עדכון: {today.strftime('%d/%m/%Y')}</i>")
    return "\n".join(lines)


def build_pending_message(records: list[Licence], today: date) -> str:
    """Early warning for requests still under review.

    No species, no dates, no appeal window — so no call to action.
    """
    lines = [
        "🔎 <b>בקשות כריתה חדשות שהוגשו וטרם אושרו</b>",
        "<i>הבקשות טרם אושרו ולכן לא ניתן עדיין להגיש השגה. "
        "נעדכן שוב אם וכאשר יינתן רישיון.</i>\n",
    ]
    for record in records:
        bits = [f"• {record.location_text or 'כתובת לא צוינה'}"]
        if record.fell_count:
            bits.append(f"| {record.fell_count} עצים מיועדים לכריתה")
        if record.address and record.block_text:
            bits.append(f"| {record.block_text}")
        if record.applicant:
            bits.append(f"| מבקש: {record.applicant}")
        lines.append(" ".join(bits))
    lines.append(f"\n📅 <i>עדכון: {today.strftime('%d/%m/%Y')}</i>")
    return "\n".join(lines)


def _print(text: str) -> None:
    """print() that cannot die on a legacy console code page."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(
            text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        )
        sys.stdout.write(chr(10))


def is_backdated(record: Licence, today: date, seen_before: bool) -> bool:
    if seen_before or not record.published:
        return False
    return (today - record.published).days > config.BACKDATED_AFTER_DAYS


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def collect_all(today: date, skip_pdf: bool = False, skip_yeela: bool = False):
    """Gather from both sources. A failure in one must not lose the other."""
    licences: list[Licence] = []
    pending: list[Licence] = []
    errors: list[str] = []
    stats: dict[str, SourceStats] = {}

    if not skip_pdf:
        key = Source.HAIFA_PDF.value
        try:
            records, source_stats = haifa_pdf.collect(today=today)
            licences.extend(records)
            stats[key] = source_stats
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            log.exception("municipal PDF source failed")
            errors.append(f"municipal PDF: {exc}")
            stats[key] = SourceStats(source=key, failed=True)

    if not skip_yeela:
        key = Source.YEELA.value
        try:
            open_now, waiting, source_stats = yeela_source.collect(today=today)
            licences.extend(open_now)
            pending.extend(waiting)
            stats[key] = source_stats
        except Exception as exc:  # noqa: BLE001
            log.exception("Yeela source failed")
            errors.append(f"Yeela: {exc}")
            stats[key] = SourceStats(source=key, failed=True)

    return licences, pending, errors, stats


def log_source_funnel(stats: dict[str, SourceStats]) -> None:
    """One INFO block per source, naming the stage where the count hit zero."""
    for key in sorted(stats):
        item = stats[key]
        if item.failed:
            log.info("%s: source FAILED — see the traceback above", key)
            continue

        log.info("%s: %s", key, item.summary())
        log.info(
            "%s: skipped as already sent: %d%s",
            key,
            item.skipped_already_sent,
            f" | pending requests: {item.pending_requests} "
            f"(new: {item.pending_new})" if item.pending_requests else "",
        )

        zero = item.first_zero()
        if zero:
            label, previous_label, previous_value = zero
            log.info(
                "%s: dropped to zero at '%s' (%s was %d) — "
                "nothing from this source will be reported",
                key,
                label,
                previous_label,
                previous_value,
            )


def log_combined(stats: dict[str, SourceStats], fresh, updates, new_pending, backdated) -> None:
    live = [s for s in stats.values() if not s.failed]
    total = SourceStats(source="TOTAL")
    for item in live:
        total.pages += item.pages
        total.raw_rows += item.raw_rows
        total.requests += item.requests
        total.passed_date_filter += item.passed_date_filter
        total.new += item.new
        total.skipped_already_sent += item.skipped_already_sent
        total.pending_requests += item.pending_requests
    log.info("TOTAL (%d source(s)): %s", len(live), total.summary())
    log.info(
        "TOTAL: skipped as already sent: %d | new: %d | updates: %d | "
        "pending reported: %d | back-dated: %d",
        total.skipped_already_sent,
        len(fresh),
        len(updates),
        len(new_pending),
        len(backdated),
    )
    zero = total.first_zero()
    if zero:
        label, previous_label, previous_value = zero
        log.info(
            "TOTAL: dropped to zero at '%s' (%s was %d)",
            label,
            previous_label,
            previous_value,
        )


def run(
    args: argparse.Namespace,
    *,
    geo_index: "GeoIndex | None" = None,
    state_store: "State | None" = None,
    collected=None,
) -> int:
    """Run one pass.

    ``geo_index``, ``state_store`` and ``collected`` exist so the tests can
    exercise the send/persist ordering without a network or a real state file.
    Production passes none of them.
    """
    settings = config.load_settings(require=not args.dry_run)
    today = args.today or date.today()

    if args.dry_run:
        log.info("DRY RUN — nothing will be sent and no state will be written")

    # Checked before any collection: a real run would otherwise spend several
    # minutes on 432 PDF pages and 18 API pages only to refuse at the end.
    if state_store is None and not config.STATE_FILE.exists() and config.LEGACY_STATE.exists():
        # Starting from an empty state with a populated legacy file would treat
        # every licence ever sent as new and re-post months of them.
        message = (
            f"{config.STATE_FILE} does not exist but {config.LEGACY_STATE.name} does. "
            "Run 'python -m tree_watch.main --migrate' first."
        )
        log.error(message)
        if not args.dry_run:
            telegram.notify_operator(message, settings=settings)
            return 1
        log.warning("dry run continues, but every licence will look new")

    if collected is None:
        licences, pending, errors, stats = collect_all(
            today, args.skip_pdf, args.skip_yeela
        )
    else:
        licences, pending, errors = collected
        stats = {}

    if errors and not licences and not pending:
        message = "both sources failed:\n" + "\n".join(errors)
        log.error(message)
        if not args.dry_run:
            telegram.notify_operator(message, settings=settings)
        return 1

    geo = geo_index if geo_index is not None else GeoIndex()
    state = state_store if state_store is not None else State()

    def resolve(record: Licence) -> None:
        match = geo.lookup(
            record.street,
            record.house,
            full_text=record.address,
            block=record.block,
            parcel=record.parcel,
        )
        record.neighborhood = match.neighborhood
        record.geo_confidence = match.confidence
        record.geo_method = match.method
        record.geo_approximate = match.approximate
        if not match.known and match.has_address:
            # Only an address we failed to place is worth curating. A licence
            # with no address at all is a source-data gap, not an index gap.
            if args.dry_run:
                log.info("unresolved address (dry run, not recorded): %r", record.address)
            else:
                geo.record_unmatched(record.street, record.house, record.address)
        elif not match.has_address:
            log.info(
                "%s has no usable location at all (block %r parcel %r)",
                record.key,
                record.block,
                record.parcel,
            )

    def stat_for(record: Licence) -> SourceStats:
        key = record.source.value
        if key not in stats:
            stats[key] = SourceStats(source=key)
        return stats[key]

    fresh: list[Licence] = []
    updates: list[Licence] = []
    for record in licences:
        counter = stat_for(record)
        seen = state.already_sent(record)
        if seen:
            counter.skipped_already_sent += 1
            # A request we reported while it was still under review has now been
            # granted a licence. Only at that point does the 14-day appeal clock
            # start, so it is worth a second message — and keying on requestId is
            # what makes that safe from duplicates.
            was_pending = state.stage_of(record) == Stage.PENDING.value
            if not (was_pending and record.stage is Stage.LICENSED):
                continue
            counter.skipped_already_sent -= 1  # it is an update, not a skip
            resolve(record)
            updates.append(record)
            continue
        counter.new += 1
        resolve(record)
        fresh.append(record)

    new_pending: list[Licence] = []
    if settings.report_pending:
        for record in pending:
            if state.already_sent(record):
                continue
            stat_for(record).pending_new += 1
            resolve(record)
            new_pending.append(record)

    if collected is None:
        log_source_funnel(stats)

    if not fresh and not updates and not new_pending:
        log_combined(stats, fresh, updates, new_pending, [])
        log.info("nothing new")
        if errors and not args.dry_run:
            telegram.notify_operator(
                "run completed with partial data:\n" + "\n".join(errors),
                settings=settings,
            )
        return 0

    # `fresh` only ever holds records that were NOT already sent, so seen_before
    # is False for all of them today. The parameter is kept because the check is
    # about our own first sighting, not the source's date field, and an update
    # (pending -> licensed) must never be flagged as back-dated — see
    # TestPendingToLicensedUpdate::test_the_update_is_not_flagged_as_backdated.
    # `updates` are deliberately not passed through this filter at all.
    backdated = [
        r for r in fresh if is_backdated(r, today, state.first_seen(r) is not None)
    ]

    log_combined(stats, fresh, updates, new_pending, backdated)

    # --- build everything first ------------------------------------------
    outbound: list[tuple[str, list[Licence]]] = []
    if fresh:
        outbound.append((build_main_message(fresh, today), fresh))
    if updates:
        outbound.append((build_update_message(updates, today), updates))
    if backdated:
        outbound.append((build_backdated_message(backdated, today), []))
    if new_pending:
        outbound.append((build_pending_message(new_pending, today), new_pending))
    if fresh or updates:
        outbound.append((ACTION_LINKS, []))

    if args.dry_run:
        for text, _ in outbound:
            chunks = telegram.split_message(text)
            _print(f"\n--- message ({len(text)} chars, {len(chunks)} chunk(s)) ---")
            _print(text)
        log.info(
            "dry run: %d new licences, %d updates, %d pending, %d back-dated "
            "— nothing sent and no state written",
            len(fresh),
            len(updates),
            len(new_pending),
            len(backdated),
        )
        return 0

    # --- send, verify, and only then persist ------------------------------
    for text, to_persist in outbound:
        try:
            telegram.send(text, settings=settings)
        except telegram.SendFailed as exc:
            log.error("send failed, state not written: %s", exc)
            telegram.notify_operator(f"send failed: {exc}", settings=settings)
            return 1
        for record in to_persist:
            state.record(record, when=today)

    if errors:
        telegram.notify_operator(
            "run completed with partial data:\n" + "\n".join(errors), settings=settings
        )
    log.info(
        "done: %d new licences, %d updates, %d pending reported",
        len(fresh),
        len(updates),
        len(new_pending),
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tree-watch", description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything except send to Telegram and except write state",
    )
    parser.add_argument("--skip-pdf", action="store_true", help="skip the municipal PDF")
    parser.add_argument("--skip-yeela", action="store_true", help="skip the Yeela API")
    parser.add_argument(
        "--today",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        help="pretend today is this date (YYYY-MM-DD), for verification runs",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="import sent_licenses.txt into data/state.jsonl and exit "
        "(idempotent; the legacy file is only read)",
    )
    parser.add_argument("--log-level", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging_setup.setup(args.log_level or "INFO")
    try:
        if args.migrate:
            written = State.migrate()
            log.info(
                "migration complete: %d new record(s) in %s; %s left untouched",
                written,
                config.STATE_FILE,
                config.LEGACY_STATE.name,
            )
            return 0
        return run(args)
    except Exception as exc:  # noqa: BLE001 - top level: log, alert, exit non-zero
        log.exception("unhandled failure")
        try:
            settings = config.load_settings(require=False)
            if settings.can_send and not args.dry_run:
                telegram.notify_operator(f"unhandled failure: {exc}", settings=settings)
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
