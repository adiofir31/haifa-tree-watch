# Haifa Tree Watch

Watches for tree-felling licences in Haifa and posts them to a public Telegram
channel, grouped by neighbourhood, while there is still time to object.

Israeli law gives the public 14 days from a licence's publication to file an
objection with the national Forestry Officer. The information is public, but it
is split between a 400-page PDF table on the municipality's website and a
paginated API at the Ministry of Agriculture — and neither tells you which
neighbourhood a street is in. This project merges the two sources, resolves each
address to a neighbourhood, and posts one readable daily digest. It also
publishes a [dashboard](#dashboard) over the whole history.

## How it works

```
municipal PDF ─┐                          ┌─ Telegram digest, grouped by neighbourhood
               ├─ parse ─ resolve ─ state ─┤
Yeela API ─────┘          (geo.py)         └─ docs/data/stats.json ─ dashboard
```

A run at 18:00 fetches both sources, keeps the licences whose objection window
is still open, drops the ones already reported, resolves an address to a
neighbourhood, sends, verifies the send, and only then records what went out.

### Two sources, two text paths

| source | format | quirk |
|---|---|---|
| Haifa municipality | PDF table, ~437 pages | pdfplumber returns it in **visual** order, so `get_display` is required |
| Ministry of Agriculture ("Yeela") | JSON API, ~19 pages | returns **logical** order, so `get_display` must **not** be applied |

This is counterintuitive and tempting to "simplify" into a bug. The parsers are
separate on purpose, and there is a test on each side that fails if the two are
ever merged.

The two also disagree on what a row means and how it is dated. Yeela gives one
row per licence × species with three independent counters (`unproot`, `copying`,
`conservation`) and dates by approval. The municipal table gives one row per
species with the action in a per-tree column, and dates by filing. Tree counts
are the sum over a request's rows, never the maximum.

## Install

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

`requirements-step4.txt` holds `python-dotenv` and `rapidfuzz`. Both are
optional and degrade rather than crash: without `python-dotenv`, settings come
from real environment variables; without `rapidfuzz`, the fuzzy tier of `geo.py`
falls back to `difflib`. That fallback is real but not equivalent — `difflib`
scores on matching characters and penalises a length difference hard, so
`"כאהן"` against `"כאהן יעקב"` scores 61 rather than about 90. `geo.py` logs
which engine it is using at startup.

The index builders under `tools/` additionally need `geopandas`, `pandas` and
`shapely`. The bot itself never loads a geometry library.

## Configure

```bash
copy .env.example .env
```

| variable | meaning |
|---|---|
| `TELEGRAM_TOKEN` | from [@BotFather](https://t.me/botfather) |
| `CHAT_ID` | the public channel, a negative id |
| `OPERATOR_CHAT_ID` | your own user id — failures go here, never to the channel |
| `REPORT_PENDING` | send early warnings for requests that have no licence yet |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

If `OPERATOR_CHAT_ID` is empty or equal to `CHAT_ID`, the bot refuses to post
error messages publicly and writes them to the log instead.

`.env` is gitignored. Nothing in this repository should ever contain a token.

## Run

```bash
run_tree_bot.bat            # what the scheduled task runs
run_tree_bot.bat --dry-run  # everything except sending and writing state
```

The launcher copies `data/state.jsonl` into `data/backups/` before every run,
keeping the last 30, verifies the copy, and carries on if the backup fails — a
missed backup is recoverable, a missed alert is not.

| flag | effect |
|---|---|
| `--dry-run` | print what would be sent; write no state and no `unmatched.csv` |
| `--migrate` | import `sent_licenses.txt` into `data/state.jsonl` and exit |
| `--skip-pdf` / `--skip-yeela` | run one source only |
| `--today YYYY-MM-DD` | pretend it is another date |
| `--log-level` | override `LOG_LEVEL` |

### Reading the log

Every run logs a funnel per source, so a quiet evening can be told apart from a
broken one:

```
yeela: pages fetched: 19 | raw rows: 1848 | requests after aggregation: 502 |
       still open (date filter): 17 | new against state: 11
yeela: skipped as already sent: 6 | pending requests: 104 (new: 0)
TOTAL: skipped as already sent: 22 | new: 4 | updates: 0 | back-dated: 0
```

If a stage reaches zero the log names which one and what fed into it, which
separates "the municipality published nothing" from "the date filter discarded
everything". Logs rotate in `logs/tree_watch.log`.

## State

`data/state.jsonl` records what has already been reported: one JSON object per
line, later lines winning, so a record is updated by appending.

Keys are namespaced by source — `yeela:16191`, `haifa_pdf:2273` — because Yeela
request ids and municipal request numbers occupy the same numeric range and nine
numbers are already both. A bare integer key would let one source's request
silently suppress the other's, and a missed alert is worse than a duplicate.

Yeela is keyed on `requestId` rather than `licenseId`, because a request that is
still under review has no licence id at all. Keying on the request id is what
lets the bot report a pending request early and then report it again, marked as
an update, once the licence is issued and the 14-day clock actually starts.

### Migrating from the legacy file

`sent_licenses.txt` holds the identifiers the original bot had sent. Migration
is idempotent, additive, and never writes to the legacy file:

```bash
run_tree_bot.bat --migrate
```

That file mixes two identifier spaces, separable by length: 7-digit Yeela
`licenseId`s and 4-digit municipal request numbers, plus one line reading `None`
— the `licenseId: null` bug — which is skipped. Legacy identifiers are stored
alongside the namespaced key and consulted within the same source, so no
`licenseId → requestId` mapping is needed and a licence that has since dropped
out of the API window is still recognised.

The bot refuses to run for real while `data/state.jsonl` is missing and
`sent_licenses.txt` is present, because starting from empty state would re-post
months of old licences to a public channel.

## Placing an address

`geo.py` answers from two flat CSVs loaded into memory at startup. All the
spatial work happened offline. The chain, in order:

1. exact street key with the house number inside a range
2. the street-level row for that street
3. a `data/landmarks.csv` pattern matched against the whole licence text
4. block and parcel, via `data/block_index.csv`
5. a fuzzy match, shown in the message as `שכונה משוערת`
6. `שכונה לא ידועה`, with the case appended to `data/unmatched.csv`

Ties are refused rather than guessed, in both the block tier and the fuzzy tier.
`"יעקב"` scores an identical 90 against 32 different streets under `rapidfuzz`,
and picking one of those arbitrarily would be worse than admitting the miss.

`normalize.py` is shared between the bot and the index builders. Both sides must
normalise street names with identical code or the lookup keys never meet, which
is why it lives in the package and the tools import it from there.

### Source layers

The inputs to the builders live in `data/raw/` and are **not committed** — they
total several megabytes and are all obtainable:

| file | what it is | where from |
|---|---|---|
| `addres_haifa.csv` | address points, `street,number,X,Y` in EPSG:2039 | Haifa municipal GIS |
| `schunot.json` | neighbourhood polygons, `SchName` | Haifa municipal GIS |
| `Helkot.geojson` | cadastral parcels with block and parcel numbers | Haifa municipal GIS / Survey of Israel |
| `osm_streets.geojson` | street centrelines | OpenStreetMap, Overpass |

The OSM layer comes from [overpass-turbo](https://overpass-turbo.eu):

```
[out:json][timeout:120];
area["name:he"="חיפה"]["admin_level"="8"]->.haifa;
way["highway"]["name"](area.haifa);
out geom;
```

### Rebuilding the street index

```bash
venv\Scripts\python tools\build_street_index.py ^
    --points        data\raw\addres_haifa.csv ^
    --neighborhoods data\raw\schunot.json ^
    --streets       data\raw\osm_streets.geojson ^
    --out           data\street_index.csv ^
    --legacy-csv    streer_to_neigh.csv --use-legacy-fallback ^
    --min-run 4 --min-support 3 ^
    --neighborhood-aliases data\neighborhood_aliases.csv
```

Four tiers, lower `priority` winning:

| priority | method |
|---|---|
| 1 | house-number range from the address points |
| 2 | street-level majority from the same layer |
| 3 | OSM centreline intersected with the neighbourhood polygons |
| 4 | the legacy `streer_to_neigh.csv` table |
| 5–7 | alias-derived variants of the above |

House-number ranges are what make long streets work. אבא חושי crosses four
neighbourhoods and מוריה three; a single answer per street would be wrong for
most of their length. The current build is 2,081 rows over 1,853 street keys,
with 77 streets crossing more than one neighbourhood.

The builder writes `data/street_index.report.txt` beside the index. Read it —
the multi-neighbourhood section is where the value is, and the word-order
section lists streets the address layer and OSM spell in opposite orders
(`חזן יעקב` against `יעקב חזן`) that were merged in favour of the
better-evidenced tier.

### Rebuilding the block index

Many Yeela licences carry no street at all, only a block (גוש) and parcel
(חלקה) — and these are disproportionately the largest ones, filed by transport
authorities, the railway, the port and the university. This index is the only
way to place them.

```bash
venv\Scripts\python tools\build_block_index.py ^
    --parcels       data\raw\Helkot.geojson ^
    --neighborhoods data\raw\schunot.json ^
    --out           data\block_index.csv ^
    --neighborhood-aliases data\neighborhood_aliases.csv
```

Columns are `block,parcel,neighborhood,priority,method,support`:

| priority | method | meaning |
|---|---|---|
| 1 | `parcel_area` | this block and parcel lie in this neighbourhood |
| 2 | `block_area_majority` | `parcel` empty: the neighbourhood covering most of the block |

`support` is the share of area behind the row. On a priority-2 row it measures
directly how much a block-only answer can be trusted: 116 of the 520 blocks
score below 0.8, and block 11190 is only 34% יזרעאליה with the rest spread over
six other neighbourhoods. Anything under the threshold is reported as
approximate rather than stated.

Yeela writes both fields as free text in inconsistent shapes — `block
"10841,10840"` with `parcel "1,2,7,72"`, or `"1-4,74"`, or `"337-338"`. Commas
separate, a hyphen is an inclusive range, and every block × parcel combination
is tried. If they land in different neighbourhoods nothing is guessed. A hyphen
range wider than 200 parcels is refused rather than expanded.

A licence with no street and no usable block is grouped under `📍 ללא כתובת`,
deliberately not the same heading as `שכונה לא ידועה`. The first means the
source gave no address; the second means it gave one that could not be placed.
They need different fixes, so they are counted separately.

### Extending the placement over time

`data/landmarks.csv` (`pattern,neighborhood,display_name`) starts empty. The
pattern is matched against the whole licence text, not just the street field, so
it is how institutions and road-segment descriptions get placed — a licence
addressed `כביש 4 בין שד' ההגנה לרח' מסילת ישרים` has neither a street nor a
usable block.

Every miss is appended to `data/unmatched.csv` with the date and the original
text. Reviewing that file every few weeks and adding a rule for anything that
recurs is the maintenance loop; it is not a file you fill in once.

## Dashboard

`tools/build_stats.py` pulls the **whole** history from both sources — not just
the licences still open for objection — resolves each one through the same
`geo.py`, and writes one flat record per licence.

```bash
python tools\build_stats.py --self-contained    # one HTML file, open it locally
python tools\build_stats.py                     # docs/data/stats.json for the site
python tools\build_stats.py --from-fixtures     # no network, uses tests/fixtures
```

`docs/index.html` reads that JSON and does all the filtering in the browser:
multi-select by year and neighbourhood, single-select by source and action, a
monthly timeline, ranked neighbourhoods, streets and species, the largest
licences, and a CSV export of whatever is on screen. There is no server and no
database. Published through GitHub Pages from the `docs/` folder.

State is deliberately not the source: it only holds what has been reported since
the cutover, while the sources go back to 2009 for the municipal table and late
2024 for Yeela.

The two sources date things differently and there is no honest way to merge
them, so both dates are carried and the page says which one it is grouping by.

## Tests

No extra install; the suite runs against the real captured fixtures rather than
mocks:

```bash
venv\Scripts\python -m unittest discover -s tests -t .
```

Assertions locked to a fixture count are marked `FIXTURE-LOCKED`, so refreshing
a fixture tells you which numbers to re-check. `pytest`, if installed, collects
the same tests unchanged.

## Layout

```
src/tree_watch/
  config.py         env vars and constants; no secrets in source
  models.py         the one dataclass both sources produce
  normalize.py      Hebrew address normalisation, shared with the builders
  net.py            HTTP with timeouts and retry
  telegram.py       message splitting, delivery, verified responses
  geo.py            address -> neighbourhood
  state.py          JSONL state and migration
  logging_setup.py  rotating file log
  main.py           orchestration
  sources/
    haifa_pdf.py    the municipal PDF table
    yeela.py        the Ministry of Agriculture API
tools/              run by hand, never in production
data/               indexes, aliases, landmarks, state, backups
docs/               the published dashboard
tests/              unittest suite and real fixtures
```

`firsd.py`, `yeela.py`, `sent_licenses.txt` and `streer_to_neigh.csv` at the
root are the original single-file bot, kept as a record of what this replaced.
They are no longer run. The token in `firsd.py` has been revoked.

## Background

The licences this watches are the counterpart to Haifa's own *"תכנית להצללה
באמצעות עצים וייעור עירוני"*, approved in June 2025, which sets a target of at
least 5,000 trees planted in 2026. Reading the felling numbers next to the
planting numbers is most of the point.

## Credits

Built with [Claude Code](https://claude.com/claude-code) — the rewrite from the
original single-file script, the spatial index builders, the test suite and this
dashboard were all developed with it.

## Licence

MIT — see [LICENSE](LICENSE).
