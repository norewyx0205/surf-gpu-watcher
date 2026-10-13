import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

import surf_auto as a
import surf_watch as w

WORKSPACE = "11111111-1111-4111-8111-111111111111"


class FakeClock:
    def __init__(self):
        self.value = 1000

    def __call__(self):
        return self.value


class FakeAPI:
    def __init__(self, cfg, auto):
        self.validator = a.WorkspaceAPI(cfg, auto)
        self.workspace = {"id": WORKSPACE, "name": "Test workspace", "status": "paused",
                          "allowed_actions": ["resume"], "workspace_actions": [],
                          "meta": {"application_type": "Compute", "host_name": "test-vm",
                                   "flavours": [{"name": "A10 - 2 GPU", "category": "size"}]}}
        self.calls = []

    def validate(self, workspace):
        self.validator.validate(workspace)

    def get(self):
        return copy.deepcopy(self.workspace)

    def action(self, name):
        self.calls.append(name)
        self.workspace["workspace_actions"].append({"id": f"{name}-{len(self.calls)}", "type": name,
                                                     "status": "pending"})
        self.workspace["status"] = "resuming" if name == "resume" else "pausing"
        self.workspace["allowed_actions"] = []
        return self.get()

    def running(self):
        self.workspace["status"] = "running"
        self.workspace["allowed_actions"] = ["pause"]
        self.workspace["workspace_actions"][-1]["status"] = "success"

    def paused(self):
        self.workspace["status"] = "paused"
        self.workspace["allowed_actions"] = ["resume"]
        self.workspace["workspace_actions"][-1]["status"] = "success"


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = FakeClock()
        self.cfg = {"_dir": self.root, "endpoint": w.ENDPOINT, "status_key": "a10", "min_free": 1,
                    "interval_seconds": 60, "timeout_seconds": 20, "state_file": "notify-state.json",
                    "token_file": "surf-token.txt",
                    "smtp": {"host": "mail.example.com", "port": 465, "security": "ssl",
                             "from": "sender@example.com", "to": "recipient@example.com", "username": ""},
                    "auto": {"workspace_id": WORKSPACE, "expected_hostname": "test-vm",
                             "expected_flavour": "A10 - 2 GPU"}}
        self.auto = a.settings(self.cfg)
        self.store = a.LeaseStore(self.cfg, self.auto)
        self.store.transaction(create=True)
        self.api = FakeAPI(self.cfg, self.auto)
        self.fetch = Mock(return_value={"a10": True})
        self.controller = self.controller_again()

    def controller_again(self):
        return a.Controller(self.cfg, self.auto, self.store, api=self.api, fetch=self.fetch, clock=self.clock)

    def start(self):
        self.controller.tick()
        self.clock.value += 30
        self.api.running()
        self.controller.tick()
        return self.store.transaction()

    def test_unavailable_polls_every_sixty_seconds_without_actions(self):
        self.fetch.return_value = {"a10": False}
        self.assertEqual(self.controller.tick(), 60)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.store.transaction()["phase"], "waiting")

    def test_resume_response_does_not_start_confirmation_timer(self):
        self.controller.tick()
        state = self.store.transaction()
        self.assertEqual(state["phase"], "resuming")
        self.assertNotIn("deadline", state)
        self.assertEqual(state["notices"], {})
        self.assertEqual(state["resume_action_id"], "resume-1")

    def test_running_starts_full_ten_minute_window(self):
        state = self.start()
        self.assertEqual(state["deadline"] - state["running_at"], 600)
        self.assertEqual(state["phase"], "awaiting_confirmation")
        self.assertGreaterEqual(len(state["confirmation_token"]), 32)
        self.assertIn("running", state["notices"])

    def test_email_preview_get_does_not_confirm(self):
        state = self.start()
        before = self.store.path.read_bytes()
        result = a.confirmation_response(self.store, self.auto, "GET", state["confirmation_token"], clock=self.clock)
        self.assertEqual(result[0], 200)
        self.assertTrue(result[3])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_click_cancels_pause_and_stops_run(self):
        state = self.start()
        result = a.confirmation_response(self.store, self.auto, "POST", state["confirmation_token"], clock=self.clock)
        self.assertEqual(result[0], 200)
        self.clock.value = state["deadline"] + 100
        self.assertIsNone(self.controller.tick())
        self.assertEqual(self.store.transaction()["phase"], "confirmed")
        self.assertEqual(self.api.calls, ["resume"])

    def test_timeout_pauses_and_waits_for_actual_paused_status(self):
        state = self.start()
        self.clock.value = state["deadline"]
        self.controller.tick()
        self.assertEqual(self.api.calls, ["resume", "pause"])
        self.assertEqual(self.store.transaction()["phase"], "pausing")
        self.controller.tick()
        self.assertNotIn("paused", self.store.transaction()["notices"])
        self.api.paused()
        self.assertIsNone(self.controller.tick())
        self.assertEqual(self.store.transaction()["phase"], "paused")
        self.assertIn("paused", self.store.transaction()["notices"])
        self.controller.tick()
        self.assertEqual(self.api.calls, ["resume", "pause"])

    def test_restart_preserves_original_deadline(self):
        state = self.start()
        self.clock.value = state["deadline"] + 1
        self.controller_again().tick()
        self.assertEqual(self.store.transaction()["deadline"], state["deadline"])
        self.assertEqual(self.api.calls, ["resume", "pause"])

    def test_confirm_before_expiry_wins_atomic_race(self):
        state = self.start()
        self.clock.value = state["deadline"] - 0.01
        self.store.confirm(state["confirmation_token"], clock=self.clock)
        self.clock.value += 1
        self.controller.tick()
        self.assertEqual(self.api.calls, ["resume"])

    def test_late_confirmation_cannot_cancel_submitted_pause(self):
        state = self.start()
        self.clock.value = state["deadline"]
        self.controller.tick()
        result = a.confirmation_response(self.store, self.auto, "POST", state["confirmation_token"], clock=self.clock)
        self.assertEqual(result[0], 410)
        self.assertEqual(self.store.transaction()["phase"], "pausing")

    def test_forged_or_old_link_cannot_confirm(self):
        state = self.start()
        for token in (None, "wrong", "previous-token", "\u00e9"):
            with self.subTest(token=token):
                self.assertEqual(a.confirmation_response(self.store, self.auto, "POST", token, clock=self.clock)[0], 403)
        self.assertEqual(self.store.transaction()["phase"], "awaiting_confirmation")
        self.store.patch(confirmation_token="new-lease-token")
        self.assertEqual(a.confirmation_response(self.store, self.auto, "POST", state["confirmation_token"], clock=self.clock)[0], 403)

    def test_already_running_workspace_is_never_paused(self):
        self.api.workspace.update(status="running", allowed_actions=["pause"])
        self.controller.tick()
        self.assertEqual(self.store.transaction()["phase"], "intervened")
        self.assertEqual(self.api.calls, [])
        self.fetch.assert_not_called()

    def test_external_action_disables_automatic_pause(self):
        state = self.start()
        self.api.workspace["workspace_actions"].append({"id": "other-user-action", "type": "update", "status": "success"})
        self.clock.value = state["deadline"]
        self.controller.tick()
        self.assertEqual(self.store.transaction()["phase"], "intervened")
        self.assertEqual(self.api.calls, ["resume"])

    def test_queued_resume_is_not_submitted_twice_while_still_paused(self):
        self.controller.tick()
        self.api.workspace["status"] = "paused"
        for _ in range(4):
            self.controller.tick()
        self.assertEqual(self.api.calls, ["resume"])
        self.assertEqual(self.store.transaction()["phase"], "resuming")

    def test_failed_resume_rechecks_availability_before_retry(self):
        self.controller.tick()
        self.api.workspace["status"] = "paused"
        self.api.workspace["allowed_actions"] = ["resume"]
        self.api.workspace["workspace_actions"][-1]["status"] = "failed"
        self.assertEqual(self.controller.tick(), 60)
        self.assertEqual(self.store.transaction()["phase"], "waiting")
        self.fetch.return_value = {"a10": False}
        self.controller.tick()
        self.assertEqual(self.api.calls, ["resume"])
        self.assertEqual(self.fetch.call_count, 2)

    def test_rejected_resume_can_retry_on_next_capacity_poll(self):
        with patch.object(self.api, "action", side_effect=a.WorkspaceError("capacity", code=400)):
            self.assertEqual(self.controller.tick(), 60)
        self.assertEqual(self.store.transaction()["phase"], "waiting")
        self.controller.tick()
        self.assertEqual(self.api.calls, ["resume"])

    def test_uncertain_resume_after_restart_is_not_duplicated_or_claimed(self):
        original = self.api.action
        def accepted_but_disconnected(name):
            original(name)
            raise a.WorkspaceError("connection lost", uncertain=True, retryable=True)
        with patch.object(self.api, "action", side_effect=accepted_but_disconnected), self.assertRaises(a.WorkspaceError):
            self.controller.tick()
        self.api.running()
        self.controller_again().tick()
        self.assertEqual(self.api.calls, ["resume"])
        self.assertEqual(self.store.transaction()["phase"], "needs_attention")

    def test_uncertain_pause_is_observed_not_submitted_twice(self):
        state = self.start()
        self.clock.value = state["deadline"]
        original = self.api.action
        def accepted_but_disconnected(name):
            original(name)
            raise a.WorkspaceError("connection lost", uncertain=True, retryable=True)
        with patch.object(self.api, "action", side_effect=accepted_but_disconnected), self.assertRaises(a.WorkspaceError):
            self.controller.tick()
        self.controller_again().tick()
        self.assertEqual(self.store.transaction()["phase"], "pausing")
        self.api.paused()
        self.controller_again().tick()
        self.assertEqual(self.store.transaction()["phase"], "paused")
        self.assertEqual(self.api.calls, ["resume", "pause"])

    def test_known_pause_rejection_can_recover_after_permissions_fixed(self):
        state = self.start()
        self.clock.value = state["deadline"]
        with patch.object(self.api, "action", side_effect=a.WorkspaceError("forbidden", code=403)), self.assertRaises(a.WorkspaceError):
            self.controller.tick()
        self.assertIsNone(self.store.transaction()["pause_request_at"])
        self.controller.tick()
        self.assertEqual(self.api.calls, ["resume", "pause"])

    def test_failed_pause_does_not_report_release(self):
        state = self.start()
        self.clock.value = state["deadline"]
        self.controller.tick()
        self.api.workspace["status"] = "running"
        self.api.workspace["workspace_actions"][-1]["status"] = "failed"
        self.controller.tick()
        state = self.store.transaction()
        self.assertEqual(state["phase"], "needs_attention")
        self.assertNotIn("paused", state["notices"])

    def test_workspace_identity_and_flavour_are_pinned(self):
        for mutation in ({"id": "another-id"}, {"meta": {"application_type": "Storage"}},
                         {"meta": {"application_type": "Compute", "host_name": "another-vm"}}):
            with self.subTest(mutation=mutation), self.assertRaises(w.WatchError):
                self.api.validate(self.api.get() | mutation)
        workspace = self.api.get()
        workspace["meta"]["flavours"] = [{"name": "A100 - 1 GPU"}]
        with self.assertRaises(w.WatchError):
            self.api.validate(workspace)

    def test_different_workspace_cannot_reuse_state(self):
        other = copy.deepcopy(self.auto)
        other["workspace_id"] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaises(w.WatchError):
            a.LeaseStore(self.cfg, other).transaction()

    def test_remote_confirmation_requires_https(self):
        self.cfg["auto"]["confirmation"] = {"base_url": "http://public.example.com"}
        with self.assertRaises(w.WatchError):
            a.settings(self.cfg)
        self.cfg["auto"]["confirmation"] = {"base_url": "https://public.example.com"}
        self.assertEqual(a.settings(self.cfg)["confirmation"]["host"], "127.0.0.1")

    def test_read_only_workspace_get_and_exact_action_endpoints(self):
        client = a.WorkspaceAPI(self.cfg, self.auto)
        opener = Mock()
        with patch.dict(os.environ, {"RESEARCH_CLOUD_TOKEN": "synthetic-token"}), \
                patch.object(w.urllib.request, "build_opener", return_value=opener):
            for operation in (None, "resume", "pause"):
                response = io.BytesIO(json.dumps(self.api.get()).encode())
                response.status = 200
                opener.open.return_value = response
                client.request(operation)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_header("Authorization"), "synthetic-token")
                self.assertEqual(request.get_method(), "GET" if operation is None else "POST")
                if operation:
                    self.assertTrue(request.full_url.endswith(f"/{WORKSPACE}/actions/{operation}/"))
                    self.assertEqual(request.data, b"{}")
                    self.assertEqual(request.get_header("Content-type"), f"application/json;{operation}")

    def test_action_timeout_is_uncertain(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("timeout")
        with patch.dict(os.environ, {"RESEARCH_CLOUD_TOKEN": "synthetic-token"}), \
                patch.object(w.urllib.request, "build_opener", return_value=opener), self.assertRaises(a.WorkspaceError) as caught:
            a.WorkspaceAPI(self.cfg, self.auto).action("resume")
        self.assertTrue(caught.exception.uncertain)

    def test_invalid_post_response_is_uncertain(self):
        opener = Mock()
        response = io.BytesIO(b'{}')
        response.status = 200
        opener.open.return_value = response
        with patch.dict(os.environ, {"RESEARCH_CLOUD_TOKEN": "synthetic-token"}), \
                patch.object(w.urllib.request, "build_opener", return_value=opener), self.assertRaises(a.WorkspaceError) as caught:
            a.WorkspaceAPI(self.cfg, self.auto).action("pause")
        self.assertTrue(caught.exception.uncertain)

    def test_smtp_latency_does_not_block_timeout_cleanup(self):
        state = self.start()
        started, release = threading.Event(), threading.Event()
        events = []
        def blocking_send(cfg, auto, state, event):
            events.append(event)
            if event == "running":
                started.set()
                release.wait(timeout=2)
        worker = a.MailWorker(self.cfg, self.auto, self.store, send=blocking_send, clock=self.clock)
        try:
            worker.pump()
            self.assertTrue(started.wait(timeout=1))
            self.clock.value = state["deadline"]
            self.controller.tick()
            self.assertEqual(self.api.calls, ["resume", "pause"])
            self.api.paused()
            self.controller.tick()
        finally:
            release.set()
            worker.close()
        self.assertIn("paused", events)
        self.assertIsNotNone(self.store.transaction()["notices"]["paused"])

    def test_failed_email_does_not_confirm_or_cancel_cleanup(self):
        state = self.start()
        failed = threading.Event()
        def failing_send(*args):
            failed.set()
            raise w.WatchError("SMTP failed")
        worker = a.MailWorker(self.cfg, self.auto, self.store, send=failing_send, clock=self.clock)
        try:
            worker.pump()
            failed.wait(timeout=1)
            self.clock.value = state["deadline"]
            self.controller.tick()
            self.assertEqual(self.api.calls, ["resume", "pause"])
        finally:
            worker.close()

    def test_confirmation_email_is_english_and_links_to_post_page(self):
        state = self.start()
        with patch.object(w, "deliver_message") as deliver:
            a.send_notice(self.cfg, self.auto, state, "running")
        message = deliver.call_args.args[1]
        self.assertIn("VM running", message["Subject"])
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("/confirm?token=", plain)
        self.assertIn("Starting SSH or Jupyter does not count", plain)
        self.assertNotIn("synthetic-token", plain)

    def test_confirmation_server_failure_before_resume_prevents_acquisition(self):
        with patch.object(a, "ThreadingHTTPServer", side_effect=OSError("in use")), self.assertRaises(w.WatchError):
            with a.confirmation_server(self.store, self.auto):
                self.fail("must not acquire without a confirmation server")

    def test_confirmation_server_failure_after_restart_keeps_guard_alive(self):
        self.start()
        with patch.object(a, "ThreadingHTTPServer", side_effect=OSError("in use")):
            with a.confirmation_server(self.store, self.auto):
                state = self.store.transaction()
                self.clock.value = state["deadline"]
                self.controller.tick()
        self.assertEqual(self.api.calls, ["resume", "pause"])

    def test_full_auto_command_acquires_and_releases_without_confirmation(self):
        cfg = dict(self.cfg)
        cfg.pop("_dir")
        path = self.root / "config.json"
        path.write_text(json.dumps(cfg))
        notices = []
        original_controller, original_worker = a.Controller, a.MailWorker

        def make_controller(cfg, auto, store):
            return original_controller(cfg, auto, store, api=self.api, fetch=self.fetch, clock=self.clock)

        def make_worker(cfg, auto, store):
            return original_worker(cfg, auto, store,
                                   send=lambda cfg, auto, state, event: notices.append(event), clock=self.clock)

        def advance(delay):
            state = self.store.transaction()
            if state["phase"] == "resuming":
                self.api.running()
                self.clock.value += delay
            elif state["phase"] == "awaiting_confirmation":
                self.clock.value = state["deadline"]
            elif state["phase"] == "pausing":
                self.api.paused()
                self.clock.value += delay
            else:
                self.fail("Unexpected phase in the full workflow: " + state["phase"])

        with patch.object(a, "Controller", side_effect=make_controller), \
                patch.object(a, "MailWorker", side_effect=make_worker), \
                patch.object(a, "confirmation_server", side_effect=lambda *args: contextlib.nullcontext()), \
                patch.object(a.time, "sleep", side_effect=advance):
            self.assertEqual(w.main(["--config", str(path), "auto"]), 0)
        self.assertEqual(self.api.calls, ["resume", "pause"])
        self.assertEqual(self.store.transaction()["phase"], "paused")
        self.assertIn("paused", notices)


if __name__ == "__main__":
    unittest.main()
