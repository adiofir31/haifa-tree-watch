"""Logging to a rotating file plus the console.

The old code printed. Under Task Scheduler nothing is attached to stdout, so
six months of runs left no record of what happened on any of them.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import config

FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"


def use_utf8_stdio() -> None:
    """Force UTF-8 on stdout/stderr.

    A Windows console defaults to a legacy code page — cp1255 on this machine —
    which cannot encode the emoji in the messages. Printing one then raises
    UnicodeEncodeError and takes the whole run down. run_tree_bot.bat sets
    PYTHONIOENCODING, but nothing guarantees that for someone running the module
    by hand, so it is enforced here too.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # already detached, or not a real tty
                pass


def setup(level: str = "INFO", log_dir=None) -> None:
    use_utf8_stdio()
    log_dir = log_dir or config.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(FORMAT)

    file_handler = RotatingFileHandler(
        log_dir / "tree_watch.log",
        maxBytes=2_000_000,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # pdfplumber's pdfminer backend logs a warning per malformed object.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
