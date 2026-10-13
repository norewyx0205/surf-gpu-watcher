import contextlib
import errno
import io
import os
from pathlib import Path
import smtplib
import socket
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import surf_auto as a
import surf_watch as w
from test_surf_auto import WORKSPACE


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"_dir": Path(self.tmp.name), "endpoint": w.ENDPOINT, "status_key": "a10", "min_free": 1,
                    "interval_seconds": 60, "timeout_seconds": 20, "state_file": "notify-state.json",
                    "token_file": "missing-token.txt",
                    "smtp": {"host": "smtp.gmail.com", "port": 465, "security": "ssl",
                             "from": "sender@example.com", "to": "recipient@example.com", "username": "user",
                             "password_file": "missing-password.txt"},
                    "auto": {"workspace_id": WORKSPACE}}

    def test_certificate_failure_is_recognized_through_urlerror(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError(1, "sensitive-string"))
        diagnosed = w.connection_error(error, "SURF connection")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(diagnosed))
        self.assertIn("Install Certificates.command", str(diagnosed))
        self.assertNotIn("sensitive-string", str(diagnosed))
        self.assertFalse(diagnosed.retryable)

    def test_common_transport_failures_are_distinguished(self):
        cases = [(socket.gaierror(-2, "private-hostname"), "DNS lookup failed", True),
                 (TimeoutError("private-hostname"), "timed out", True),
                 (ConnectionRefusedError("private-hostname"), "Connection refused", True),
                 (ConnectionResetError("private-hostname"), "Connection reset", True),
                 (OSError(errno.ENETUNREACH, "private-hostname"), "unreachable", True),
                 (OSError(errno.EACCES, "private-hostname"), "blocked", False),
                 (ssl.SSLError(1, "private-hostname"), "TLS handshake failed", False),
                 (smtplib.SMTPNotSupportedError("private-hostname"), "does not support", False)]
        for error, text, retryable in cases:
            with self.subTest(text=text):
                diagnosed = w.connection_error(error, "Connection")
                self.assertIn(text, str(diagnosed))
                self.assertEqual(diagnosed.retryable, retryable)
                self.assertNotIn("private-hostname", str(diagnosed))

    def test_proxy_errors_do_not_dump_proxy_credentials(self):
        error = urllib.error.URLError("Tunnel connection failed: 407 https://user:private-password@proxy/")
        diagnosed = w.connection_error(error, "SURF")
        self.assertIn("proxy authentication failed", str(diagnosed))
        self.assertNotIn("private-password", str(diagnosed))

    def test_catalog_reports_certificate_cause_not_generic_timeout(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError(1, "hidden"))
        with patch.dict(os.environ, {"RESEARCH_CLOUD_TOKEN": "synthetic-token"}), \
                patch.object(w.urllib.request, "build_opener", return_value=opener), self.assertRaises(w.WatchError) as caught:
            w.fetch_status(self.cfg)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(caught.exception))
        self.assertFalse(caught.exception.retryable)

    def test_workspace_retains_post_uncertainty_with_detailed_tls_error(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError(1, "hidden"))
        with patch.dict(os.environ, {"RESEARCH_CLOUD_TOKEN": "synthetic-token"}), \
                patch.object(w.urllib.request, "build_opener", return_value=opener), self.assertRaises(a.WorkspaceError) as caught:
            a.WorkspaceAPI(self.cfg, a.settings(self.cfg)).action("resume")
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(caught.exception))
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(caught.exception.retryable)

    def test_smtp_certificate_error_is_reported_without_password(self):
        with patch.dict(os.environ, {"SMTP_PASSWORD": "synthetic-password"}), \
                patch.object(w.smtplib, "SMTP_SSL", side_effect=ssl.SSLCertVerificationError(1, "private")), \
                self.assertRaises(w.WatchError) as caught:
            w.send_email(self.cfg, "a10", False, test=True)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(caught.exception))
        self.assertNotIn("synthetic-password", str(caught.exception))
        self.assertFalse(caught.exception.retryable)

    def test_doctor_checks_tls_without_credentials_authentication_or_email(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(w.ENDPOINT, 401, "Unauthorized", {}, io.BytesIO(b""))
        smtp = Mock()
        smtp.__enter__ = Mock(return_value=smtp)
        smtp.__exit__ = Mock(return_value=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(w, "secret") as secret, \
                patch.object(w.urllib.request, "getproxies", return_value={"https": "https://user:private-password@proxy/"}), \
                patch.object(w.urllib.request, "build_opener", return_value=opener), \
                patch.object(w.smtplib, "SMTP_SSL", return_value=smtp):
            self.assertTrue(w.doctor(self.cfg))
        secret.assert_not_called()
        smtp.login.assert_not_called()
        smtp.send_message.assert_not_called()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIn("SURF HTTPS: OK - HTTP 401", output.getvalue())
        self.assertNotIn("private-password", output.getvalue())
        self.assertNotIn("sender@example.com", output.getvalue())

    def test_doctor_reports_api_tls_and_smtp_dns_independently(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError(1, "hidden"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(w.urllib.request, "build_opener", return_value=opener), \
                patch.object(w.smtplib, "SMTP_SSL", side_effect=socket.gaierror(-2, "hidden")):
            self.assertFalse(w.doctor(self.cfg))
        text = output.getvalue()
        self.assertIn("CERTIFICATE_VERIFY_FAILED", text)
        self.assertIn("DNS lookup failed", text)

    def test_doctor_starttls_probes_without_logging_in(self):
        self.cfg["smtp"].update(security="starttls", port=587)
        response = io.BytesIO(b"")
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        smtp = Mock()
        smtp.__enter__ = Mock(return_value=smtp)
        smtp.__exit__ = Mock(return_value=False)
        with contextlib.redirect_stdout(io.StringIO()), patch.object(w.urllib.request, "build_opener", return_value=opener), \
                patch.object(w.smtplib, "SMTP", return_value=smtp):
            self.assertTrue(w.doctor(self.cfg))
        smtp.starttls.assert_called_once()
        smtp.login.assert_not_called()
        smtp.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
