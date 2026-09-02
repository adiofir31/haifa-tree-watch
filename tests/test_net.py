"""Bug 10: no timeout on any requests call, and no retry."""

from __future__ import annotations

import unittest

import requests

from tree_watch import config, net


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeResponse:
    def __init__(self, status=200):
        self.status_code = status
        self.text = ""


class NetHarness(unittest.TestCase):
    def setUp(self):
        self._original_request = requests.request
        self._original_backoff = config.HTTP_BACKOFF
        config.HTTP_BACKOFF = 0  # no real sleeping in tests

    def tearDown(self):
        requests.request = self._original_request
        config.HTTP_BACKOFF = self._original_backoff

    def install(self, *responses) -> Recorder:
        recorder = Recorder(*responses)
        requests.request = recorder
        return recorder


class TestTimeout(NetHarness):
    def test_every_call_carries_a_timeout(self):
        recorder = self.install(FakeResponse())
        net.get("https://example.test")
        self.assertIn("timeout", recorder.calls[0][2])
        self.assertEqual(config.HTTP_TIMEOUT, recorder.calls[0][2]["timeout"])

    def test_timeout_is_connect_and_read(self):
        self.assertEqual(2, len(config.HTTP_TIMEOUT))
        self.assertTrue(all(t > 0 for t in config.HTTP_TIMEOUT))

    def test_post_also_has_a_timeout(self):
        recorder = self.install(FakeResponse())
        net.post("https://example.test", json={})
        self.assertIn("timeout", recorder.calls[0][2])


class TestRetry(NetHarness):
    def test_transient_failure_is_retried(self):
        recorder = self.install(
            requests.ConnectionError("reset"), FakeResponse()
        )
        response = net.get("https://example.test")
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, len(recorder.calls))

    def test_gives_up_after_the_configured_attempts(self):
        recorder = self.install(*[requests.Timeout("slow")] * config.HTTP_RETRIES)
        with self.assertRaises(net.FetchError):
            net.get("https://example.test")
        self.assertEqual(config.HTTP_RETRIES, len(recorder.calls))

    def test_server_error_is_retried(self):
        recorder = self.install(FakeResponse(503), FakeResponse(200))
        net.get("https://example.test")
        self.assertEqual(2, len(recorder.calls))

    def test_rate_limit_is_retried(self):
        recorder = self.install(FakeResponse(429), FakeResponse(200))
        net.get("https://example.test")
        self.assertEqual(2, len(recorder.calls))

    def test_client_error_is_not_retried(self):
        recorder = self.install(FakeResponse(404))
        with self.assertRaises(net.FetchError):
            net.get("https://example.test")
        self.assertEqual(1, len(recorder.calls), "404 is our bug, not a transient fault")


if __name__ == "__main__":
    unittest.main()
