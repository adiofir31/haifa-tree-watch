"""Telegram delivery: timeouts, retries, splitting, and a checked response.

The old ``send_telegram_msg`` posted with no timeout, wrapped everything in a
bare ``except``, and never looked at the response. A failed send was
indistinguishable from a successful one — and history had already been written
by then, so the licence was marked sent forever.

Here a failure raises. Nothing persists state unless the send returned ok.
"""

from __future__ import annotations

import html
import logging

from . import config, net

log = logging.getLogger(__name__)


class SendFailed(RuntimeError):
    """Telegram did not accept the message."""


def split_message(text: str, limit: int = config.TELEGRAM_LIMIT) -> list[str]:
    """Split into chunks under ``limit``, preferring natural boundaries.

    Tries paragraph breaks, then single lines, and only as a last resort cuts a
    line that is itself longer than the limit. A busy day used to fail the whole
    message; now it becomes several.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.rstrip("\n"))
        current = ""

    for block in text.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        flush()
        if len(block) <= limit:
            current = block
            continue
        # The paragraph alone is too long: fall back to lines.
        for line in block.split("\n"):
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= limit:
                current = candidate
                continue
            flush()
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
    flush()
    return chunks


def send(
    text: str,
    *,
    settings: config.Settings,
    chat_id: str | None = None,
    parse_mode: str = "HTML",
    disable_preview: bool = True,
) -> list[dict]:
    """Send ``text``, split if needed. Raises :class:`SendFailed` on any failure.

    Returns the API results, one per chunk. If chunk 2 of 3 fails, the exception
    carries how many got through, so the caller can decide what to persist.
    """
    if not settings.can_send:
        raise SendFailed("no Telegram credentials configured")

    chat = chat_id or settings.chat_id
    url = f"{config.TELEGRAM_API}/bot{settings.telegram_token}/sendMessage"
    chunks = split_message(text)
    results: list[dict] = []

    for index, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": chat,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        try:
            response = net.post(url, data=payload)
        except net.FetchError as exc:
            raise SendFailed(
                f"chunk {index}/{len(chunks)} failed after retries "
                f"({len(results)} already delivered): {exc}"
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise SendFailed(
                f"chunk {index}/{len(chunks)}: response was not JSON "
                f"({len(results)} already delivered)"
            ) from exc

        # The check the old code never made.
        if not response.ok or not body.get("ok"):
            raise SendFailed(
                f"chunk {index}/{len(chunks)} rejected: "
                f"{body.get('error_code')} {body.get('description')!r} "
                f"({len(results)} already delivered)"
            )
        results.append(body)
        log.info("telegram: chunk %d/%d delivered to %s", index, len(chunks), chat)

    return results


def notify_operator(text: str, *, settings: config.Settings) -> None:
    """Report a failure to the operator, never to the public channel.

    Best effort by definition: this runs when something has already gone wrong,
    so it must not raise and mask the original error.
    """
    target = settings.operator_chat_id
    if not target or not settings.telegram_token:
        log.error("no operator chat configured; failure not reported: %s", text)
        return
    if target == settings.chat_id:
        log.error(
            "OPERATOR_CHAT_ID is unset or equal to CHAT_ID — refusing to post an "
            "error to the public channel. Message was: %s",
            text,
        )
        return
    try:
        send(f"⚠️ <b>tree-watch</b>\n<pre>{html.escape(text)}</pre>",
             settings=settings, chat_id=target)
    except Exception as exc:  # noqa: BLE001 - must never mask the real failure
        log.error("could not notify operator: %s", exc)
