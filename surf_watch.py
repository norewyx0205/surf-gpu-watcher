#!/usr/bin/env python3
"""Watch SURF GPU capacity and notify or automatically resume a timed VM lease.

Python 3.10+, standard library only. See README.md before running.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import getpass
import json
import logging
import math
import os
from pathlib import Path
import random
import smtplib
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parsedate_to_datetime

LOG = logging.getLogger("surf-watch")
LEGACY_ENDPOINT = "https://gw.live.surfresearchcloud.nl/application-market/health/status/"
ENDPOINT = "https://gw.live.surfresearchcloud.nl/v1/application-market/health/status/"
PORTAL = "https://portal.live.surfresearchcloud.nl/help"
DEFAULT_TO = ""


class WatchError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def connection_error(error: Exception, service: str) -> WatchError:
    """Describe the transport failure without dumping URLs, proxy credentials or server replies."""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    retryable = True
    text_reason = reason.lower() if isinstance(reason, str) else ""
    if isinstance(reason, ssl.SSLCertVerificationError) or "certificate_verify_failed" in text_reason:
        detail = ("TLS certificate verification failed (CERTIFICATE_VERIFY_FAILED). "
                  "Check Python's trusted CA certificates; run doctor for certificate paths. "
                  "For python.org Python on macOS, run its Install Certificates.command.")
        retryable = False
    elif isinstance(reason, ssl.SSLError):
        detail = "TLS handshake failed (SSLError). Check the server's TLS mode/port and trusted certificates."
        retryable = False
    elif isinstance(reason, socket.gaierror):
        detail = "DNS lookup failed (gaierror). Check the hostname, DNS, and proxy/VPN settings."
    elif isinstance(reason, TimeoutError):
        detail = "Connection timed out (TimeoutError). Check reachability and firewall/proxy settings."
    elif isinstance(reason, ConnectionRefusedError):
        detail = "Connection refused (ConnectionRefusedError). Check the server/port and proxy settings."
    elif isinstance(reason, ConnectionResetError):
        detail = "Connection reset (ConnectionResetError). Check the network, VPN and firewall."
    elif isinstance(reason, OSError) and reason.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        detail = "Network or host unreachable. Check the network, VPN and routing."
    elif isinstance(reason, OSError) and reason.errno in (errno.EACCES, errno.EPERM):
        detail = "Connection blocked by OS permissions or network policy. Check the local firewall/policy."
        retryable = False
    elif "tunnel connection failed: 407" in text_reason:
        detail = "HTTPS proxy authentication failed (407). Check the configured proxy."
        retryable = False
    elif "tunnel connection failed" in text_reason:
        detail = "HTTPS proxy tunnel failed. Check the configured proxy and its destination policy."
    elif isinstance(reason, smtplib.SMTPNotSupportedError):
        detail = "The SMTP server does not support the requested TLS/authentication mode. Check SMTP settings."
        retryable = False
    elif isinstance(reason, smtplib.SMTPServerDisconnected):
        detail = "SMTP server disconnected. Check SMTP security/port and network reachability."
    else:
        error_type = type(reason).__name__ if isinstance(reason, BaseException) else type(error).__name__
        detail = f"Connection failed ({error_type}). Run doctor to check TLS, DNS and proxy settings."
    return WatchError(f"{service}: {detail}", retryable=retryable)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_catalog_endpoint(endpoint):
    """The browser route rejects personal tokens; use its versioned API equivalent."""
    return ENDPOINT if endpoint == LEGACY_ENDPOINT else endpoint


def private_write(path: Path, text: str) -> None:
    """Atomically replace a file; create it with user-only permissions on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WatchError(f"Cannot read JSON file: {path}. Check the file and its format.") from None
    if not isinstance(value, dict):
        raise WatchError(f"The top-level JSON value must be an object: {path}")
    return value


