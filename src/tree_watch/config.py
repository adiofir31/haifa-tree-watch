"""Configuration: environment variables and constants.

No secrets in source. ``TELEGRAM_TOKEN`` and ``CHAT_ID`` were hardcoded in
``firsd.py``; here they come from the environment, loaded from a ``.env`` file
that is gitignored. See ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Repository root: src/tree_watch/config.py -> src/tree_watch -> src -> root
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

# --- data files -------------------------------------------------------------
STREET_INDEX = DATA_DIR / "street_index.csv"
BLOCK_INDEX = DATA_DIR / "block_index.csv"
NEIGHBORHOOD_ALIASES = DATA_DIR / "neighborhood_aliases.csv"
LANDMARKS = DATA_DIR / "landmarks.csv"
UNMATCHED_LOG = DATA_DIR / "unmatched.csv"
STATE_FILE = DATA_DIR / "state.jsonl"

# The legacy state file. Read once by the migration, never written.
LEGACY_STATE = ROOT / "sent_licenses.txt"

# --- sources ----------------------------------------------------------------
PDF_URL = (
    "https://www3.haifa.muni.il/trees/"
    "%D7%A8%D7%A9%D7%99%D7%9E%D7%AA%20%D7%91%D7%A7%D7%A9%D7%95%D7%AA.pdf"
)
YEELA_URL = (
    "https://yeela-trees.moag.gov.il/api/Fo/FOServiceRequest/"
    "getFOGridPublicityLicenses"
)
YEELA_CITY_ID = 4000  # Haifa
YEELA_PAGE_SIZE = 100
YEELA_MAX_PAGES = 50
YEELA_PAGE_DELAY = 0.3  # seconds between pages, to be polite to the ministry

YEELA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) "
        "Gecko/20100101 Firefox/147.0"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "userOrgRoleId": "",
    "Origin": "https://yeela-trees.moag.gov.il",
    "Referer": "https://yeela-trees.moag.gov.il/FoPublic/FoLicence",
}

# --- network ----------------------------------------------------------------
# Every requests call gets a timeout. Under Task Scheduler a hung process is
# invisible, so "wait forever" is the one behaviour we cannot allow.
HTTP_TIMEOUT = (10, 60)  # (connect, read)
HTTP_RETRIES = 3
HTTP_BACKOFF = 2.0  # seconds, doubled per attempt

TELEGRAM_LIMIT = 4096
TELEGRAM_API = "https://api.telegram.org"

# --- behaviour --------------------------------------------------------------
BACKDATED_AFTER_DAYS = 5  # a licence older than this when first seen is flagged
FUZZY_THRESHOLD = 88  # rapidfuzz score above which a street match is accepted

#: Fuzzy candidates within this many points of the winner count as tied. When
#: tied streets sit in different neighbourhoods the match is refused rather than
#: guessed: rapidfuzz scores a short query at 90 against every longer name that
#: contains it, so "יעקב" ties across 32 streets.
FUZZY_TIE_MARGIN = 2

#: A block-only match below this share of area is reported as approximate.
#: 116 of the 520 blocks are under it, so this is not an edge case.
BLOCK_SUPPORT_THRESHOLD = 0.8

#: Largest hyphen range we will expand from a parcel string, so malformed input
#: such as "1-999999" cannot blow up a loop.
MAX_PARCEL_RANGE = 200

#: The address exists but we could not place it.
UNKNOWN_NEIGHBORHOOD = "שכונה לא ידועה"

#: There is no address in the source at all — a different failure with a
#: different fix. Keeping the two apart is the only way to know which to chase.
NO_ADDRESS = "ללא כתובת"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings, resolved from the environment."""

    telegram_token: str = ""
    chat_id: str = ""
    operator_chat_id: str = ""  # failures go here, never to the public channel
    report_pending: bool = False  # Yeela early-warning messages (brief step 4)
    log_level: str = "INFO"

    missing: tuple[str, ...] = field(default=(), compare=False)

    @property
    def can_send(self) -> bool:
        return bool(self.telegram_token and self.chat_id)


def load_settings(require: bool = True) -> Settings:
    """Read settings from the environment, loading ``.env`` if present.

    ``require=False`` is used by ``--dry-run`` and by the tests, which must work
    on a machine with no credentials at all.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ModuleNotFoundError:  # pragma: no cover - dotenv is a hard dep
        pass

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("CHAT_ID", "").strip()
    operator = os.environ.get("OPERATOR_CHAT_ID", "").strip()

    missing = tuple(
        name
        for name, value in (("TELEGRAM_TOKEN", token), ("CHAT_ID", chat))
        if not value
    )
    if missing and require:
        raise RuntimeError(
            "missing required environment variables: "
            + ", ".join(missing)
            + " — copy .env.example to .env and fill it in"
        )

    return Settings(
        telegram_token=token,
        chat_id=chat,
        operator_chat_id=operator or chat,
        report_pending=_env_flag("REPORT_PENDING", False),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        missing=missing,
    )
