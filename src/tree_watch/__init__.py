"""Haifa tree-felling licence watch.

Two sources — the municipality's PDF table and the Ministry of Agriculture's
Yeela API — normalised into one :class:`~tree_watch.models.Licence` record,
grouped by neighbourhood and posted to a public Telegram channel.

The legacy entry point ``firsd.py`` at the repository root is still the running
system. Nothing in this package writes to ``sent_licenses.txt``.
"""

__version__ = "0.1.0"
