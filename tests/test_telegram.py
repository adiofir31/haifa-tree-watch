"""Telegram delivery — bugs 1 (the response check), 11 (splitting), 14."""

from __future__ import annotations

import unittest

from tree_watch import config, net, telegram


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class SendHarness(unittest.TestCase):
    def setUp(self):
        self.settings = config.Settings(
            telegram_token="tok", chat_id="-100", operator_chat_id="42"
        )
        self.posted = []
        self._original = telegram.net.post
        telegram.net.post = self._post

    def tearDown(self):
        telegram.net.post = self._original

    def _post(self, url, **kwargs):
        self.posted.append(kwargs.get("data"))
        return self.response_for(len(self.posted))

    def response_for(self, index):
        return FakeResponse({"ok": True, "result": {"message_id": index}})


class TestSplitting(unittest.TestCase):
    """Bug 11: a busy day exceeded 4096 characters and the whole message failed."""

    def test_short_message_is_one_chunk(self):
        self.assertEqual(["hello"], telegram.split_message("hello"))

    def test_empty_message_is_no_chunks(self):
        self.assertEqual([], telegram.split_message(""))

    def test_every_chunk_is_within_the_limit(self):
        text = "\n\n".join(f"📍 <b>שכונה {i}</b>:\n" + "\n".join(
            f"• רחוב מספר {j} | <b>3 עצים</b> (אורן, ברוש)" for j in range(20)
        ) for i in range(40))
        self.assertGreater(len(text), 4096)
        chunks = telegram.split_message(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), config.TELEGRAM_LIMIT)

    def test_split_happens_on_boundaries_not_mid_line(self):
        text = "\n\n".join("x" * 200 for _ in range(60))
        for chunk in telegram.split_message(text):
            for line in chunk.split("\n"):
                self.assertIn(line, ("", "x" * 200))

    def test_nothing_is_lost(self):
        blocks = [f"block {i} " + "y" * 300 for i in range(40)]
        text = "\n\n".join(blocks)
        rejoined = "\n\n".join(telegram.split_message(text))
        for block in blocks:
            self.assertIn(block, rejoined)

    def test_single_line_longer_than_limit_is_hard_split(self):
        chunks = telegram.split_message("z" * 10000)
        self.assertEqual(3, len(chunks))
        self.assertEqual(10000, sum(len(c) for c in chunks))

    def test_exact_limit_is_one_chunk(self):
        text = "a" * config.TELEGRAM_LIMIT
        self.assertEqual(1, len(telegram.split_message(text)))


class TestResponseChecking(SendHarness):
    """Bug 1, enabling half: the old sender never looked at the response."""

    def test_successful_send_returns_results(self):
        results = telegram.send("hello", settings=self.settings)
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(self.posted))

    def test_api_level_failure_raises(self):
        self.response_for = lambda i: FakeResponse(
            {"ok": False, "error_code": 400, "description": "chat not found"}
        )
        with self.assertRaises(telegram.SendFailed) as ctx:
            telegram.send("hello", settings=self.settings)
        self.assertIn("chat not found", str(ctx.exception))

    def test_http_error_raises(self):
        self.response_for = lambda i: FakeResponse({"ok": False}, status=500)
        with self.assertRaises(telegram.SendFailed):
            telegram.send("hello", settings=self.settings)

    def test_non_json_response_raises(self):
        self.response_for = lambda i: FakeResponse(None)
        with self.assertRaises(telegram.SendFailed):
            telegram.send("hello", settings=self.settings)

    def test_network_failure_raises(self):
        def boom(url, **kwargs):
            raise net.FetchError("connection reset")

        telegram.net.post = boom
        with self.assertRaises(telegram.SendFailed):
            telegram.send("hello", settings=self.settings)

    def test_failure_on_a_later_chunk_is_reported_with_progress(self):
        def responses(index):
            if index < 3:
                return FakeResponse({"ok": True, "result": {}})
            return FakeResponse({"ok": False, "error_code": 429, "description": "flood"})

        self.response_for = responses
        text = "\n\n".join("q" * 3000 for _ in range(4))
        with self.assertRaises(telegram.SendFailed) as ctx:
            telegram.send(text, settings=self.settings)
        self.assertIn("2 already delivered", str(ctx.exception))

    def test_missing_credentials_raises_rather_than_silently_passing(self):
        with self.assertRaises(telegram.SendFailed):
            telegram.send("hi", settings=config.Settings())


class TestOperatorNotification(SendHarness):
    """Bug 14: failures go to the operator, never to the public channel."""

    def test_operator_gets_the_message(self):
        telegram.notify_operator("PDF header changed", settings=self.settings)
        self.assertEqual(1, len(self.posted))
        self.assertEqual("42", self.posted[0]["chat_id"])
        self.assertIn("PDF header changed", self.posted[0]["text"])

    def test_refuses_to_post_errors_to_the_public_channel(self):
        settings = config.Settings(
            telegram_token="tok", chat_id="-100", operator_chat_id="-100"
        )
        telegram.notify_operator("boom", settings=settings)
        self.assertEqual([], self.posted)

    def test_never_raises_even_when_sending_fails(self):
        def boom(url, **kwargs):
            raise net.FetchError("down")

        telegram.net.post = boom
        telegram.notify_operator("boom", settings=self.settings)  # must not raise


if __name__ == "__main__":
    unittest.main()
