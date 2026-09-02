"""Persistent "already reported" state.

Format: JSONL, one record per line, in ``data/state.jsonl``::

    {"key": "yeela:16191", "source": "yeela", "source_id": "16191",
     "licence_id": "1007665", "legacy_ids": ["1007665"],
     "first_seen": "2026-08-10", "stage": "licensed", "content": {...}}

Later lines win, so a record can be rewritten by appending — which is how a
request moves from ``pending`` to ``licensed`` without rewriting the file.

Keys are namespaced by source
-----------------------------
Yeela ``requestId`` values and municipal request numbers occupy the same numeric
range. Nine numbers (2177, 2180, 2181, 2195, 2201, 2214, 2270, 2280, 2285) are
already both. A bare integer key would let one source's request suppress the
other's — a silently missed alert, which is worse than a duplicate. Hence
``yeela:16191`` and ``haifa_pdf:2273``, and every legacy lookup is scoped to one
source.

Migration from ``sent_licenses.txt``
------------------------------------
Runs once, and is idempotent — re-running adds nothing. ``sent_licenses.txt`` is
**read-only and stays on disk**; a verified copy is at ``_archive/backups/``.

The legacy file mixes two identifier spaces, and they are separable by length:

* 134 lines of 7 digits — Yeela ``licenseId``. Every Yeela ``licenseId`` in the
  API is 7 digits (min 1000155), and no ``requestId`` is (max 16253).
* 133 lines of 4 digits — municipal request numbers.
* 1 line reading ``None`` — the ``licenseId: null`` bug. Skipped.

So no ``licenseId -> requestId`` map is needed. A legacy id is imported as a
legacy id for its source, and :meth:`State.already_sent` checks the namespaced
key *or* the legacy id, always within the same source. A Yeela licence that has
since dropped out of the API window is still recognised, because the match is on
``licenseId``, which never changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config
from .models import Licence, Source, Stage

log = logging.getLogger(__name__)

LEGACY_PREFIX = "legacy"
#: Yeela licence ids are 7 digits; municipal request numbers are 4.
YEELA_ID_LENGTH = 7


class MigrationError(RuntimeError):
    """The legacy file could not be read. Never swallowed."""


def classify_legacy_id(raw: str) -> Source | None:
    """Which source a line of ``sent_licenses.txt`` came from.

    Returns ``None`` for the literal ``None`` line and for anything non-numeric.
    """
    value = (raw or "").strip()
    if not value or value == "None" or not value.isdigit():
        return None
    return Source.YEELA if len(value) == YEELA_ID_LENGTH else Source.HAIFA_PDF


def legacy_key(source: Source, identifier: str) -> str:
    return f"{source.value}:{LEGACY_PREFIX}:{identifier}"


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _from_iso(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass
class Record:
    key: str
    source: str
    source_id: str
    licence_id: str | None = None
    legacy_ids: tuple[str, ...] = ()
    first_seen: date | None = None
    stage: str = Stage.LICENSED.value
    content: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "key": self.key,
                "source": self.source,
                "source_id": self.source_id,
                "licence_id": self.licence_id,
                "legacy_ids": list(self.legacy_ids),
                "first_seen": _iso(self.first_seen),
                "stage": self.stage,
                "content": self.content or None,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_dict(cls, payload: dict) -> "Record":
        return cls(
            key=str(payload["key"]),
            source=str(payload.get("source") or ""),
            source_id=str(payload.get("source_id") or ""),
            licence_id=payload.get("licence_id"),
            legacy_ids=tuple(str(x) for x in (payload.get("legacy_ids") or ())),
            first_seen=_from_iso(payload.get("first_seen")),
            stage=str(payload.get("stage") or Stage.LICENSED.value),
            content=payload.get("content") or {},
        )


def natural_legacy_id(licence: Licence) -> str | None:
    """The identifier the legacy bot would have written for this licence.

    Yeela: the 7-digit ``licenseId`` (nothing was written while pending, beyond
    the single poisoned ``None``). Municipal: the request number.
    """
    if licence.source is Source.YEELA:
        return licence.licence_id
    return licence.source_id or None


class State:
    """The set of requests already reported, loaded once per run."""

    def __init__(self, path: Path | None = None, autoload: bool = True):
        self.path = Path(path) if path else config.STATE_FILE
        self._records: dict[str, Record] = {}
        self._legacy: dict[str, dict[str, Record]] = {s.value: {} for s in Source}
        if autoload:
            self.load()

    # --- loading ---------------------------------------------------------
    def load(self) -> None:
        self._records.clear()
        for bucket in self._legacy.values():
            bucket.clear()
        if not self.path.exists():
            log.info("no state file at %s — starting empty", self.path)
            return

        bad = 0
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = Record.from_dict(json.loads(line))
                except (ValueError, KeyError):
                    bad += 1
                    log.warning("%s:%d is not a valid state record", self.path, number)
                    continue
                self._index(record)
        if bad:
            log.warning("%d unreadable line(s) in %s were ignored", bad, self.path)
        log.info("state: %d records loaded from %s", len(self._records), self.path)

    def _index(self, record: Record) -> None:
        self._records[record.key] = record  # later lines win
        bucket = self._legacy.setdefault(record.source, {})
        for identifier in record.legacy_ids:
            bucket[identifier] = record

    def _find(self, licence: Licence) -> Record | None:
        record = self._records.get(licence.key)
        if record is not None:
            return record
        identifier = natural_legacy_id(licence)
        if not identifier:
            return None
        # Same source only. Cross-source matching would let Yeela request 2177
        # suppress municipal request 2177.
        return self._legacy.get(licence.source.value, {}).get(identifier)

    # --- queries ---------------------------------------------------------
    def already_sent(self, licence: Licence) -> bool:
        return self._find(licence) is not None

    def first_seen(self, licence: Licence) -> date | None:
        record = self._find(licence)
        return record.first_seen if record else None

    def stage_of(self, licence: Licence) -> str | None:
        record = self._find(licence)
        return record.stage if record else None

    def __len__(self) -> int:
        return len(self._records)

    # --- writes ----------------------------------------------------------
    def record(self, licence: Licence, *, when: date | None = None) -> Record:
        """Append one licence. Called only after a verified send."""
        when = when or date.today()
        previous = self._find(licence)

        legacy_ids = set(previous.legacy_ids) if previous else set()
        identifier = natural_legacy_id(licence)
        if identifier:
            legacy_ids.add(identifier)

        record = Record(
            key=licence.key,
            source=licence.source.value,
            source_id=licence.source_id,
            licence_id=licence.licence_id,
            legacy_ids=tuple(sorted(legacy_ids)),
            # Preserve the original sighting across a pending -> licensed update.
            first_seen=(previous.first_seen if previous and previous.first_seen else when),
            stage=licence.stage.value,
            content={
                "address": licence.address,
                "neighborhood": licence.neighborhood,
                "species": licence.species,
                "fell": licence.fell_count,
                "relocate": licence.relocate_count,
                "preserve": licence.preserve_count,
                "appeal_last": _iso(licence.appeal_last),
                "published": _iso(licence.published),
                "applicant": licence.applicant,
            },
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")
        self._index(record)
        return record

    # --- migration -------------------------------------------------------
    @classmethod
    def migrate(
        cls,
        legacy_path: Path | None = None,
        out_path: Path | None = None,
    ) -> int:
        """One-time import of ``sent_licenses.txt``. Returns records written.

        Idempotent: legacy ids already present are not written again.
        ``sent_licenses.txt`` is opened read-only and never modified.
        """
        legacy_path = Path(legacy_path) if legacy_path else config.LEGACY_STATE
        state = cls(out_path)

        if not legacy_path.exists():
            raise MigrationError(f"legacy state file not found: {legacy_path}")

        try:
            lines = legacy_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise MigrationError(f"cannot read {legacy_path}: {exc}") from exc

        written = 0
        skipped_null = 0
        unclassified: list[str] = []
        counts = {Source.YEELA: 0, Source.HAIFA_PDF: 0}

        state.path.parent.mkdir(parents=True, exist_ok=True)
        with state.path.open("a", encoding="utf-8") as handle:
            for line in lines:
                identifier = line.strip()
                if not identifier:
                    continue
                source = classify_legacy_id(identifier)
                if source is None:
                    if identifier == "None":
                        skipped_null += 1
                    else:
                        unclassified.append(identifier)
                    continue

                counts[source] += 1
                if identifier in state._legacy.get(source.value, {}):
                    continue  # already migrated

                record = Record(
                    key=legacy_key(source, identifier),
                    source=source.value,
                    source_id="",
                    licence_id=identifier if source is Source.YEELA else None,
                    legacy_ids=(identifier,),
                    # null, so nothing imported is misflagged as back-dated
                    first_seen=None,
                    stage=Stage.LICENSED.value,
                    content={},
                )
                handle.write(record.to_json() + "\n")
                state._index(record)
                written += 1

        log.info(
            "migration: %d records written (%d yeela, %d municipal), "
            "%d 'None' line(s) skipped, %d unclassified",
            written,
            counts[Source.YEELA],
            counts[Source.HAIFA_PDF],
            skipped_null,
            len(unclassified),
        )
        if unclassified:
            log.warning("unclassified legacy ids ignored: %s", unclassified[:20])
        return written
