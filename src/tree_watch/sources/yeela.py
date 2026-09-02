"""Ministry of Agriculture "Yeela" felling-licence API.

BiDi
----
This API returns Hebrew in **logical** order already — ``'יותם 11'`` arrives
reading correctly. Passing it through ``get_display`` would reverse it. The
municipal PDF is the opposite case. Two sources, two text paths, deliberately.

Shape
-----
One row per (licence x species). Each row carries three independent counters at
licence level::

    unproot       trees to be felled       כריתה
    copying       trees to be relocated    העתקה
    conservation  trees to be preserved    שימור

They are not exclusive: 265 rows in the captured fixture set both ``unproot``
and ``conservation``. The old bot read only ``unproot`` for the count but listed
every species on the licence, including trees that are being preserved.

``requestId`` is present on every row and survives the transition from "under
review" to "licensed". ``licenseId`` is ``null`` for 443 of 1,745 rows, and
``str(None)`` is what put the literal line ``None`` in the legacy state file.
State is keyed on ``requestId``; ``licenseId`` is carried for display only.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

from .. import config, net
from ..models import Licence, Source, SourceStats, Stage
from ..normalize import clean_text, split_street_house

log = logging.getLogger(__name__)


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).split("T")[0], "%Y-%m-%d").date()
    except ValueError:
        log.warning("unparseable Yeela date %r", value)
        return None


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_rows(rows: list[dict]) -> list[Licence]:
    """Aggregate raw API rows into one :class:`Licence` per ``requestId``."""
    by_request: dict[str, Licence] = {}
    skipped = 0

    for row in rows:
        request_id = row.get("requestId")
        if request_id in (None, ""):
            # Never write a record with an empty key.
            skipped += 1
            log.warning("Yeela row with no requestId, skipped: %r", str(row)[:200])
            continue
        request_id = str(request_id)

        licence_id = row.get("licenseId")
        licence_id = str(licence_id) if licence_id not in (None, "") else None

        expand = (row.get("expandRows") or [{}])[0]
        street_raw = clean_text(expand.get("street"))
        house = clean_text(expand.get("homeNumber"))
        if not house:
            street_raw, house = split_street_house(street_raw)
        if not street_raw:
            # Nothing to geocode. Reported by requestId so it can be looked up
            # by hand at https://yeela-trees.moag.gov.il/FoPublic/FoLicence
            log.warning(
                "Yeela requestId %s has no street at all "
                "(licenceId %s, block %s parcel %s, applicant %r)",
                request_id,
                licence_id,
                clean_text(expand.get("block")) or "?",
                clean_text(expand.get("parcel")) or "?",
                clean_text(expand.get("customerName")),
            )

        status = clean_text(row.get("licenseStatusDesc"))

        record = Licence(
            source=Source.YEELA,
            source_id=request_id,
            stage=Stage.LICENSED if licence_id else Stage.PENDING,
            licence_id=licence_id,
            street=street_raw,
            house=house,
            appeal_last=_parse_date(row.get("appealLastDate")),
            published=_parse_date(row.get("approvedDate")),
            can_appeal=bool(licence_id) and "לא ניתן" not in status,
            applicant=clean_text(expand.get("customerName")),
            block=clean_text(expand.get("block")),
            parcel=clean_text(expand.get("parcel")),
            reason=clean_text(expand.get("requestReason")) or status,
        )

        fell = _int(row.get("unproot"))
        record.fell_count = fell
        record.relocate_count = _int(row.get("copying"))
        record.preserve_count = _int(row.get("conservation"))

        # Only species actually being felled belong in a felling alert.
        species = clean_text(row.get("treeName"))
        if fell > 0 and species and species.lower() != "none":
            record.species[species] = fell

        existing = by_request.get(request_id)
        if existing is None:
            by_request[request_id] = record
        else:
            existing.merge(record)

    if skipped:
        log.warning("skipped %d Yeela rows with no requestId", skipped)
    return list(by_request.values())


def relevant(records: list[Licence], today: date | None = None) -> list[Licence]:
    """Licensed requests that actually fell something and are still appealable.

    Pending requests are handled separately (see ``main``), because they have no
    species, no dates and no appeal window.
    """
    today = today or date.today()
    keep = []
    for record in records:
        if record.stage is not Stage.LICENSED:
            continue
        if record.fell_count <= 0:
            continue
        if record.appeal_last and record.appeal_last < today:
            continue
        keep.append(record)
    return keep


def pending(records: list[Licence]) -> list[Licence]:
    """Requests still under review: no licence id yet, felling already declared."""
    return [
        r for r in records if r.stage is Stage.PENDING and r.fell_count > 0
    ]


def fetch_page(page: int, city_id: int | None = None) -> dict:
    payload = {
        "orderDetails": None,
        "pageDetails": {"pageNumber": page, "pageSize": config.YEELA_PAGE_SIZE},
        "parameters": {
            "cityId": city_id or config.YEELA_CITY_ID,
            "appealLastDate": None,
        },
    }
    response = net.post(config.YEELA_URL, json=payload, headers=config.YEELA_HEADERS)
    return response.json()


def fetch_all(city_id: int | None = None) -> list[dict]:
    """Walk every page.

    The response carries ``pagination: {totalPages: 18, totalCount: 1738}``.
    The old client read page 1 and stopped, so it saw 100 of 1,738 rows.
    """
    return fetch_all_with_pages(city_id)[0]


def fetch_all_with_pages(city_id: int | None = None) -> tuple[list[dict], int]:
    """As :func:`fetch_all`, also reporting how many pages were actually read."""
    first = fetch_page(1, city_id)
    pagination = first.get("pagination") or {}
    total_pages = int(pagination.get("totalPages") or 1)
    total_count = pagination.get("totalCount")
    if total_pages > config.YEELA_MAX_PAGES:
        log.warning(
            "Yeela reports %d pages, capping at %d", total_pages, config.YEELA_MAX_PAGES
        )
        total_pages = config.YEELA_MAX_PAGES
    log.info("Yeela: API reports %s rows across %d pages", total_count, total_pages)

    rows = list(first.get("result") or [])
    for page in range(2, total_pages + 1):
        time.sleep(config.YEELA_PAGE_DELAY)
        rows.extend(fetch_page(page, city_id).get("result") or [])
    return rows, total_pages


def collect(
    today: date | None = None, city_id: int | None = None
) -> tuple[list[Licence], list[Licence], SourceStats]:
    """Return ``(licensed_and_open, pending, stats)``."""
    rows, pages = fetch_all_with_pages(city_id)
    stats = SourceStats(source=Source.YEELA.value, pages=pages, raw_rows=len(rows))

    records = parse_rows(rows)
    stats.requests = len(records)

    open_now = relevant(records, today=today)
    stats.passed_date_filter = len(open_now)

    waiting = pending(records)
    stats.pending_requests = len(waiting)
    return open_now, waiting, stats
