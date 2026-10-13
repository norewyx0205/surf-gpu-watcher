"""Behavior tests; all responses and mail transports are synthetic."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

import surf_watch as w


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {
            "_dir": self.root, "endpoint": w.ENDPOINT, "status_key": "a10", "min_free": 1,
            "interval_seconds": 60, "timeout_seconds": 20,
            "token_file": "token.txt", "state_file": "state.json",
            "smtp": {"host": "mail.example.com", "port": 465, "security": "ssl",
                     "from": "sender@example.com", "to": "recipient@example.com", "username": ""},
        }

    def test_exact_model_selection_and_zero_capacity(self):
        self.assertEqual(w.availability({"a10": 0, "rtx2080": True, "a100": 4}, "A10")[0], False)
        self.assertEqual(w.availability({"A10": 2}, "a10", 2), (True, "A10", 2))
        self.assertFalse(w.availability({"a10": 1}, "a10", 2)[0])
        self.assertTrue(w.availability({"a10": True}, "a10")[0])
        self.assertFalse(w.availability({"a10": False}, "a10")[0])

    def test_missing_or_ambiguous_key_never_means_available(self):
        for payload in ({"a100": True}, {"A10": True, "a10": False}, {}):
            with self.subTest(payload=payload), self.assertRaises(w.WatchError):
                w.availability(payload, "a10")

    def test_unexpected_types_are_not_truthy(self):
        for value in ("false", "true", "0", None, -1, 1.0, {"available": True}, [True]):
            with self.subTest(value=value), self.assertRaises(w.WatchError):
                w.availability({"a10": value}, "a10")

    def test_boolean_cannot_confirm_two_gpus(self):
        with self.assertRaises(w.WatchError):
            w.availability({"a10": True}, "a10", 2)

    def test_wait_send_stop_and_deduplicate_across_restart(self):
        fetch = Mock(side_effect=[{"a10": False}, {"a10": 0}, {"a10": True}])
        send, sleep = Mock(), Mock()
        w.watch(self.cfg, fetch=fetch, send=send, sleep=sleep)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(60), unittest.mock.call(60)])
        self.assertTrue(w.read_json(self.root / "state.json")["notified_at"])
        second_fetch, second_send = Mock(), Mock()
        w.watch(self.cfg, fetch=second_fetch, send=second_send, sleep=Mock())
        second_fetch.assert_not_called()
        second_send.assert_not_called()

    def test_initially_available_sends_immediately(self):
        send, sleep = Mock(), Mock()
        w.watch(self.cfg, fetch=lambda cfg: {"a10": True}, send=send, sleep=sleep)
        send.assert_called_once()
        sleep.assert_not_called()

    def test_email_failure_preserves_opportunity_and_rechecks_capacity(self):
        fetch = Mock(side_effect=[{"a10": True}, {"a10": False}, {"a10": True}])
        sleep = Mock()
        observed_state = []

        def send(cfg, key, value):
            observed_state.append(w.read_json(self.root / "state.json")["notified_at"])
            if len(observed_state) == 1:
                raise w.WatchError("temporary SMTP error", retryable=True)

        with patch.object(w.random, "uniform", return_value=0):
            w.watch(self.cfg, fetch=fetch, send=send, sleep=sleep)
        self.assertEqual(observed_state, [None, None])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [120, 60])
        self.assertTrue(w.read_json(self.root / "state.json")["notified_at"])

    def test_rate_limit_retry_after_is_respected_without_notification(self):
        fetch = Mock(side_effect=[w.WatchError("429", retryable=True, retry_after=3600),
                                  {"a10": True}])
        send, sleep = Mock(), Mock()
        with patch.object(w.random, "uniform", return_value=0):
            w.watch(self.cfg, fetch=fetch, send=send, sleep=sleep)
        sleep.assert_called_once_with(3600)
        send.assert_called_once()

    def test_auth_failure_does_not_send_or_mark_notified(self):
        send, sleep = Mock(), Mock()
        with self.assertRaises(w.WatchError):
            w.watch(self.cfg, fetch=Mock(side_effect=w.WatchError("401")), send=send, sleep=sleep)
        send.assert_not_called()
        sleep.assert_not_called()
        self.assertIsNone(w.read_json(self.root / "state.json")["notified_at"])

    def test_changed_recipient_cannot_reuse_completed_task(self):
        w.watch(self.cfg, fetch=lambda cfg: {"a10": True}, send=Mock(), sleep=Mock())
        self.cfg["smtp"]["to"] = "other@example.com"
        with self.assertRaises(w.WatchError):
            w.watch(self.cfg, fetch=Mock(), send=Mock(), sleep=Mock())

    def test_corrupt_state_stops_instead_of_resending(self):
        (self.root / "state.json").write_text("{broken")
        send = Mock()
        with self.assertRaises(w.WatchError):
            w.watch(self.cfg, fetch=Mock(), send=send, sleep=Mock())
        send.assert_not_called()

    def test_same_state_cannot_be_used_concurrently(self):
        path = self.root / "state.json"
        with w.state_lock(path):
            with self.assertRaises(w.WatchError):
                with w.state_lock(path):
                    self.fail("second process should not get the lock")
        with w.state_lock(path):
            pass

    def test_disk_failure_after_mail_acceptance_stops_without_retry(self):
        send, sleep = Mock(), Mock()
        original = w.private_write
        calls = 0

        def fail_third(path, text):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("disk full")
            original(path, text)

        with patch.object(w, "private_write", side_effect=fail_third), self.assertRaises(w.WatchError):
            w.watch(self.cfg, fetch=lambda cfg: {"a10": True}, send=send, sleep=sleep)
        send.assert_called_once()
        sleep.assert_not_called()

    def test_query_uses_raw_token_get_and_reloads_file(self):
        (self.root / "token.txt").write_text("synthetic-test-token\n")
        response = io.BytesIO(b'{"a10": false}')
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        with patch.dict(os.environ, {}, clear=True), patch.object(w.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(w.fetch_status(self.cfg), {"a10": False})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "synthetic-test-token")
            self.assertEqual(request.full_url, w.ENDPOINT)
            (self.root / "token.txt").write_text("rotated-synthetic-token\n")
            response2 = io.BytesIO(b'{"a10": true}')
            response2.status = 200
            opener.open.return_value = response2
            w.fetch_status(self.cfg)
            self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "rotated-synthetic-token")

    def test_api_http_errors_never_leak_response_or_token(self):
        for code, retryable in ((401, False), (403, False), (302, False), (429, True), (503, True)):
            error = urllib.error.HTTPError(w.ENDPOINT, code, "error", {"Retry-After": "300"},
                                           io.BytesIO(b"sensitive response should not be printed"))
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(code=code), patch.dict(os.environ, {"RESEARCH_CLOUD_TOKEN": "synthetic-token"}), \
                    patch.object(w.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(w.WatchError) as caught:
                    w.fetch_status(self.cfg)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn("sensitive", str(caught.exception))
                self.assertNotIn("synthetic-token", str(caught.exception))
                if retryable:
                    self.assertEqual(caught.exception.retry_after, 300)

    def test_invalid_json_and_lists_are_rejected(self):
        for raw in (b"<html>login</html>", b"[]"):
            response = io.BytesIO(raw)
            response.status = 200
            opener = Mock()
            opener.open.return_value = response
            with self.subTest(raw=raw), patch.dict(os.environ, {"RESEARCH_CLOUD_TOKEN": "test-token"}), \
                    patch.object(w.urllib.request, "build_opener", return_value=opener), self.assertRaises(w.WatchError):
                w.fetch_status(self.cfg)

    def test_no_redirect_forwards_authorization(self):
        request = urllib.request.Request(w.ENDPOINT, headers={"authorization": "synthetic"})
        self.assertIsNone(w.NoRedirect().redirect_request(request, None, 302, "Found", {},
                                                         "https://other.example/"))

    def test_smtp_ssl_and_starttls_and_test_label(self):
        for security, port in (("ssl", 465), ("starttls", 587)):
            self.cfg["smtp"].update(security=security, port=port, username="user")
            smtp = Mock()
            smtp.__enter__ = Mock(return_value=smtp)
            smtp.__exit__ = Mock(return_value=False)
            smtp.send_message.return_value = {}
            with self.subTest(security=security), patch.dict(os.environ, {"SMTP_PASSWORD": "synthetic-password"}), \
                    patch.object(w.smtplib, "SMTP_SSL", return_value=smtp) as ssl_connect, \
                    patch.object(w.smtplib, "SMTP", return_value=smtp) as tls_connect:
                w.send_email(self.cfg, "a10", False, test=True)
                message = smtp.send_message.call_args.args[0]
                self.assertEqual(message["To"], "recipient@example.com")
                self.assertIn("test email", message["Subject"])
                self.assertIn("does not mean", message.get_content())
                smtp.login.assert_called_once_with("user", "synthetic-password")
                if security == "starttls":
                    tls_connect.assert_called_once()
                    ssl_connect.assert_not_called()
                    smtp.starttls.assert_called_once()
                else:
                    ssl_connect.assert_called_once()
                    tls_connect.assert_not_called()
                    smtp.starttls.assert_not_called()

    def test_smtp_refusal_is_not_success(self):
        smtp = Mock()
        smtp.__enter__ = Mock(return_value=smtp)
        smtp.__exit__ = Mock(return_value=False)
        smtp.send_message.return_value = {"recipient@example.com": (550, b"refused")}
        with patch.object(w.smtplib, "SMTP_SSL", return_value=smtp), self.assertRaises(w.WatchError):
            w.send_email(self.cfg, "a10", True)

    def test_config_prevents_accidental_token_forwarding(self):
        for endpoint in ("http://gw.live.surfresearchcloud.nl/application-market/health/status/",
                         "https://other.example/application-market/health/status/",
                         "https://gw.live.surfresearchcloud.nl/user/users/self/", None,
                         "https://gw.live.surfresearchcloud.nl:invalid/application-market/health/status/"):
            path = self.root / "config.json"
            path.write_text(json.dumps({"endpoint": endpoint}))
            with self.subTest(endpoint=endpoint), self.assertRaises(w.WatchError):
                w.load_config(path)

    def test_missing_credentials_fails_without_starting_monitor(self):
        path = self.root / "config.json"
        cfg = dict(self.cfg)
        cfg.pop("_dir")
        path.write_text(json.dumps(cfg))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(w.main(["--config", str(path), "check"]), 78)


if __name__ == "__main__":
    unittest.main()
