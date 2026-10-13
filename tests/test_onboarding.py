"""Exercise setup and read-only guidance with synthetic input and no network."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import surf_auto as a
import surf_watch as w
from test_surf_auto import WORKSPACE


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"
        self.output = io.StringIO()
        self.gmail_inputs = ["smtp.gmail.com", "", "", "sender@gmail.com", "",
                             "recipient@university.example", WORKSPACE]
        self.credentials = ["synthetic-token", "abcdefghijklmnop"]

    def initialize(self, inputs, credentials, path=None):
        with contextlib.redirect_stdout(self.output), patch("builtins.input", side_effect=inputs), \
                patch.object(w.getpass, "getpass", side_effect=credentials), \
                patch.object(w, "fetch_status") as fetch, patch.object(w, "deliver_message") as deliver, \
                patch.object(a.WorkspaceAPI, "request") as workspace_request:
            w.initialize(path or self.path)
        fetch.assert_not_called()
        deliver.assert_not_called()
        workspace_request.assert_not_called()
        return w.load_config(path or self.path)

    def test_gmail_to_other_provider_creates_valid_automatic_config_without_network(self):
        cfg = self.initialize(self.gmail_inputs, self.credentials)
        self.assertEqual(cfg["smtp"]["host"], "smtp.gmail.com")
        self.assertEqual((cfg["smtp"]["security"], cfg["smtp"]["port"]), ("ssl", 465))
        self.assertEqual(cfg["smtp"]["from"], "sender@gmail.com")
        self.assertEqual(cfg["smtp"]["to"], "recipient@university.example")
        self.assertEqual(a.settings(cfg)["workspace_id"], WORKSPACE)
        with patch.dict(os.environ, {}, clear=True):
            w.validate_mail(cfg)
            self.assertEqual(w.secret(cfg, "RESEARCH_CLOUD_TOKEN", cfg["token_file"]), "synthetic-token")
            self.assertEqual(w.secret(cfg, "SMTP_PASSWORD", cfg["smtp"]["password_file"]), "abcdefghijklmnop")
        self.assertIn("myaccount.google.com/apppasswords", self.output.getvalue())
        for credential in self.credentials:
            self.assertNotIn(credential, self.output.getvalue())
        if os.name == "posix":
            for name in ("config.json", "surf-token.txt", "smtp-password.txt"):
                self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_gmail_starttls_uses_matching_port_and_custom_sender(self):
        inputs = ["smtp.gmail.com", "STARTTLS", "", "sender@gmail.com", "alias@example.com",
                  "recipient@example.com", ""]
        cfg = self.initialize(inputs, self.credentials)
        self.assertEqual((cfg["smtp"]["security"], cfg["smtp"]["port"]), ("starttls", 587))
        self.assertEqual(cfg["smtp"]["from"], "alias@example.com")
        self.assertNotIn("auto", cfg)
        self.assertNotIn("workspace-check", self.output.getvalue())

    def test_invalid_fields_are_retried_in_place_and_secrets_are_not_echoed(self):
        inputs = ["https://smtp.gmail.com", "smtp.gmail.com", "wrong", "ssl", "587", "0", "465",
                  "", "sender@gmail.com", "", "recipient", "recipient@university.example", "hostname", WORKSPACE]
        credentials = ["", "synthetic-token", "abcd efgh ijkl mnop", "abcdefghijklmnop"]
        cfg = self.initialize(inputs, credentials)
        self.assertEqual(cfg["smtp"]["port"], 465)
        self.assertEqual(cfg["auto"]["workspace_id"], WORKSPACE)
        self.assertGreaterEqual(self.output.getvalue().count("Please try again:"), 8)
        self.assertNotIn("abcd efgh ijkl mnop", self.output.getvalue())
        self.assertNotIn("synthetic-token", self.output.getvalue())

    def test_custom_relay_skips_password_and_can_use_notification_only_mode(self):
        cfg = self.initialize(["relay.example.com", "starttls", "2525", "", "sender@example.com",
                               "recipient@example.com", ""], ["synthetic-token"])
        self.assertEqual(cfg["smtp"]["username"], "")
        self.assertEqual(cfg["smtp"]["port"], 2525)
        self.assertFalse((self.root / "smtp-password.txt").exists())
        self.assertNotIn("auto", cfg)

    def test_custom_password_spaces_are_preserved(self):
        cfg = self.initialize(["mail.example.com", "ssl", "", "custom-user", "sender@example.com",
                               "recipient@example.com", ""], ["synthetic-token", "  custom password  "])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(w.secret(cfg, "SMTP_PASSWORD", cfg["smtp"]["password_file"]), "  custom password  ")

    def test_aborted_setup_after_invalid_workspace_leaves_no_files(self):
        inputs = self.gmail_inputs[:-1] + ["hostname"]
        # Input ends after the invalid last field; credentials must still be in memory only.
        with contextlib.redirect_stdout(self.output), \
                patch("builtins.input", side_effect=inputs + [EOFError]), \
                patch.object(w.getpass, "getpass", side_effect=self.credentials):
            self.assertEqual(w.main(["--config", str(self.path), "init"]), 78)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_credential_file_is_preserved_without_prompting(self):
        token = self.root / "surf-token.txt"
        token.write_text("existing-private-token")
        with patch("builtins.input") as input_mock, patch.object(w.getpass, "getpass") as getpass_mock:
            with self.assertRaises(w.WatchError):
                w.initialize(self.path)
        input_mock.assert_not_called()
        getpass_mock.assert_not_called()
        self.assertEqual(token.read_text(), "existing-private-token")
        self.assertFalse(self.path.exists())

    def test_next_commands_keep_custom_config_path_with_spaces(self):
        path = self.root / "my config.json"
        self.initialize(self.gmail_inputs, self.credentials, path)
        for command in ("check", "workspace-check", "test-email", "auto", "doctor"):
            hint = w.command_hint(command, path)
            self.assertIn(hint, self.output.getvalue())
            self.assertEqual(shlex.split(hint), ["python3", "surf_watch.py", "--config", str(path), command])

    def test_zero_capacity_is_successful_read_only_query(self):
        self.initialize(self.gmail_inputs, self.credentials)
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output), patch.object(w, "fetch_status", return_value={"a10": 0}), \
                patch.object(w, "send_email") as mail, patch.object(a, "run") as run:
            self.assertEqual(w.main(["--config", str(self.path), "check"]), 0)
        self.assertIn("normal waiting, not an error", self.output.getvalue())
        mail.assert_not_called()
        run.assert_not_called()
        self.assertFalse((self.root / "auto-state.json").exists())

    def test_transition_check_explains_empty_actions_without_mutations(self):
        cfg = self.initialize(self.gmail_inputs, self.credentials)
        workspace = {"id": WORKSPACE, "name": "Example VM", "status": "resuming",
                     "meta": {"application_type": "Compute", "host_name": "example-vm", "flavours": []},
                     "allowed_actions": [], "workspace_actions": []}
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output), patch.object(a.WorkspaceAPI, "get", return_value=copy.deepcopy(workspace)), \
                patch.object(a.WorkspaceAPI, "action") as action:
            a.dispatch(cfg, "workspace-check")
        self.assertIn("empty allowed_actions list can be normal", self.output.getvalue())
        action.assert_not_called()
        self.assertFalse((self.root / "auto-state.json").exists())


if __name__ == "__main__":
    unittest.main()
