"""Street -> neighbourhood lookup.

All the spatial work happened offline in ``tools/build_street_index.py``. This
module reads the flat CSV that produced and answers from a dict: it must never
import geopandas, shapely, or any geometry library.

The chain, in order:

1. exact street key + house number inside a ``house_from..house_to`` range
2. street key alone (the street-level row, ``house_to = 99999``)
2b. token containment — every word of the query appears in exactly one indexed
   street name. Hebrew addresses routinely give a surname only ("כאהן 4" for
   "כאהן יעקב"). Ambiguous containment is never guessed at.
3. ``data/landmarks.csv`` — pattern matched against the **full licence text**,
   not just the street field, for institutions and addresses with house number 0
3b. ``data/block_index.csv`` — cadastral block and parcel, for the many Yeela
   licences that carry no street. Exact block+parcel first, then the block's
   dominant neighbourhood; below ``config.BLOCK_SUPPORT_THRESHOLD`` of area the
   answer is flagged approximate. Never consulted while a street still resolves.
4. fuzzy match above ``config.FUZZY_THRESHOLD``, flagged as approximate
5. ``config.UNKNOWN_NEIGHBORHOOD`` when there was an address we could not place,
   or ``config.NO_ADDRESS`` when the source gave us nothing at all

Both sides of the lookup are normalised by :mod:`tree_watch.normalize` — the
same code ``build_street_index.py`` used to write the keys. If the two ever
diverge the keys stop meeting and every lookup silently falls through to fuzzy.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import config
from .normalize import (
    clean_text,
    normalize_street,
    parse_house_number,
    split_street_house,
    street_keys,
)

log = logging.getLogger(__name__)

STREET_LEVEL_HOUSE_TO = 99999

#: Confidence by index priority: 1 = address-point house range, 2 = street-level
#: majority, 3 = OSM centreline, 4 = legacy table. The builder also emits 5-7
#: for alias-derived variants of the same tiers, so they are mapped too rather
#: than silently collapsing to a default.
PRIORITY_CONFIDENCE = {1: 1.0, 2: 0.9, 3: 0.75, 4: 0.6, 5: 0.85, 6: 0.7, 7: 0.55}


@dataclass(frozen=True)
class Match:
    neighborhood: str
    confidence: float
    method: str
    #: Set when the answer is a best guess rather than a placement: a fuzzy
    #: street match, or a block whose dominant neighbourhood covers less than
    #: ``config.BLOCK_SUPPORT_THRESHOLD`` of its area. The message says so.
    approximate: bool = False

    @property
    def known(self) -> bool:
        return self.neighborhood not in (
            config.UNKNOWN_NEIGHBORHOOD,
            config.NO_ADDRESS,
        )

    @property
    def has_address(self) -> bool:
        """False only when the source gave us nothing to place at all."""
        return self.neighborhood != config.NO_ADDRESS


@dataclass(frozen=True)
class Row:
    house_from: int
    house_to: int
    neighborhood: str
    priority: int
    method: str

    @property
    def is_street_level(self) -> bool:
        """Covers the whole street, so it says nothing about a house number.

        Both bounds matter. ``אבא חושי`` has a priority-1 row ``150-99999``,
        which is a real open-ended house range for the top of the street — not a
        street-level row. Testing ``house_to`` alone would skip it and resolve
        number 200 to whichever range happened to sort first.
        """
        return self.house_from <= 0 and self.house_to >= STREET_LEVEL_HOUSE_TO

    @property
    def width(self) -> int:
        return self.house_to - self.house_from


def fuzzy_engine() -> str:
    """Which similarity backend is actually in use.

    rapidfuzz is optional. difflib is a real fallback, not an equivalent one: it
    scores on the ratio of matching characters, so it penalises a length
    difference hard — "כאהן" against "כאהן יעקב" scores about 61, well under the
    threshold, where rapidfuzz's partial matching scores it near 100. The
    difference decides whether a street resolves, so it is logged at startup
    rather than left to be discovered.
    """
    try:
        import rapidfuzz  # noqa: F401

        return "rapidfuzz"
    except ModuleNotFoundError:
        return "difflib"


def _similarity(query: str, choices: list[str], limit: int = 40) -> list[tuple[str, float]]:
    """Best fuzzy candidates, highest score first.

    A ranked list rather than a single winner, because the top score is often a
    tie. rapidfuzz's WRatio scores a short query at 90 against *every* longer
    name containing it: "יעקב" ties at 90 across 32 different streets. Taking
    the first of those would be a coin flip dressed up as a 0.90 confidence.
    """
    if not query or not choices:
        return []
    if fuzzy_engine() == "rapidfuzz":
        from rapidfuzz import process, fuzz

        hits = process.extract(query, choices, scorer=fuzz.WRatio, limit=limit)
        return [(name, float(score)) for name, score, _ in hits]

    import difflib

    scored = [
        (choice, difflib.SequenceMatcher(None, query, choice).ratio() * 100)
        for choice in choices
    ]
    scored.sort(key=lambda item: -item[1])
    return scored[:limit]


def parse_id_list(text: object, max_range: int = None) -> list[str]:
    """Split a Yeela block or parcel field into individual numbers.

    The field is free text and its shapes are not consistent. All of these are
    real, taken from production logs::

        block "10841,10840"     parcel "1,2,7,72"
        block "10841,11115"     parcel "1-4,74"
        block "11697"           parcel "10-12"
        block "12255"           parcel "337-338"
        block "10903"           parcel "86,114"

    Commas separate, a hyphen is an inclusive range. Order is preserved and
    duplicates are dropped. A range wider than ``max_range`` is refused rather
    than expanded, so that malformed input such as "1-999999" cannot turn into
    a million-iteration loop.
    """
    max_range = config.MAX_PARCEL_RANGE if max_range is None else max_range
    raw = clean_text(text)
    if not raw:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            out.append(value)

    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            low, high = low.strip(), high.strip()
            if low.isdigit() and high.isdigit():
                start, end = int(low), int(high)
                if start > end:
                    start, end = end, start
                if end - start + 1 > max_range:
                    log.warning(
                        "refusing to expand range %r (%d values, limit %d)",
                        part,
                        end - start + 1,
                        max_range,
                    )
                    add(low)
                    continue
                for number in range(start, end + 1):
                    add(str(number))
                continue
        if part.isdigit():
            add(str(int(part)))  # strip leading zeros so keys meet
        else:
            add(part)
    return out


class GeoIndex:
    """Loaded once at startup; answers street lookups from memory."""

    def __init__(
        self,
        index_path: Path | None = None,
        aliases_path: Path | None = None,
        landmarks_path: Path | None = None,
        unmatched_path: Path | None = None,
        block_index_path: Path | None = None,
    ):
        self.index_path = Path(index_path) if index_path else config.STREET_INDEX
        self.block_index_path = (
            Path(block_index_path) if block_index_path else config.BLOCK_INDEX
        )
        self.aliases_path = Path(aliases_path) if aliases_path else config.NEIGHBORHOOD_ALIASES
        self.landmarks_path = Path(landmarks_path) if landmarks_path else config.LANDMARKS
        self.unmatched_path = Path(unmatched_path) if unmatched_path else config.UNMATCHED_LOG

        self.aliases: dict[str, str] = {}
        self.dropped: set[str] = set()
        self.streets: dict[str, list[Row]] = {}
        self._key_tokens: list[tuple[str, frozenset[str]]] = []
        self.landmarks: list[tuple[str, str, str]] = []
        self.block_parcels: dict[tuple[str, str], tuple[str, float]] = {}
        self.block_majority: dict[str, tuple[str, float]] = {}
        self._unmatched_seen: set[tuple[str, str]] = set()

        self._load_aliases()
        self._load_index()
        self._load_blocks()
        self._load_landmarks()
        self._load_unmatched()

    # --- loading ---------------------------------------------------------
    def _load_aliases(self) -> None:
        if not self.aliases_path.exists():
            log.warning("no alias file at %s", self.aliases_path)
            return
        with self.aliases_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(_strip_comments(handle)):
                alias = clean_text(row.get("alias"))
                canonical = clean_text(row.get("canonical"))
                if not alias:
                    continue
                if canonical:
                    self.aliases[alias] = canonical
                else:
                    # Empty right column means "not a place" — road corridors.
                    self.dropped.add(alias)
        log.info(
            "geo: %d neighbourhood aliases, %d dropped names",
            len(self.aliases),
            len(self.dropped),
        )

    def canonical(self, name: str) -> str:
        name = clean_text(name)
        seen = set()
        while name in self.aliases and name not in seen:
            seen.add(name)
            name = self.aliases[name]
        return name

    def _load_index(self) -> None:
        if not self.index_path.exists():
            raise FileNotFoundError(
                f"street index not found at {self.index_path} — "
                "run tools/build_street_index.py"
            )
        rows = 0
        with self.index_path.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                key = clean_text(raw.get("street_key"))
                neighborhood = self.canonical(raw.get("neighborhood"))
                if not key or not neighborhood or neighborhood in self.dropped:
                    continue
                try:
                    row = Row(
                        house_from=int(raw.get("house_from") or 0),
                        house_to=int(raw.get("house_to") or STREET_LEVEL_HOUSE_TO),
                        neighborhood=neighborhood,
                        priority=int(raw.get("priority") or 4),
                        method=clean_text(raw.get("method")),
                    )
                except ValueError:
                    log.warning("unparseable index row for %r", key)
                    continue
                self.streets.setdefault(key, []).append(row)
                rows += 1

        for entries in self.streets.values():
            # Lower priority first; among equals prefer the narrower range.
            entries.sort(key=lambda r: (r.priority, r.width))

        # Token sets for the subset tier, computed once.
        self._key_tokens = [(key, frozenset(key.split())) for key in self.streets]

        log.info("geo: %d index rows over %d street keys", rows, len(self.streets))
        engine = fuzzy_engine()
        log.info(
            "geo: fuzzy engine = %s%s",
            engine,
            ""
            if engine == "rapidfuzz"
            else " (rapidfuzz not installed — difflib penalises length "
            "differences, so partial street names are far less likely to match; "
            "see requirements-step4.txt)",
        )

    def _load_landmarks(self) -> None:
        if not self.landmarks_path.exists():
            log.info("no landmarks file at %s", self.landmarks_path)
            return
        with self.landmarks_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(_strip_comments(handle)):
                pattern = clean_text(row.get("pattern"))
                neighborhood = self.canonical(row.get("neighborhood"))
                if not pattern or not neighborhood:
                    continue
                self.landmarks.append(
                    (pattern, neighborhood, clean_text(row.get("display_name")))
                )
        log.info("geo: %d landmark patterns", len(self.landmarks))

    def _load_unmatched(self) -> None:
        """Remember misses already on disk.

        Without this the same unresolvable streets are appended on every run and
        the file grows without bound, which makes it useless for curation.
        """
        if not self.unmatched_path.exists():
            return
        try:
            with self.unmatched_path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    self._unmatched_seen.add(
                        (clean_text(row.get("street")), clean_text(row.get("house")))
                    )
        except OSError as exc:
            log.warning("could not read %s: %s", self.unmatched_path, exc)

    def _load_blocks(self) -> None:
        """Load the cadastral index: block+parcel, and block-level majorities.

        Built offline by ``tools/build_block_index.py``. Priority 1 rows are
        parcel polygons (``parcel_area``); priority 2 rows are one per block
        (``block_area_majority``), where ``support`` is the share of the block's
        area falling in its dominant neighbourhood.
        """
        if not self.block_index_path.exists():
            log.info(
                "no block index at %s — licences without a street cannot be "
                "placed",
                self.block_index_path,
            )
            return

        weak = 0
        with self.block_index_path.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                block = clean_text(raw.get("block"))
                neighborhood = self.canonical(raw.get("neighborhood"))
                if not block or not neighborhood or neighborhood in self.dropped:
                    continue
                try:
                    support = float(raw.get("support") or 0.0)
                    priority = int(raw.get("priority") or 2)
                except ValueError:
                    log.warning("unparseable block index row for block %r", block)
                    continue

                parcel = clean_text(raw.get("parcel"))
                if priority == 1 and parcel:
                    self.block_parcels[(block, parcel)] = (neighborhood, support)
                elif not parcel:
                    self.block_majority[block] = (neighborhood, support)
                    if support < config.BLOCK_SUPPORT_THRESHOLD:
                        weak += 1

        log.info(
            "geo: %d parcels over %d blocks (%d blocks below the %.0f%% support "
            "threshold, reported as approximate)",
            len(self.block_parcels),
            len(self.block_majority),
            weak,
            config.BLOCK_SUPPORT_THRESHOLD * 100,
        )

    # --- block lookup ----------------------------------------------------
    def lookup_block(self, block: object, parcel: object = None) -> Match | None:
        """Place a licence by cadastral block and parcel.

        Only ever consulted when the street failed: a street with a house number
        is more precise than a block, and a block can span several
        neighbourhoods.
        """
        blocks = parse_id_list(block)
        parcels = parse_id_list(parcel)
        if not blocks:
            return None

        # (a) exact block+parcel. Every combination is tried, because the two
        # fields are parallel lists that do not pair up positionally.
        hits: list[tuple[str, str, str]] = []
        for one_block in blocks:
            for one_parcel in parcels:
                found = self.block_parcels.get((one_block, one_parcel))
                if found:
                    hits.append((one_block, one_parcel, found[0]))

        if hits:
            neighborhoods = {n for _, _, n in hits}
            if len(neighborhoods) == 1:
                return Match(hits[0][2], 0.95, "block_parcel")
            # Split across neighbourhoods: do not pick one. Fall back to the
            # block majority, flagged approximate.
            log.info(
                "geo: block/parcel %r/%r spans %d neighbourhoods (%s) — "
                "falling back to the block majority",
                block,
                parcel,
                len(neighborhoods),
                ", ".join(sorted(neighborhoods)),
            )
            return self._block_majority_match(blocks, force_approximate=True)

        return self._block_majority_match(blocks)

    def _block_majority_match(
        self, blocks: list[str], force_approximate: bool = False
    ) -> Match | None:
        """(b) and (c): the block alone, on the first block we actually know."""
        for one_block in blocks:
            found = self.block_majority.get(one_block)
            if not found:
                continue
            neighborhood, support = found
            if support >= config.BLOCK_SUPPORT_THRESHOLD:
                return Match(
                    neighborhood, 0.8, "block_majority", approximate=force_approximate
                )
            # The dominant neighbourhood covers less than the threshold: the
            # answer is a lean, not a placement. 116 of 520 blocks are here.
            return Match(
                neighborhood, support, "block_majority_weak", approximate=True
            )
        log.info("geo: no block index entry for %r", blocks[:4])
        return None

    # --- lookup ----------------------------------------------------------
    def _house_number(self, street: str, house: object) -> int | None:
        """The house number, from its own column or from inside the street text.

        Yeela has no house-number field — it writes "שדרות מוריה 1" into
        ``street``. The municipal PDF keeps the number in column 5. Both arrive
        here, so both have to work.
        """
        if house not in (None, ""):
            number = parse_house_number(house)
            if number is not None:
                return number
        _, embedded = split_street_house(street)
        return parse_house_number(embedded) if embedded else None

    def _resolve(self, key: str, number: int | None) -> tuple[Row, bool] | None:
        """Best row for a known street key, and whether a house range matched."""
        entries = self.streets.get(key)
        if not entries:
            return None
        if number is not None:
            for row in entries:
                if not row.is_street_level and row.house_from <= number <= row.house_to:
                    return row, True
        # No house number, or none of the ranges contained it: use the row that
        # describes the whole street.
        street_level = next((r for r in entries if r.is_street_level), None)
        if street_level is not None:
            return street_level, False

        # No whole-street row exists, so entries[0] is the narrowest priority-1
        # range — one specific stretch of road, which is the same bad guess the
        # house-range fix removed from the main path. Warn so we find out
        # whether it happens on real data and can fix the index instead.
        fallback = entries[0]
        log.warning(
            "geo: %r has no street-level row; falling back to the narrowest "
            "range %d-%d -> %r (house number %s). Consider adding a "
            "street-level row to the index.",
            key,
            fallback.house_from,
            fallback.house_to,
            fallback.neighborhood,
            number if number is not None else "unknown",
        )
        return fallback, False

    def lookup(
        self,
        street: str,
        house: object = None,
        full_text: str = "",
        block: object = None,
        parcel: object = None,
    ) -> Match:
        number = self._house_number(street, house)

        # 1 + 2: exact key, house range first, then the street-level row.
        for key in street_keys(street):
            hit = self._resolve(key, number)
            if hit is None:
                continue
            row, in_range = hit
            confidence = PRIORITY_CONFIDENCE.get(row.priority, 0.5)
            if not in_range and number is not None:
                confidence *= 0.95  # we had a number and could not place it
            return Match(
                row.neighborhood,
                confidence,
                f"house_range_p{row.priority}" if in_range else f"street_level_p{row.priority}",
            )

        # 2b: token containment. Referring to a street by surname alone is
        # normal in Hebrew — a licence says "כאהן 4" where the index has
        # "כאהן יעקב". Accept it only when exactly one indexed street contains
        # every word of the query; more than one and we would be guessing, so
        # fall through rather than pick.
        for key in street_keys(street):
            query_tokens = frozenset(key.split())
            if not query_tokens:
                continue
            contained = [k for k, tokens in self._key_tokens if query_tokens <= tokens]
            if len(contained) == 1:
                row, in_range = self._resolve(contained[0], number)
                log.info(
                    "geo: %r matched %r by token containment", key, contained[0]
                )
                return Match(
                    row.neighborhood,
                    PRIORITY_CONFIDENCE.get(row.priority, 0.5) * 0.9,
                    f"token_subset_p{row.priority}",
                )
            if len(contained) > 1:
                log.info(
                    "geo: %r is contained in %d streets (%s...) — too ambiguous, "
                    "falling through to fuzzy",
                    key,
                    len(contained),
                    ", ".join(sorted(contained)[:3]),
                )

        # 3: landmarks, matched against the whole licence text.
        haystack = clean_text(f"{full_text} {street}")
        for pattern, neighborhood, _display in self.landmarks:
            if pattern and pattern in haystack:
                return Match(neighborhood, 0.85, "landmark")

        # 3b: cadastral block and parcel. Only reached when the street failed —
        # a street with a house number is more precise, and a block can span
        # several neighbourhoods. Many Yeela licences carry no street at all but
        # always carry a block, which is the only way to place them.
        block_match = self.lookup_block(block, parcel)
        if block_match is not None:
            return block_match

        # 4: fuzzy — then resolve the house number against the matched street,
        # exactly as an exact hit would.
        query = normalize_street(street)
        if query and self.streets:
            ranked = _similarity(query, list(self.streets))
            ranked = [(n, sc) for n, sc in ranked if sc >= config.FUZZY_THRESHOLD]
            if ranked:
                top_score = ranked[0][1]
                # Everything effectively tied with the winner. If they all point
                # at one neighbourhood the answer is safe even though the street
                # is uncertain; if they disagree, this is a guess and we decline
                # — the same rule the token-containment tier applies.
                tied = [
                    n for n, sc in ranked if top_score - sc <= config.FUZZY_TIE_MARGIN
                ]
                resolved = {self._resolve(n, number)[0].neighborhood for n in tied}
                if len(resolved) > 1:
                    log.info(
                        "geo: fuzzy match for %r is tied across %d streets in %d "
                        "neighbourhoods (%s) — declining to guess",
                        query,
                        len(tied),
                        len(resolved),
                        ", ".join(sorted(resolved)[:3]),
                    )
                else:
                    candidate = ranked[0][0]
                    row, in_range = self._resolve(candidate, number)
                    tier = "house_range" if in_range else "street_level"
                    return Match(
                        row.neighborhood,
                        top_score / 100.0,
                        f"fuzzy_{tier}_p{row.priority}",
                        approximate=True,
                    )

        # 5: give up. Two different failures, kept apart on purpose: an address
        # we could not place is an indexing gap to fix, while no address at all
        # is a source-data gap. Collapsing them into one bucket hides which.
        if not clean_text(street):
            return Match(config.NO_ADDRESS, 0.0, "no_address")
        return Match(config.UNKNOWN_NEIGHBORHOOD, 0.0, "unknown")

    # --- misses ----------------------------------------------------------
    def record_unmatched(self, street: str, house: object, full_text: str) -> None:
        """Append a miss to ``data/unmatched.csv``, once per street+house, ever.

        A blank street is not recorded: some Yeela rows carry no address at all,
        and a row of empty columns tells whoever curates ``landmarks.csv``
        nothing they can act on.
        """
        street = clean_text(street)
        house_text = clean_text(house)
        if not street:
            log.info("licence with no address at all, not recorded: %r", full_text[:80])
            return
        signature = (street, house_text)
        if signature in self._unmatched_seen:
            return
        self._unmatched_seen.add(signature)

        try:
            self.unmatched_path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.unmatched_path.exists()
            with self.unmatched_path.open("a", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                if new:
                    writer.writerow(["first_seen", "street", "house", "full_text"])
                writer.writerow(
                    [date.today().isoformat(), street, house_text, clean_text(full_text)]
                )
        except OSError as exc:
            # A logging convenience must never take the run down.
            log.warning("could not record unmatched street %r: %s", street, exc)


def _strip_comments(handle):
    """Yield the header plus every non-comment line.

    ``neighborhood_aliases.csv`` is hand-maintained and documents itself with
    ``#`` lines under the header.
    """
    for line in handle:
        if not line.lstrip().startswith("#"):
            yield line