def load_config(path: Path) -> dict:
    cfg = read_json(path)
    cfg["_dir"] = path.resolve().parent
    cfg.setdefault("endpoint", ENDPOINT)
    cfg["endpoint"] = canonical_catalog_endpoint(cfg["endpoint"])
    cfg.setdefault("status_key", "a10")
    cfg.setdefault("min_free", 1)
    cfg.setdefault("interval_seconds", 60)
    cfg.setdefault("timeout_seconds", 20)
    cfg.setdefault("state_file", "state.json")
    cfg.setdefault("token_file", "surf-token.txt")
    if not isinstance(cfg["endpoint"], str):
        raise WatchError("endpoint must be a URL string.")
    try:
        url = urllib.parse.urlsplit(cfg["endpoint"])
        port = url.port
    except ValueError:
        raise WatchError("Invalid endpoint URL.") from None
    if (url.scheme != "https" or url.hostname != "gw.live.surfresearchcloud.nl"
            or url.username or url.password or url.query or url.fragment
            or port not in (None, 443)
            or url.path not in ("/application-market/health/status/",
                                "/v1/application-market/health/status/")):
        raise WatchError("endpoint must be a SURF Catalog health/status HTTPS URL.")
    if not isinstance(cfg["status_key"], str) or not cfg["status_key"].strip():
        raise WatchError("status_key must be a nonempty field name.")
    if type(cfg["min_free"]) is not int or cfg["min_free"] < 1:
        raise WatchError("min_free must be a positive integer.")
    for key, minimum in (("interval_seconds", 15), ("timeout_seconds", 1)):
        value = cfg[key]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value < minimum):
            raise WatchError(f"{key} must be at least {minimum} seconds.")
    for key in ("state_file", "token_file"):
        if not isinstance(cfg[key], str) or not cfg[key]:
            raise WatchError(f"{key} must be a nonempty file path.")
    return cfg


def local_path(cfg: dict, name: str) -> Path:
    return cfg["_dir"] / name


def secret(cfg: dict, env_name: str, filename: str) -> str:
    value = os.environ.get(env_name)
    if value is None:
        try:
            value = local_path(cfg, filename).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            raise WatchError(f"Missing {env_name}. Run init or configure the credential file.") from None
    if not value:
        raise WatchError(f"{env_name} must not be empty.")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the authorization token to a redirect destination.
        return None


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        result = float(value)
        return max(0, result) if math.isfinite(result) else 0
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def fetch_status(cfg: dict) -> dict:
    token = secret(cfg, "RESEARCH_CLOUD_TOKEN", cfg["token_file"]).strip()
    if not token or any(c in token for c in "\r\n"):
        raise WatchError("The SURF token must be nonempty and must not contain newlines.")
    request = urllib.request.Request(cfg["endpoint"], method="GET", headers={
        "authorization": token,  # SURF documents the raw token, without adding Bearer.
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "surf-gpu-watch/1.0",
    })
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=cfg["timeout_seconds"]) as response:
            if response.status != 200:
                raise WatchError(f"Unexpected SURF HTTP status: {response.status}.")
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise WatchError("SURF response exceeds 1 MiB; refusing to interpret it.")
            payload = json.loads(raw)
    except urllib.error.HTTPError as e:
        status = e.code
        retry_after = retry_after_seconds(e.headers.get("Retry-After")) if e.headers else 0
        e.close()
        if status == 401:
            raise WatchError("SURF HTTP 401: invalid or expired API token. Check your API token.") from None
        if status == 403:
            raise WatchError("SURF HTTP 403: the token is not permitted to access this Catalog endpoint. Check the API URL and endpoint permissions.") from None
        raise WatchError(f"SURF HTTP {status}.", retryable=status in (408, 429) or status >= 500,
                         retry_after=retry_after) from None
    except (urllib.error.URLError, OSError) as e:
        raise connection_error(e, "SURF connection") from None
    except (ValueError, UnicodeError):
        raise WatchError("SURF returned invalid JSON; refusing to interpret it.") from None
    if not isinstance(payload, dict):
        raise WatchError("The SURF status response must be a JSON object. Check the endpoint.")
    return payload


def availability(payload: dict, key: str, minimum: int = 1) -> tuple[bool, str, bool | int]:
    """Exact key match, case-insensitive. Never treat an arbitrary value as truthy."""
    matches = [k for k in payload if k.casefold() == key.casefold()]
    if len(matches) != 1:
        keys = ", ".join(sorted(payload))
        raise WatchError(f"Cannot find a unique field {key!r}. Fields: {keys}. Run check to verify status_key.")
    actual = matches[0]
    value = payload[actual]
    if type(value) is bool:
        if minimum != 1:
            raise WatchError("A boolean status cannot confirm multiple free GPUs. Use min_free=1 as an availability trigger.")
        return value, actual, value
    if type(value) is int and value >= 0:
        return value >= minimum, actual, value
    raise WatchError(f"Field {actual!r} has type {type(value).__name__}; expected a boolean or nonnegative integer.")


