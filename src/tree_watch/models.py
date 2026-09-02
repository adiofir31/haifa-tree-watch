"""The one record shape both sources produce.

The old code passed 16-element lists around, indexed by position, with the two
sources filling different slots. Every column-index bug in the port traces back
to that. One dataclass, named fields, both sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


def clean_block(value: object) -> str:
    """A block or parcel string as printed: trimmed, never the word "None"."""
    text = " ".join(str(value or "").split())
    return "" if text.lower() in ("", "none", "0") else text


class Source(str, Enum):
    HAIFA_PDF = "haifa_pdf"
    YEELA = "yeela"

    @property
    def label(self) -> str:
        return {"haifa_pdf": "עיריית חיפה", "yeela": "מערכת יעלה"}[self.value]


class Action(str, Enum):
    """What is to be done to the tree.

    Both sources distinguish these, and the old bot reported all three as
    felling. Only ``FELL`` belongs in a felling alert.
    """

    FELL = "fell"  # כריתה
    RELOCATE = "relocate"  # העתקה
    PRESERVE = "preserve"  # שימור
    OTHER = "other"  # נדחה, planting notes, anything unrecognised

    @property
    def label(self) -> str:
        return {
            "fell": "כריתה",
            "relocate": "העתקה",
            "preserve": "שימור",
            "other": "אחר",
        }[self.value]


class Stage(str, Enum):
    """Where the request is in its lifecycle.

    A Yeela row with no ``licenseId`` is a request under review: no species, no
    dates, no appeal window. It becomes ``LICENSED`` later, under the same
    ``requestId`` — which is why state is keyed on the request, not the licence.
    """

    PENDING = "pending"
    LICENSED = "licensed"


@dataclass
class SourceStats:
    """Per-source pipeline counters, for comparing a run against the old bot.

    "nothing new" is not a useful log line on its own: it cannot distinguish a
    source that returned nothing from a filter that discarded everything. Each
    stage is counted so the drop-off point is visible.
    """

    source: str
    pages: int = 0
    raw_rows: int = 0
    requests: int = 0
    passed_date_filter: int = 0
    new: int = 0
    skipped_already_sent: int = 0
    pending_requests: int = 0
    pending_new: int = 0
    failed: bool = False

    #: The funnel, in order. Each stage should be <= the one before it.
    STAGE_LABELS = (
        ("pages", "pages fetched"),
        ("raw_rows", "raw rows"),
        ("requests", "requests after aggregation"),
        ("passed_date_filter", "still open (date filter)"),
        ("new", "new against state"),
    )

    @property
    def stages(self) -> list[tuple[str, int]]:
        return [(label, getattr(self, attr)) for attr, label in self.STAGE_LABELS]

    def first_zero(self) -> tuple[str, str, int] | None:
        """The earliest stage that reached zero, and what fed into it.

        Returns ``(label, previous_label, previous_value)``, or ``None`` if
        everything survived to the end.
        """
        previous_label, previous_value = "start", 0
        for label, value in self.stages:
            if value == 0:
                return label, previous_label, previous_value
            previous_label, previous_value = label, value
        return None

    def summary(self) -> str:
        return " | ".join(f"{label}: {value}" for label, value in self.stages)


@dataclass
class Licence:
    """One request, aggregated across all of its species rows."""

    source: Source
    source_id: str
    """The source's own stable identifier.

    Yeela: ``requestId``. Municipal PDF: the request number in column 14.
    Never the Yeela ``licenseId`` — that is null while a request is under
    review, and ``str(None)`` is what poisoned the legacy state file.
    """

    stage: Stage = Stage.LICENSED
    licence_id: str | None = None
    """Yeela's ``licenseId`` once issued. Informational only; never a state key."""

    street: str = ""
    house: str = ""

    # Filled in by geo.lookup(); see geo.py.
    neighborhood: str = ""
    geo_confidence: float = 0.0
    geo_method: str = ""
    #: The placement is a lean rather than a fix — a fuzzy street match or a
    #: block whose dominant neighbourhood covers only part of its area.
    geo_approximate: bool = False

    # Species -> number of trees, counting only what is actually being felled.
    species: dict[str, int] = field(default_factory=dict)
    fell_count: int = 0
    relocate_count: int = 0
    preserve_count: int = 0

    appeal_last: date | None = None
    published: date | None = None
    can_appeal: bool = True

    applicant: str = ""
    block: str = ""
    parcel: str = ""
    reason: str = ""

    @property
    def key(self) -> str:
        """The state key.

        Namespaced by source. The two sources number their requests in
        overlapping ranges — Yeela ``requestId`` 2177 and municipal request 2177
        both exist — so a bare id is not unique across sources.
        """
        return f"{self.source.value}:{self.source_id}"

    @property
    def address(self) -> str:
        street = " ".join(str(self.street).split())
        house = str(self.house).strip()
        if house and house.lower() != "none" and house not in street:
            return f"{street} {house}".strip()
        return street

    @property
    def block_text(self) -> str:
        """"גוש 11539 חלקה 2038", as much of it as the source gave us."""
        block = clean_block(self.block)
        parcel = clean_block(self.parcel)
        if not block:
            return ""
        return f"גוש {block} חלקה {parcel}" if parcel else f"גוש {block}"

    @property
    def location_text(self) -> str:
        """What to print as the licence's location.

        The street when there is one, otherwise the cadastral block. Many Yeela
        licences carry no street at all, and "גוש 11539 חלקה 2038" is a real
        address a reader can look up — far better than an empty bullet.
        """
        return self.address or self.block_text

    @property
    def species_text(self) -> str:
        parts = []
        for name, count in sorted(self.species.items(), key=lambda kv: -kv[1]):
            name = " ".join(str(name).split())
            if not name:
                continue
            parts.append(f"{name} x{count}" if count > 1 else name)
        return ", ".join(parts)

    def merge(self, other: "Licence") -> None:
        """Fold another row of the same request into this one."""
        for name, count in other.species.items():
            self.species[name] = self.species.get(name, 0) + count
        self.fell_count += other.fell_count
        self.relocate_count += other.relocate_count
        self.preserve_count += other.preserve_count

        if not self.street and other.street:
            self.street = other.street
            self.house = other.house
        if other.appeal_last and (
            self.appeal_last is None or other.appeal_last > self.appeal_last
        ):
            self.appeal_last = other.appeal_last
        if other.published and (
            self.published is None or other.published < self.published
        ):
            self.published = other.published
        if other.licence_id and not self.licence_id:
            self.licence_id = other.licence_id
        if other.stage is Stage.LICENSED:
            self.stage = Stage.LICENSED
        # A single "cannot appeal" note on any row disqualifies the request.
        self.can_appeal = self.can_appeal and other.can_appeal
        for attr in ("applicant", "block", "parcel", "reason"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
