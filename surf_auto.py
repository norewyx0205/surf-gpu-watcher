"""Automatic SURF workspace lease management and explicit email confirmation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import surf_watch as w

API_ROOT = "https://gw.live.surfresearchcloud.nl/v1/workspace/workspaces/"
FINISHED = {"confirmed", "paused", "intervened", "needs_attention"}


class WorkspaceError(w.WatchError):
    def __init__(self, message, *, code=None, uncertain=False, retryable=False, retry_after=0):
        super().__init__(message, retryable=retryable, retry_after=retry_after)
        self.code = code
        self.uncertain = uncertain


def settings(cfg):
    auto = cfg.get("auto")
    if not isinstance(auto, dict):
        raise w.WatchError("Configure the auto section before using automatic mode.")
    auto = copy.deepcopy(auto)
    try:
        value = str(uuid.UUID(auto["workspace_id"]))
        if value != auto["workspace_id"].lower():
            raise ValueError
    except (ValueError, KeyError, AttributeError, TypeError):
        raise w.WatchError("auto.workspace_id must be a valid SURF workspace UUID.") from None
    auto["workspace_id"] = value
    auto.setdefault("state_file", "auto-state.json")
    auto.setdefault("confirmation_seconds", 600)
    auto.setdefault("status_poll_seconds", 5)
    auto.setdefault("resume_timeout_seconds", 900)
    auto.setdefault("display_timezone", "Europe/Amsterdam")
    auto.setdefault("confirmation", {})
    if not isinstance(auto["state_file"], str) or not auto["state_file"]:
        raise w.WatchError("auto.state_file must be a nonempty file path.")
    if w.local_path(cfg, auto["state_file"]).resolve() == w.local_path(cfg, cfg["state_file"]).resolve():
        raise w.WatchError("Automatic mode and notification-only mode need different state files.")
    for key, minimum in (("confirmation_seconds", 1), ("status_poll_seconds", 1),
                         ("resume_timeout_seconds", 30)):
        value = auto[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
            raise w.WatchError(f"auto.{key} must be at least {minimum} seconds.")
    for key in ("expected_hostname", "expected_flavour", "display_timezone"):
        if key in auto and (not isinstance(auto[key], str) or not auto[key]):
            raise w.WatchError(f"auto.{key} must be a nonempty string.")
    confirmation = auto["confirmation"]
    if not isinstance(confirmation, dict):
        raise w.WatchError("auto.confirmation must be an object.")
    confirmation.setdefault("host", "127.0.0.1")
    confirmation.setdefault("port", 8765)
    confirmation.setdefault("base_url", "http://127.0.0.1:8765")
    if confirmation["host"] != "127.0.0.1":
        raise w.WatchError("The confirmation server must bind to 127.0.0.1. Use an HTTPS reverse proxy for remote access.")
    if type(confirmation["port"]) is not int or not 1 <= confirmation["port"] <= 65535:
        raise w.WatchError("auto.confirmation.port must be a valid port.")
    try:
        url = urllib.parse.urlsplit(confirmation["base_url"])
        port = url.port
    except (ValueError, TypeError, AttributeError):
        raise w.WatchError("Invalid confirmation base_url.") from None
    local = url.hostname in ("localhost", "127.0.0.1")
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.path not in ("", "/") or url.scheme not in ("http", "https")
            or (url.scheme == "http" and not local)
            or (url.scheme == "http" and (port or 80) != confirmation["port"])):
        raise w.WatchError("confirmation.base_url must be an HTTPS origin, or the local HTTP server origin.")
    confirmation["base_url"] = confirmation["base_url"].rstrip("/")
    return auto


class WorkspaceAPI:
    def __init__(self, cfg, auto):
        self.cfg, self.auto = cfg, auto
        self.url = API_ROOT + auto["workspace_id"] + "/"

    def request(self, action=None):
        if action not in (None, "resume", "pause"):
            raise w.WatchError("Only resume and pause are supported.")
        try:
            token = w.secret(self.cfg, "RESEARCH_CLOUD_TOKEN", self.cfg["token_file"]).strip()
        except w.WatchError as e:
            raise WorkspaceError(str(e), uncertain=False) from None
        if not token or any(c in token for c in "\r\n"):
            raise WorkspaceError("Invalid SURF API token.", uncertain=False)
        headers = {"authorization": token, "Accept": "application/json", "Cache-Control": "no-cache",
                   "User-Agent": "surf-gpu-watch/2.0"}
        url = self.url
        if action:
            url += f"actions/{action}/"
            headers["Content-Type"] = f"application/json;{action}"
        request = urllib.request.Request(url, method="POST" if action else "GET",
                                         data=b"{}" if action else None, headers=headers)
        opener = urllib.request.build_opener(w.NoRedirect())
        try:
            with opener.open(request, timeout=self.cfg["timeout_seconds"]) as response:
                if response.status != 200:
                    raise WorkspaceError(f"Unexpected workspace HTTP {response.status}.", uncertain=bool(action))
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise WorkspaceError("Workspace response exceeds 1 MiB.", uncertain=bool(action))
                payload = json.loads(raw)
        except urllib.error.HTTPError as e:
            code = e.code
            delay = w.retry_after_seconds(e.headers.get("Retry-After")) if e.headers else 0
            e.close()
            raise WorkspaceError(f"Workspace HTTP {code}. Check permissions and workspace status.", code=code,
                                 uncertain=bool(action) and (code == 408 or code >= 500),
                                 retryable=code in (408, 429) or code >= 500, retry_after=delay) from None
        except (urllib.error.URLError, OSError) as e:
            detail = w.connection_error(e, "Workspace connection")
            raise WorkspaceError(str(detail), uncertain=bool(action), retryable=detail.retryable) from None
        except (ValueError, UnicodeError):
            raise WorkspaceError("Invalid workspace JSON response.", uncertain=bool(action)) from None
        try:
            self.validate(payload)
        except w.WatchError as e:
            raise WorkspaceError(str(e), uncertain=bool(action)) from None
        return payload

    def validate(self, workspace):
        if not isinstance(workspace, dict) or workspace.get("id") != self.auto["workspace_id"]:
            raise w.WatchError("The API response does not match the configured workspace ID.")
        if not isinstance(workspace.get("status"), str):
            raise w.WatchError("Missing workspace status.")
        if (not isinstance(workspace.get("allowed_actions"), list)
                or not isinstance(workspace.get("workspace_actions"), list)):
            raise w.WatchError("Missing allowed_actions or workspace_actions; refusing automatic control.")
        for action in workspace["workspace_actions"]:
            if not isinstance(action, dict) or not isinstance(action.get("id"), str):
                raise w.WatchError("Invalid workspace action history.")
        meta = workspace.get("meta", {})
        if not isinstance(meta, dict) or meta.get("application_type") != "Compute":
            raise w.WatchError("The target must be a Compute workspace.")
        if self.auto.get("expected_hostname") and meta.get("host_name") != self.auto["expected_hostname"]:
            raise w.WatchError("Workspace hostname does not match expected_hostname.")
        if self.auto.get("expected_flavour"):
            flavours = meta.get("flavours", [])
            names = [v.get("name") for v in flavours if isinstance(v, dict)]
            if self.auto["expected_flavour"] not in names:
                raise w.WatchError("Workspace flavour does not match expected_flavour. Run workspace-check.")

    def get(self):
        return self.request()

    def action(self, name):
        return self.request(name)


class LeaseStore:
    """Short transactions let confirmation and timeout serialize across processes."""
    def __init__(self, cfg, auto):
        self.path = w.local_path(cfg, auto["state_file"])
        if not isinstance(cfg.get("smtp"), dict) or not isinstance(cfg["smtp"].get("to"), str):
            raise w.WatchError("Configure smtp.to before accessing automatic lease state.")
        self.identity = {"workspace_id": auto["workspace_id"], "endpoint": cfg["endpoint"],
                         "status_key": cfg["status_key"], "min_free": cfg["min_free"],
                         "to": cfg["smtp"]["to"], "confirmation_seconds": auto["confirmation_seconds"],
                         "confirmation_base_url": auto["confirmation"]["base_url"]}

    def transaction(self, change=None, *, create=False):
        with w.state_lock(self.path, blocking=True):
            migrated = False
            if self.path.exists():
                state = w.read_json(self.path)
                if isinstance(state.get("identity"), dict):
                    saved_endpoint = state["identity"].get("endpoint")
                    canonical = w.canonical_catalog_endpoint(saved_endpoint)
                    if canonical != saved_endpoint:
                        state["identity"]["endpoint"] = canonical
                        migrated = True
                if state.get("version") != 2 or state.get("identity") != self.identity:
                    raise w.WatchError("This auto state file belongs to another configuration. Use a new auto.state_file.")
                if state.get("phase") not in {"waiting", "resume_pending", "resuming", "awaiting_confirmation",
                                              "pause_pending", "pausing"} | FINISHED:
                    raise w.WatchError("Invalid saved lease phase. Inspect the state before restarting.")
            elif create:
                state = {"version": 2, "identity": self.identity, "phase": "waiting", "notices": {},
                         "lease_id": None, "resume_action_id": None, "pause_action_id": None}
            else:
                raise w.WatchError("No automatic lease exists yet.")
            if change:
                change(state)
            if change or create or migrated:
                w.private_write(self.path, json.dumps(state, indent=2) + "\n")
            return copy.deepcopy(state)

    def patch(self, **values):
        return self.transaction(lambda s: s.update(values))

    def confirm(self, token=None, *, clock=time.time):
        def change(state):
            if token is not None and not valid_token(state, token):
                raise w.WatchError("Invalid or expired confirmation link.")
            if state["phase"] == "confirmed":
                return
            if state["phase"] != "awaiting_confirmation" or clock() >= state["deadline"]:
                raise w.WatchError("The confirmation window has expired or the VM is not ready.")
            state.update(phase="confirmed", confirmed_at=clock())
        return self.transaction(change)


def valid_token(state, token):
    expected = state.get("confirmation_token")
    return (isinstance(token, str) and isinstance(expected, str)
            and secrets.compare_digest(token.encode(), expected.encode()))


def display_time(timestamp, auto):
    try:
        zone = ZoneInfo(auto["display_timezone"])
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return datetime.fromtimestamp(timestamp, zone).strftime("%Y-%m-%d %H:%M:%S %Z")


class Controller:
    def __init__(self, cfg, auto, store, *, api=None, fetch=w.fetch_status, clock=time.time):
        self.cfg, self.auto, self.store = cfg, auto, store
        self.api = api or WorkspaceAPI(cfg, auto)
        self.fetch, self.clock = fetch, clock

    def finish(self, phase, reason):
        def change(state):
            state.update(phase=phase, reason=reason)
            state.setdefault("notices", {})["attention" if phase in ("intervened", "needs_attention") else "paused"] = None
        self.store.transaction(change)
        w.LOG.warning("%s: %s", phase, reason)

    def bind_resume(self, state, workspace):
        known = set(state["baseline_actions"])
        owned = {state.get("resume_action_id"), state.get("pause_action_id")}
        new = [a for a in workspace["workspace_actions"] if a["id"] not in known | owned]
        if state.get("resume_action_id") is None:
            resumes = [a for a in new if a.get("type") == "resume"]
            if len(resumes) == 1 and len(new) == 1 and state.get("resume_accepted"):
                self.store.patch(resume_action_id=resumes[0]["id"])
                state["resume_action_id"] = resumes[0]["id"]
                new = []
        if state.get("resume_action_id") and state.get("pause_request_at") is not None:
            pauses = [a for a in new if a.get("type") == "pause"]
            if state.get("pause_action_id") is None and len(pauses) == len(new) == 1:
                self.store.patch(pause_action_id=pauses[0]["id"])
                state["pause_action_id"] = pauses[0]["id"]
                new = []
        if new:
            self.finish("intervened", "An unexpected workspace action appeared. Automatic pause is disabled for this run.")
            return False
        if not state.get("resume_action_id"):
            if self.clock() - state["resume_requested_at"] >= self.auto["resume_timeout_seconds"]:
                self.finish("needs_attention", "Cannot identify the accepted resume action. Check the VM manually.")
            return False
        if not any(a["id"] == state["resume_action_id"] for a in workspace["workspace_actions"]):
            self.finish("needs_attention", "The tracked resume action disappeared. Check the VM manually.")
            return False
        return True

    def tick(self):
        state = self.store.transaction()
        phase = state["phase"]
        if phase in FINISHED:
            return None
        if phase == "resume_pending":
            self.finish("needs_attention", "A previous resume request has an uncertain outcome. No duplicate request was sent.")
            return None
        # Reserve cleanup atomically. A confirmation committed before the deadline wins.
        if phase == "awaiting_confirmation":
            def expire(current):
                if current["phase"] == "awaiting_confirmation" and self.clock() >= current["deadline"]:
                    current.update(phase="pause_pending", pause_request_at=None)
            state = self.store.transaction(expire)
            phase = state["phase"]
            if phase in FINISHED:
                return None
        workspace = self.api.get()
        self.api.validate(workspace)
        status = workspace["status"]
        self.store.patch(last_status=status, checked_at=self.clock(), workspace_name=workspace.get("name", ""))
        if phase == "waiting":
            if status != "paused":
                self.finish("intervened", f"Workspace is {status}, not paused. This watcher did not resume it and will not pause it.")
                return None
            if "resume" not in workspace["allowed_actions"]:
                raise w.WatchError("Your token cannot resume this workspace. The owner or a workspace admin must authorize it.")
            ready, _, _ = w.availability(self.fetch(self.cfg), self.cfg["status_key"], self.cfg["min_free"])
            if not ready:
                w.LOG.info("%s unavailable; waiting for capacity.", self.cfg["status_key"])
                return self.cfg["interval_seconds"]
            state = self.store.patch(phase="resume_pending", resume_requested_at=self.clock(), resume_accepted=False,
                                     baseline_actions=[a["id"] for a in workspace["workspace_actions"]],
                                     lease_id=str(uuid.uuid4()), resume_action_id=None, pause_action_id=None,
                                     confirmation_token=None, notices={})
            try:
                result = self.api.action("resume")
            except WorkspaceError as e:
                if e.code in (400, 409, 429) and not e.uncertain:
                    self.store.patch(phase="waiting", reason=str(e))
                    w.LOG.warning("Resume rejected; rechecking capacity before another attempt.")
                    return max(self.cfg["interval_seconds"], e.retry_after)
                if not e.uncertain:
                    self.store.patch(phase="waiting", reason=str(e))
                raise
            self.api.validate(result)
            state = self.store.patch(phase="resuming", resume_accepted=True)
            self.bind_resume(state, result)
            w.LOG.info("Resume accepted for %s; waiting for running status.", self.auto["workspace_id"])
            return self.auto["status_poll_seconds"]
        if phase == "pausing":
            if status == "paused":
                self.finish("paused", "The workspace reached paused. GPU capacity has been released.")
                return None
            if status not in ("running", "pausing"):
                self.finish("needs_attention", f"Unexpected status {status} while waiting for pause completion.")
                return None
            # A new resume while the tool is pausing indicates external intervention.
            allowed = set(state["baseline_actions"]) | {state["resume_action_id"], state.get("pause_action_id")}
            new = [a for a in workspace["workspace_actions"] if a["id"] not in allowed]
            pauses = [a for a in new if a.get("type") == "pause"]
            if state.get("pause_action_id") is None and len(new) == len(pauses) == 1:
                self.store.patch(pause_action_id=pauses[0]["id"])
                new = []
            if new:
                self.finish("intervened", "Another workspace action appeared during pause. Check the VM manually.")
                return None
            pause = next((a for a in workspace["workspace_actions"] if a["id"] == state.get("pause_action_id")), None)
            if pause and pause.get("status") in ("failed", "cancelled", "unknown_error"):
                self.finish("needs_attention", "The pause action failed. The VM may still be consuming GPU credits.")
                return None
            if self.clock() - state["pause_request_at"] >= self.auto["resume_timeout_seconds"]:
                self.finish("needs_attention", "Pause has not completed within the operation timeout. Check the VM manually.")
                return None
            return self.auto["status_poll_seconds"]
        if not self.bind_resume(state, workspace):
            return self.auto["status_poll_seconds"]
        resume = next(a for a in workspace["workspace_actions"] if a["id"] == state["resume_action_id"])
        if phase == "resuming":
            if status == "running":
                started = self.clock()
                expired = started - state["resume_requested_at"] >= self.auto["resume_timeout_seconds"]
                self.store.patch(phase="pause_pending" if expired else "awaiting_confirmation",
                                 running_at=started, deadline=started + self.auto["confirmation_seconds"],
                                 confirmation_token=secrets.token_urlsafe(32), pause_request_at=None,
                                 notices={"running": None} if not expired else {})
                w.LOG.info("Workspace is running. %s", "Startup timeout exceeded; cleanup required." if expired
                           else f"Confirm by {display_time(started + self.auto['confirmation_seconds'], self.auto)}.")
            elif resume.get("status") in ("failed", "cancelled", "unknown_error"):
                if status == "paused":
                    self.store.patch(phase="waiting", reason="Resume failed; waiting for another capacity opportunity.")
                    return self.cfg["interval_seconds"]
                self.finish("needs_attention", f"Resume failed and workspace is {status}. Check the VM manually.")
            elif status not in ("paused", "resuming"):
                self.finish("needs_attention", f"Unexpected status during resume: {status}.")
            # An in-flight resume is not submitted twice, even if the API still says paused.
            return self.auto["status_poll_seconds"]
        if phase == "awaiting_confirmation":
            if status != "running":
                self.finish("intervened", f"Workspace changed to {status} during confirmation. Automatic pause is disabled.")
                return None
            return min(self.auto["status_poll_seconds"], max(0, state["deadline"] - self.clock()))
        if phase == "pause_pending":
            if status == "paused":
                self.finish("paused", "The workspace is paused. No additional pause request was needed.")
                return None
            if status == "pausing":
                self.store.patch(phase="pausing", pause_request_at=state.get("pause_request_at") or self.clock())
                return self.auto["status_poll_seconds"]
            if status != "running":
                self.finish("needs_attention", f"Cannot pause a workspace in status {status}. Check it manually.")
                return None
            if state.get("pause_request_at") is not None:
                # The last POST may have succeeded. Observe; never blindly repeat it.
                if self.clock() - state["pause_request_at"] >= self.auto["resume_timeout_seconds"]:
                    self.finish("needs_attention", "Pause outcome is uncertain. No duplicate pause was submitted.")
                    return None
                return self.auto["status_poll_seconds"]
            if "pause" not in workspace["allowed_actions"]:
                raise w.WatchError("Pause is not allowed. Check the token's owner/admin permissions; the VM is still running.")
            self.store.patch(pause_request_at=self.clock())
            try:
                result = self.api.action("pause")
            except WorkspaceError as e:
                if not e.uncertain:
                    self.store.patch(pause_request_at=None)
                raise
            candidates = [a for a in result["workspace_actions"] if a["id"] not in set(state["baseline_actions"])
                          | {state["resume_action_id"]} and a.get("type") == "pause"]
            self.store.patch(phase="pausing", pause_action_id=candidates[0]["id"] if len(candidates) == 1 else None)
            w.LOG.info("Pause accepted; waiting for paused status.")
            return self.auto["status_poll_seconds"]


def confirmation_response(store, auto, method, token, *, clock=time.time):
    state = store.transaction()
    if not valid_token(state, token):
        return 403, "Invalid confirmation link", "This link is invalid or belongs to an earlier attempt.", False
    if state["phase"] == "confirmed":
        return 200, "VM confirmed", "Automatic pause has been cancelled for this lease.", False
    if state["phase"] != "awaiting_confirmation" or clock() >= state["deadline"]:
        return 410, "Confirmation window closed", "This lease can no longer be confirmed. Check the VM in SURF.", False
    if method == "POST":
        try:
            store.confirm(token, clock=clock)
        except w.WatchError:
            return 410, "Confirmation window closed", "The timeout won the race. Check the VM in SURF.", False
        return 200, "VM confirmed", "Automatic pause has been cancelled. You can now use your VM.", False
    return 200, "Confirm VM use", (f"Workspace: {state.get('workspace_name') or state['identity']['workspace_id']}. "
                                  f"Confirm before {display_time(state['deadline'], auto)} to keep it running."), True


def handler_for(store, auto):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Query strings contain bearer confirmation tokens.

        def do_GET(self):
            self.respond("GET")

        def do_POST(self):
            self.respond("POST")

        def respond(self, method):
            route = urllib.parse.urlsplit(self.path)
            token = None
            try:
                if route.path != "/confirm":
                    raise ValueError
                if method == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                        raise ValueError
                    parameters = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=4)
                else:
                    if len(route.query) > 4096:
                        raise ValueError
                    parameters = urllib.parse.parse_qs(route.query, max_num_fields=4)
                tokens = parameters.get("token", [])
                if len(tokens) != 1:
                    raise ValueError
                token = tokens[0]
                code, title, message, button = confirmation_response(store, auto, method, token)
            except (ValueError, UnicodeError, w.WatchError, OSError):
                code, title, message, button = 400, "Invalid request", "Unable to process this confirmation request.", False
            form = (f'<form method="post" action="/confirm"><input type="hidden" name="token" '
                    f'value="{html.escape(token, quote=True)}"><button type="submit">Confirm use and keep VM running</button></form>') if button else ""
            body = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                    f'<title>{html.escape(title)}</title><body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>'
                    f'{form}<p>Opening this page does not confirm use. Click the button to confirm.</p></body></html>').encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
    return Handler


@contextlib.contextmanager
def confirmation_server(store, auto):
    try:
        server = ThreadingHTTPServer((auto["confirmation"]["host"], auto["confirmation"]["port"]),
                                     handler_for(store, auto))
    except OSError:
        if store.transaction()["phase"] in ("resuming", "awaiting_confirmation", "pause_pending", "pausing"):
            w.LOG.error("Confirmation server unavailable after restart. Timed cleanup remains active; use the CLI confirm command if needed.")
            yield
            return
        raise w.WatchError("Cannot start the confirmation server. Check its port before attempting resume.") from None
    server.daemon_threads = True
    # Bound stalled HTTP clients; do not let them occupy a request thread indefinitely.
    original = server.get_request
    def get_request():
        connection, address = original()
        connection.settimeout(10)
        return connection, address
    server.get_request = get_request
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def send_notice(cfg, auto, state, event):
    message = EmailMessage()
    mail = w.validate_mail(cfg)
    message["From"], message["To"] = mail["from"], mail["to"]
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid()
    name = state.get("workspace_name") or auto["workspace_id"]
    if event == "running":
        link = auto["confirmation"]["base_url"] + "/confirm?" + urllib.parse.urlencode({"token": state["confirmation_token"]})
        deadline = display_time(state["deadline"], auto)
        message["Subject"] = f"[SURF] VM running: confirm use within {auto['confirmation_seconds'] / 60:g} minutes"
        message.set_content(f"Your workspace {name} is running.\nWorkspace ID: {auto['workspace_id']}\n"
                            f"Confirm by: {deadline}\n\nOpen this link and click the confirmation button:\n{link}\n\n"
                            "Without confirmation, the watcher will request Pause at the deadline.\n"
                            "Starting SSH or Jupyter does not count as confirmation.\n")
        message.add_alternative(f'<html lang="en"><body><h1>Your VM is running</h1><p>{html.escape(name)}</p>'
                                f'<p>Confirm by {html.escape(deadline)} to cancel automatic Pause.</p>'
                                f'<p><a href="{html.escape(link, quote=True)}">Open confirmation page</a></p>'
                                '<p>Opening the link alone does not confirm use. Click the button on the page.</p>'
                                '<p>Starting SSH or Jupyter does not count as confirmation.</p></body></html>', subtype="html")
    elif event == "paused":
        message["Subject"] = "[SURF] Unconfirmed VM is paused"
        message.set_content(f"Workspace {name} ({auto['workspace_id']}) was observed in paused status.\n"
                            "This watcher has finished and will not acquire GPUs again automatically.\n"
                            f"Reason: {state.get('reason', '')}\n")
    else:
        message["Subject"] = "[SURF] VM watcher needs attention"
        message.set_content(f"Workspace {name} ({auto['workspace_id']}) needs attention.\n"
                            f"Reason: {state.get('reason', '')}\n"
                            "GPU release has NOT been confirmed. Check the workspace in SURF.\n")
    w.deliver_message(cfg, message)


class MailWorker:
    """SMTP latency or failure must not stop the cleanup timer."""
    def __init__(self, cfg, auto, store, *, send=send_notice, clock=time.time):
        self.cfg, self.auto, self.store, self.send, self.clock = cfg, auto, store, send, clock
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="surf-mail")
        self.future = None
        self.pending = None
        self.next_retry = 0

    def pump(self):
        if self.future is not None:
            if not self.future.done():
                return
            event, lease_id = self.pending
            try:
                self.future.result()
            except Exception as e:
                w.LOG.error("Email %s failed (%s); VM cleanup remains active.", event, type(e).__name__)
                self.next_retry = self.clock() + 30
            else:
                def delivered(state):
                    if state.get("lease_id") == lease_id and event in state["notices"]:
                        state["notices"][event] = self.clock()
                self.store.transaction(delivered)
                w.LOG.info("SMTP accepted the %s notification.", event)
            self.future = None
        if self.clock() < self.next_retry:
            return
        state = self.store.transaction()
        for event, delivered in state.get("notices", {}).items():
            if delivered is not None:
                continue
            if event == "running" and (state["phase"] != "awaiting_confirmation" or self.clock() >= state["deadline"]):
                continue
            self.pending = (event, state.get("lease_id"))
            self.future = self.pool.submit(self.send, self.cfg, self.auto, state, event)
            return

    def close(self):
        self.pool.shutdown(wait=True)
        # Record a successful last email; no new submission is needed here.
        if self.future and self.future.done():
            event, lease_id = self.pending
            try:
                self.future.result()
            except Exception:
                w.LOG.error("Final notification failed. Check the VM and watcher logs.")
            else:
                def delivered(state):
                    if state.get("lease_id") == lease_id and event in state["notices"]:
                        state["notices"][event] = self.clock()
                self.store.transaction(delivered)
        # A slow earlier email may have occupied the worker when the VM finished.
        # Deliver the final event now, after cleanup, without affecting its timer.
        state = self.store.transaction()
        for event, delivered_at in state.get("notices", {}).items():
            if event == "running" or delivered_at is not None:
                continue
            try:
                self.send(self.cfg, self.auto, state, event)
            except Exception:
                w.LOG.error("Final %s notification failed. Check the VM and watcher logs.", event)
            else:
                self.store.transaction(lambda current: current["notices"].update({event: self.clock()}))


def run(cfg, auto):
    w.validate_mail(cfg)
    store = LeaseStore(cfg, auto)
    # A separate runner lock allows short state transactions from the web/CLI.
    with w.state_lock(store.path.with_name(store.path.name + ".runner")):
        state = store.transaction(create=True)
        if state["phase"] in FINISHED:
            w.LOG.info("Saved run is %s. Use a new auto.state_file to arm another run.", state["phase"])
            return
        with confirmation_server(store, auto):
            controller = Controller(cfg, auto, store)
            mailer = MailWorker(cfg, auto, store)
            try:
                while True:
                    try:
                        delay = controller.tick()
                    except w.WatchError as e:
                        state = store.transaction()
                        if state["phase"] == "resume_pending" and isinstance(e, WorkspaceError) and e.uncertain:
                            controller.finish("needs_attention", "Resume outcome is uncertain. Check the workspace; the request will not be duplicated.")
                            delay = None
                        elif state["phase"] in ("waiting", "resume_pending") and not e.retryable:
                            raise
                        else:
                            # Keep the guard running so refreshing a token/network can recover cleanup.
                            w.LOG.error("%s The cleanup guard remains active.", e)
                            delay = max(auto["status_poll_seconds"], e.retry_after)
                    mailer.pump()
                    if delay is None or store.transaction()["phase"] in FINISHED:
                        break
                    time.sleep(max(0, delay))
            finally:
                mailer.close()
    state = store.transaction()
    w.LOG.info("Automatic run finished: %s", state["phase"])
    if state["phase"] in ("intervened", "needs_attention"):
        raise w.WatchError(state.get("reason", "Check the workspace manually."))


def dispatch(cfg, command):
    auto = settings(cfg)
    if command == "workspace-check":
        diagnostic = copy.deepcopy(auto)
        diagnostic.pop("expected_hostname", None)
        diagnostic.pop("expected_flavour", None)
        workspace = WorkspaceAPI(cfg, diagnostic).get()
        print(json.dumps({"id": workspace["id"], "name": workspace.get("name"), "status": workspace["status"],
                          "hostname": workspace["meta"].get("host_name"),
                          "flavours": [f.get("name") for f in workspace["meta"].get("flavours", [])],
                          "allowed_actions": workspace["allowed_actions"]}, indent=2))
        print("Read-only check: no Resume or Pause was requested.")
        WorkspaceAPI(cfg, auto).validate(workspace)
        return
    if command == "auto":
        run(cfg, auto)
        return
    store = LeaseStore(cfg, auto)
    if command == "confirm":
        store.confirm()
        print("VM use confirmed. Automatic pause is cancelled for this lease.")
    elif command == "auto-status":
        state = store.transaction()
        print(json.dumps({k: state.get(k) for k in ("phase", "workspace_name", "last_status", "reason")}, indent=2))
        if state.get("deadline"):
            print("Confirmation deadline:", display_time(state["deadline"], auto))