def validate_mail(cfg: dict, *, credentials: bool = True) -> dict:
    mail = cfg.get("smtp")
    if not isinstance(mail, dict):
        raise WatchError("Missing smtp configuration. Run init.")
    for key in ("host", "from", "to"):
        if not isinstance(mail.get(key), str) or not mail[key].strip():
            raise WatchError(f"Configure smtp.{key}.")
        if any(c in mail[key] for c in "\r\n"):
            raise WatchError(f"smtp.{key} must not contain newlines.")
    for key in ("from", "to"):
        address = mail[key]
        if address.count("@") != 1 or any(c.isspace() or c in ",;<>" for c in address):
            raise WatchError(f"smtp.{key} must contain one email address.")
    if any(c in mail["host"] for c in "/@ \t"):
        raise WatchError("smtp.host must be a hostname or IP address, without a URL, user information or whitespace.")
    if mail.get("security") not in ("ssl", "starttls"):
        raise WatchError("smtp.security must be ssl (usually port 465) or starttls (usually port 587).")
    if type(mail.get("port")) is not int or not 1 <= mail["port"] <= 65535:
        raise WatchError("smtp.port must be a valid port number.")
    if not isinstance(mail.get("username", ""), str):
        raise WatchError("smtp.username must be a string.")
    if mail.get("username") and credentials:
        filename = mail.get("password_file", "smtp-password.txt")
        if not isinstance(filename, str) or not filename:
            raise WatchError("smtp.password_file must be a nonempty file path.")
        secret(cfg, "SMTP_PASSWORD", filename)
    return mail


def send_email(cfg: dict, actual_key: str, value: bool | int, *, test: bool = False) -> None:
    mail = validate_mail(cfg)
    message = EmailMessage()
    message["From"] = mail["from"]
    message["To"] = mail["to"]
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid()
    if test:
        message["Subject"] = "[SURF] GPU watcher test email"
        text = "This is a delivery test. It does not mean that an A10 GPU is available.\n"
    else:
        label = cfg["status_key"].upper()
        message["Subject"] = f"[SURF] {label} available: try resuming your VM"
        text = (f"SURF Catalog information reports {label} as available.\n"
                f"Detected at (UTC): {now()}\n"
                f"API field: {actual_key} = {json.dumps(value)}\n"
                f"Open {PORTAL} and try resuming your VM.\n\n"
                "This notification does not reserve GPU capacity.\n"
                "A boolean does not report a GPU count or guarantee that a multi-GPU VM can resume.\n")
    message.set_content(text)
    deliver_message(cfg, message)


def deliver_message(cfg: dict, message: EmailMessage) -> None:
    """Send a prepared message using the configured SMTP transport."""
    mail = validate_mail(cfg)
    context = ssl.create_default_context()
    try:
        if mail["security"] == "ssl":
            connection = smtplib.SMTP_SSL(mail["host"], mail["port"],
                                          timeout=cfg["timeout_seconds"], context=context)
        else:
            connection = smtplib.SMTP(mail["host"], mail["port"], timeout=cfg["timeout_seconds"])
        with connection as smtp:
            if mail["security"] == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            if mail.get("username"):
                smtp.login(mail["username"], secret(cfg, "SMTP_PASSWORD",
                                                   mail.get("password_file", "smtp-password.txt")))
            refused = smtp.send_message(message)
            if refused:
                raise WatchError("SMTP did not accept the recipient. Check smtp.to.")
    except smtplib.SMTPAuthenticationError:
        raise WatchError("SMTP authentication failed. Check the account, app password, and SMTP support.") from None
    except smtplib.SMTPResponseException as e:
        raise WatchError(f"SMTP returned {e.smtp_code}; delivery was not confirmed.",
                         retryable=400 <= e.smtp_code < 500) from None
    except smtplib.SMTPRecipientsRefused:
        raise WatchError("SMTP rejected the recipient. Check smtp.to.") from None
    except (smtplib.SMTPException, OSError) as e:
        raise connection_error(e, "SMTP connection or sending failed; delivery was not confirmed") from None


def doctor(cfg: dict) -> bool:
    """Probe connections without reading credentials, authenticating, emailing or changing a VM."""
    paths = ssl.get_default_verify_paths()
    proxies = urllib.request.getproxies()
    print("Read-only connection diagnostics: no credential files are read, no mail is sent, and no VM action is requested.")
    print("Python:", sys.version.split()[0])
    print("Executable:", sys.executable)
    print("Platform:", sys.platform)
    print("OpenSSL:", ssl.OPENSSL_VERSION)
    print("Default CA file:", paths.cafile or "(missing)")
    print("Default CA directory:", paths.capath or "(missing)")
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        print(name + ":", "set" if os.environ.get(name) else "not set")
    print("HTTPS proxy detected:", "yes (address and credentials hidden)" if proxies.get("https") else "no")
    print("SMTP uses a direct TCP connection; HTTP/HTTPS proxy settings do not proxy smtplib.")
    if sys.platform == "darwin":
        installer = Path(f"/Applications/Python {sys.version_info.major}.{sys.version_info.minor}/Install Certificates.command")
        if installer.exists():
            print("python.org certificate installer:", installer)
    passed = True
    try:
        context = ssl.create_default_context()
        print("Trusted CA certificates:", context.cert_store_stats().get("x509_ca", 0))
    except (OSError, ValueError) as e:
        print("TLS context: FAIL -", connection_error(e, "TLS configuration"))
        return False
    # An unauthenticated HTTP response (usually 401) proves DNS/TCP/TLS worked.
    request = urllib.request.Request(cfg["endpoint"], method="GET", headers={"User-Agent": "surf-gpu-watch/doctor"})
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=cfg["timeout_seconds"]) as response:
            print(f"SURF HTTPS: OK - HTTP {response.status}; authentication was not tested.")
    except urllib.error.HTTPError as e:
        print(f"SURF HTTPS: OK - HTTP {e.code}; an HTTP response was received without sending a token.")
        e.close()
    except (urllib.error.URLError, OSError) as e:
        print("SURF HTTPS: FAIL -", connection_error(e, "SURF connection"))
        passed = False
    try:
        mail = validate_mail(cfg, credentials=False)
        print(f"SMTP target: {mail['host']}:{mail['port']} ({mail['security']})")
        if mail["security"] == "ssl":
            smtp = smtplib.SMTP_SSL(mail["host"], mail["port"], timeout=cfg["timeout_seconds"], context=context)
        else:
            smtp = smtplib.SMTP(mail["host"], mail["port"], timeout=cfg["timeout_seconds"])
        with smtp:
            smtp.ehlo()
            if mail["security"] == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
        print("SMTP TCP/TLS: OK - login and email delivery were not tested.")
    except WatchError as e:
        print("SMTP configuration: FAIL -", e)
        passed = False
    except (smtplib.SMTPException, OSError) as e:
        print("SMTP TCP/TLS: FAIL -", connection_error(e, "SMTP connection"))
        passed = False
    print("After fixing connection failures, rerun check, workspace-check, and test-email.")
    return passed


@contextlib.contextmanager
def state_lock(path: Path, *, blocking: bool = False):
    """Prevent two watcher processes using the same state from sending twice."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            raise WatchError("Another process is using this state file. Do not start a duplicate watcher.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            # Closing the descriptor releases the POSIX lock, even after a crash.


def watch(cfg: dict, *, fetch=fetch_status, send=send_email, sleep=time.sleep) -> None:
    mail = validate_mail(cfg)
    path = local_path(cfg, cfg["state_file"])
    with state_lock(path):
        if path.exists():
            state = read_json(path)
            if (canonical_catalog_endpoint(state.get("endpoint")) != cfg["endpoint"]
                    or state.get("status_key") != cfg["status_key"].casefold()
                    or state.get("min_free") != cfg["min_free"]
                    or state.get("to") != mail["to"]):
                raise WatchError("This state file belongs to a different configuration. Use a new state_file.")
            state["endpoint"] = cfg["endpoint"]
            if state.get("notified_at"):
                LOG.info("This task already notified you (%s). Use a new state_file for another wait.",
                         state["notified_at"])
                return
        else:
            state = {"endpoint": cfg["endpoint"], "status_key": cfg["status_key"].casefold(),
                     "min_free": cfg["min_free"], "to": mail["to"],
                     "notified_at": None}
        # Persist before contacting the API so an unwritable state path fails early.
        private_write(path, json.dumps(state, indent=2) + "\n")
        failures = 0
        while True:
            try:
                ready, key, value = availability(fetch(cfg), cfg["status_key"], cfg["min_free"])
                state.update(checked_at=now(), last_value=value)
                private_write(path, json.dumps(state, indent=2) + "\n")
                LOG.info("%s = %s; %s", key, json.dumps(value), "available" if ready else "still waiting")
                if ready:
                    send(cfg, key, value)
                    state["notified_at"] = now()
                    try:
                        private_write(path, json.dumps(state, indent=2) + "\n")
                    except OSError:
                        raise WatchError("SMTP accepted the email but saving notification state failed. Avoid restarting this task to prevent duplicates.") from None
                    LOG.info("SMTP accepted the notification for %s. Watcher finished.", cfg["smtp"]["to"])
                    return
                failures = 0
                delay = cfg["interval_seconds"]
            except WatchError as e:
                if not e.retryable:
                    raise
                failures += 1
                backoff = min(900, cfg["interval_seconds"] * 2 ** min(failures, 10))
                delay = max(e.retry_after, backoff) + random.uniform(0, 5)
                LOG.warning("%s Retrying in %.1f seconds; no availability notification was sent.", e, delay)
            sleep(delay)


def initialize(path: Path) -> None:
    cfg = {
        "endpoint": ENDPOINT, "status_key": "a10", "min_free": 1,
        "interval_seconds": 60, "timeout_seconds": 20,
        "token_file": "surf-token.txt", "state_file": "state.json",
        "smtp": {"host": "", "port": 465, "security": "ssl", "username": "",
                 "password_file": "smtp-password.txt", "from": "", "to": DEFAULT_TO},
    }
    paths = [path, path.parent / "surf-token.txt", path.parent / "smtp-password.txt"]
    if any(p.exists() for p in paths):
        raise WatchError("Configuration or credential files already exist. init will not overwrite them; edit config.json.")
    print("Create a SURF token under Profile > API tokens. Credential input is hidden.")
    token = getpass.getpass("SURF personal API token: ").strip()
    if not token or any(c in token for c in "\r\n"):
        raise WatchError("The token must be nonempty and must not contain newlines.")
    mail = cfg["smtp"]
    mail["host"] = input("SMTP host (provided by your email service): ").strip()
    security = input("SMTP security [ssl/starttls, default ssl]: ").strip() or "ssl"
    if security not in ("ssl", "starttls"):
        raise WatchError("Enter ssl or starttls.")
    mail["security"] = security
    default_port = 465 if security == "ssl" else 587
    try:
        mail["port"] = int(input(f"SMTP port [{default_port}]: ").strip() or default_port)
    except ValueError:
        raise WatchError("SMTP port must be an integer.") from None
    mail["username"] = input("SMTP username (blank for an authorized relay): ").strip()
    password = getpass.getpass("SMTP password / app password: ") if mail["username"] else ""
    mail["from"] = input("Sender address (authorized by the email service): ").strip()
    mail["to"] = input(f"Recipient address: ").strip() or DEFAULT_TO
    workspace_id = input("Workspace ID for automatic mode (blank for notification-only): ").strip()
    if workspace_id:
        cfg["auto"] = {"workspace_id": workspace_id, "state_file": "auto-state.json",
                       "confirmation_seconds": 600, "status_poll_seconds": 5,
                       "resume_timeout_seconds": 900, "display_timezone": "Europe/Amsterdam",
                       "confirmation": {"host": "127.0.0.1", "port": 8765,
                                        "base_url": "http://127.0.0.1:8765"}}
    private_write(paths[1], token + "\n")
    if mail["username"]:
        private_write(paths[2], password + "\n")
    private_write(path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    validate_mail(load_config(path))
    print(f"Saved configuration: {path.resolve()}. Credential files use owner-only permissions on POSIX.")
    print("Next: run check, then test-email. Use watch for notification-only mode or auto for automatic VM management.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="Path to the configuration JSON file")
    parser.add_argument("command", choices=("init", "check", "test-email", "watch",
                                           "auto", "workspace-check", "confirm", "auto-status", "doctor"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "init":
            initialize(args.config)
            return 0
        cfg = load_config(args.config)
        if args.command == "doctor":
            return 0 if doctor(cfg) else 75
        if args.command == "check":
            payload = fetch_status(cfg)
            print("Catalog status fields (booleans/integers only; no email is sent):")
            print(json.dumps({k: v for k, v in payload.items() if type(v) in (bool, int)},
                             ensure_ascii=False, indent=2))
            ready, key, value = availability(payload, cfg["status_key"], cfg["min_free"])
            print(f"{key} = {json.dumps(value)}; {'available' if ready else 'unavailable'}")
        elif args.command == "test-email":
            send_email(cfg, cfg["status_key"], False, test=True)
            LOG.info("SMTP accepted the test email. Check your inbox.")
        elif args.command == "watch":
            watch(cfg)
        else:
            from surf_auto import dispatch
            dispatch(cfg, args.command)
        return 0
    except WatchError as e:
        LOG.error("%s", e)
        return 75 if e.retryable else 78
    except OSError:
        LOG.error("A local file or system operation failed. Check permissions and free disk space.")
        return 75 if args.command == "auto" else 78
    except KeyboardInterrupt:
        LOG.info("Watcher stopped.")
        return 130


if __name__ == "__main__":
    # Lazy auto-mode imports must share this module's exception and lock classes.
    sys.modules["surf_watch"] = sys.modules[__name__]
    raise SystemExit(main())
