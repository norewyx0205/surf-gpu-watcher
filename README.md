# SURF GPU Watcher

A Python tool for SURF Research Cloud. Requires **Python 3.10+**, with no third-party packages.

Automatic mode performs one acquisition cycle:

1. Check Catalog GPU availability every **60 seconds** while waiting.
2. Request **Resume** for one explicitly configured, paused workspace when capacity is reported available.
3. Wait for the VM to actually reach **running**. An accepted API request is not treated as a running VM.
4. Email a confirmation link and start a **10-minute window** from the observed running time.
5. Opening the link displays a page. Clicking **Confirm use and keep VM running** cancels automatic Pause.
6. Without confirmation, request **Pause**, observe **paused**, email a release notification, and finish.

After confirmation, the VM stays running and the watcher finishes. After timeout cleanup, it finishes too; it does not repeatedly reacquire GPUs. A new state-file name arms another cycle. Notification-only mode from version 1 remains available with `watch`.

## Setup and verification

Run the watcher on an **independent, always-on computer or server**, never on the GPU VM it controls. Keep it awake and connected. Its automatic Pause guard is a local process, not a timer installed in SURF.

Create your own SURF token under **Profile > API tokens**. It must have permission to Resume and Pause the selected workspace. Being able to log in to Jupyter or belonging to the collaboration does not necessarily grant these actions. Prepare SMTP host, port, TLS mode, authorized sender, username and password/app password. Sender and recipient can differ. SMTP username/password and authorized relays are supported; SMTP OAuth is not implemented.

For a new installation:

```bash
python3 surf_watch.py init
```

Follow the [step-by-step quick start](QUICKSTART.md) for every wizard field, a Gmail-to-Outlook example, and common troubleshooting results.

The English wizard explains all nine fields, checks answers before saving, and lets you retry invalid input. Credential entry is hidden; no characters appearing while you type or paste is normal. Setup does not connect to SURF, send email, or operate a VM. It creates `config.json`, `surf-token.txt` and, when required, `smtp-password.txt`. Linux/macOS files use owner-only permissions. Restrict directory access using Windows file permissions on Windows; use `python` instead of `python3` there.

For a Gmail sender, use `smtp.gmail.com`, `ssl`/465 or `starttls`/587, your full Gmail address as username, and a [Google App Password](https://myaccount.google.com/apppasswords). Enable 2-Step Verification first; use the 16-character app password without grouping spaces, rather than your normal Google password. Some managed accounts restrict app passwords; see [Google's requirements](https://support.google.com/accounts/answer/185833). Your recipient can be a university Outlook address; its provider does not determine the sending settings.

Alternatively, copy `config.example.json` to `config.json` and fill it in. Environment variables `RESEARCH_CLOUD_TOKEN` and `SMTP_PASSWORD` override the credential files. Relative paths resolve against the configuration directory. SURF tokens are sent in `authorization` without adding `Bearer`, as SURF documents.

Catalog polling uses `https://gw.live.surfresearchcloud.nl/v1/application-market/health/status/`. The unversioned browser route can return 403 for a valid personal API token even while workspace queries work. Existing configurations using that exact old route are normalized to `/v1/` when loaded, so there is no need to rerun `init` or replace credentials. Existing lease deadlines and notification records are preserved through this URL migration.

For a version 1 installation, keep your SMTP settings and credentials, add the sample's `auto` section, and supply a workspace UUID. Use a distinct `auto.state_file`; do not reuse the notification-only state file.

Verify before arming automatic mode:

```bash
python3 surf_watch.py check
python3 surf_watch.py workspace-check
python3 surf_watch.py test-email
```

- `check` only reads Catalog information once. `a10 = 0; unavailable` is a successful query reporting no current capacity, not an error. It does not start polling. Compare the exact field against Help > Catalog information. It accepts booleans or nonnegative integers and refuses strings such as `"false"`, missing fields, and unexpected structures.
- `workspace-check` only reads the VM. Verify the ID, hostname, flavour, status and allowed actions. The VM should be paused and `resume` should be allowed. An empty `allowed_actions` list while `resuming` or `pausing` can be normal; wait and check again. `pause` normally appears when running; its absence from a paused VM's actions does not by itself prove a permission problem.
- `test-email` sends a clearly labelled test. Confirm inbox receipt, including spam. SMTP acceptance does not guarantee delivery.

Then start:

```bash
python3 surf_watch.py auto
```

**`auto` makes real Resume/Pause requests.** Resuming consumes your SURF budget until the VM is paused. Keep the process running until it reports `confirmed` or `paused`.

An already-running VM is not adopted or automatically paused. Unexpected new workspace actions during the lease stop automatic control and report intervention. These checks cannot detect a colleague logging in via SSH/Jupyter or a job starting at boot. Coordinate use of shared VMs, and confirm before starting work you do not want interrupted. Logging into SSH/Jupyter **does not count as confirmation**.

Useful commands on the watcher machine:

```bash
python3 surf_watch.py auto-status
python3 surf_watch.py confirm
```

`auto-status` shows the phase and deadline without printing the link token. `confirm` is a fallback that uses the same deadline and state transaction as the web button; it does not contact SURF.

To arm another cycle after the process finishes, change `auto.state_file`, for example to `auto-state-next.json`. Do not delete active state or lock files. A finished run does not silently rearm after restart.

## Troubleshoot connection failures on macOS

If Catalog, workspace and SMTP checks all fail immediately, first run:

```bash
python3 surf_watch.py doctor
```

This diagnostic reads no token/password files, sends no credentials or email, and makes no VM actions. It reports the Python installation, trusted certificate paths, whether an HTTPS proxy is detected, and separate SURF HTTPS / SMTP TLS connection results. An unauthenticated SURF HTTP 401 means the HTTPS connection worked; it does not verify your token. SMTP is probed without logging in or sending a message. Proxy addresses and credentials are hidden.

`CERTIFICATE_VERIFY_FAILED` usually needs repair of the Python installation's trusted certificates. For python.org Python on macOS, run **Install Certificates.command** in `/Applications/Python X.Y/` for the interpreter version in the diagnostic. This is a documented part of [installing Python on macOS](https://docs.python.org/3/using/mac.html). `cafile=None` and `capath=None` are a strong clue that default certificate locations are unavailable. Install a supported Python version (3.10+) if your current interpreter is older, and complete its matching certificate-installation step. Existing watcher configuration and credential files can be retained.

For DNS failures, check the SMTP hostname and local DNS/proxy/VPN settings. For connection refusal or timeout, check the configured port and network/firewall reachability. Python's `smtplib` uses a direct TCP connection and does not tunnel SMTP through HTTP/HTTPS proxy variables. For Gmail, use `smtp.gmail.com` with `ssl`/465 or `starttls`/587.

After repairing transport problems, rerun `check`, `workspace-check`, and `test-email` to verify authentication and delivery. Certificate verification remains enabled; the diagnostic does not install certificates or alter proxy settings.

## Confirmation links

The confirmation server binds to **127.0.0.1:8765**, starting before a new Resume. Each lease gets a new unguessable token. A link can only confirm that lease; it contains no SURF or SMTP credentials. GET never confirms use: only a button POST does. The page has no third-party assets, and token-bearing requests are excluded from application access logs.

Choose an arrangement reachable from the browser where you open email.

### Watcher and browser on the same computer

Keep the sample's `base_url` as `http://127.0.0.1:8765`. This link does not work from a phone or another computer.

### Remote server through SSH forwarding

Keep the same base URL on the server. Maintain this tunnel on the computer where you open email:

```bash
ssh -N -L 8765:127.0.0.1:8765 YOUR_USER@YOUR_SERVER
```

The localhost link reaches the server through the tunnel. Disconnecting the tunnel prevents web confirmation but does not stop a guard running on the server. You can still SSH to the server and run `confirm` before the deadline.

### Remote server with an HTTPS domain

Set `auto.confirmation.base_url` to a reachable HTTPS origin, e.g. `https://gpu-watch.example.org`. Forward the root URL through an HTTPS reverse proxy to `127.0.0.1:8765`. DNS, TLS and proxy deployment are separate setup steps. `Caddyfile.example` provides a minimal template; replace its domain and follow Caddy's hosting/TLS requirements.

Do not enable query-string access logging. Public HTTP URLs are rejected; the local server remains bound to loopback. Use a domain root, not a URL subpath. If an active lease's restarted watcher cannot bind the confirmation port, it keeps the timed guard active and logs the issue; CLI confirmation is still available.

## Configuration and multi-GPU workspaces

The generic sample and shareable archive contain no personal recipient or VM ID. Each colleague supplies their own configuration, credentials and independent state file.

| Setting | Default / meaning |
| --- | --- |
| `status_key` | `a10`; verify the field with `check` |
| `interval_seconds` | `60`; capacity polling while waiting |
| `min_free` | `1`; trigger threshold, **not the VM's GPU count** |
| `timeout_seconds` | `20`; per-network-operation timeout |
| `auto.workspace_id` | Exact workspace UUID to control |
| `auto.expected_hostname` | Optional check against `meta.host_name` |
| `auto.expected_flavour` | Optional exact check against `meta.flavours` names |
| `auto.confirmation_seconds` | `600`; window after observed running |
| `auto.status_poll_seconds` | `5`; observation during resume, confirmation and cleanup |
| `auto.resume_timeout_seconds` | `900`; operation observation timeout |
| `auto.display_timezone` | `Europe/Amsterdam`; UTC fallback if system timezone data is missing |
| `auto.state_file` | `auto-state.json`; durable lease state |

For **A10 - 2 GPU**, a boolean A10 indicator may only mean some capacity exists. Keep `min_free=1` as that trigger; a value of 2 makes the tool refuse a boolean it cannot interpret as a GPU count. The selected VM's actual flavour determines the allocation requested. A rejected Resume or failed action returning to paused is followed by a fresh availability check. A one-GPU opportunity cannot guarantee a two-GPU allocation, and another user can still win the race.

An authenticated Catalog response has been verified to contain integer A10/RTX2080 indicators. Only use a numeric quantity threshold after verifying with SURF that the value represents the free GPU count relevant to your flavour; the data type alone does not establish that meaning.

## Persistence and limitations

The owner-only state file saves phase, action IDs, confirmation token and original deadline. A runner lock prevents duplicate watchers; short state locks serialize timeout with confirmation. Restarting does not reset the window. Confirmation committed before expiry wins; late confirmation cannot undo an initiated Pause.

SMTP runs in a background worker. Slow or failed email neither confirms the lease nor stops cleanup. Only an observed `paused` state is reported as released. A rejected/failed Pause does not produce a false success message.

**Ten minutes is the cleanup initiation deadline, not a guarantee of release at exactly ten minutes.** Network/API latency, rate limits, permissions and SURF's asynchronous queue can delay or prevent Pause. While the watcher computer is asleep/offline or its process is stopped, the local guard cannot act. Restarting retains the original deadline but cannot enforce it retroactively. Use an independent always-on host and supervisor.

The API does not document action idempotency keys. An uncertain Resume response, or a crash between requesting Resume and saving its response, requires manual inspection. The tool does not blindly repeat the request or claim ownership of a potentially external resume. An uncertain Pause is observed without duplicate submissions. Inspect SURF when the phase is `needs_attention` or `intervened`; the watcher has not confirmed GPU release in those states.

If resume exceeds the 15-minute operation timeout, a later running state leads directly to cleanup instead of opening a fresh confirmation window. In-flight queued actions are not submitted twice. Rare duplicate emails remain possible when an SMTP server accepts a message just before a response is lost or the process crashes; this does not extend the lease.

## Linux service

Edit user paths and Python path in `surf-a10-watch.service.example`. It uses `auto`; change it to `watch` for notification-only mode.

```bash
mkdir -p ~/.config/systemd/user
cp surf-a10-watch.service.example ~/.config/systemd/user/surf-a10-watch.service
systemd-analyze --user verify ~/.config/systemd/user/surf-a10-watch.service
systemctl --user daemon-reload
systemctl --user enable --now surf-a10-watch.service
journalctl --user -u surf-a10-watch.service -f
```

If needed, an administrator can enable user lingering with `loginctl enable-linger YOUR_USER` so the service survives logout and starts on boot. The template restarts crashes/temporary local failures, retains lease state, and does not restart successful finished runs or permanent configuration errors.

Stopping the service also stops its guard; check an active VM manually or restart the guard:

```bash
systemctl --user stop surf-a10-watch.service
```

`systemd-analyze verify` is blocked in this delivery's sandbox by `SO_PASSCRED failed: Operation not permitted`. Validate the service on the actual Linux host. This delivery installs no service.

## Sources and validation

Verified on **13 October 2026**:

- The current [SURF portal](https://portal.live.surfresearchcloud.nl/help) uses the unversioned Catalog health route in its browser UI. A user-authenticated test confirmed that the `/v1/application-market/health/status/` equivalent accepts a personal API token; the unversioned route returned 403 for the same account. The versioned response includes `a10` and `rtx2080` integer indicators. This health endpoint is absent from public Catalog Swagger and may change.
- [Official Workspace Swagger](https://gw.live.surfresearchcloud.nl/v1/workspace/swagger/docs/) documents `GET /v1/workspace/workspaces/{id}/` and `POST /v1/workspace/workspaces/{id}/actions/{action_type}/`, with `resume`/`pause`, empty JSON objects, and workspace responses exposing status/action history.
- [Workspace actions](https://servicedesk.surf.nl/wiki/spaces/WIKI/pages/19825316/Workspace%2Bactions) documents resuming/running and pausing/paused transitions.
- [Workspace admin access](https://servicedesk.surf.nl/wiki/spaces/WIKI/pages/37388582/Sharing%2Bcontrol%2Bworkspace%2Badmin) describes owner and `src_ws_admin` action permissions.
- [API first steps](https://servicedesk.surf.nl/wiki/spaces/WIKI/pages/174981256/Research%2BCloud%2BAPI%2B-%2BFirst%2BSteps) covers personal tokens and authentication.

Run synthetic tests:

```bash
python3 -m unittest discover -s tests -v
```

Tests cover wizard validation and cancellation, parsing, exact target selection, GET versus POST confirmation, timeout races, original restart deadlines, duplicate prevention, uncertain action outcomes, permission recovery, external intervention, failed Pause, SMTP latency/failure, and transport diagnostics. They do not access real SURF workspaces or send real email. The user's local checks confirmed authenticated Catalog/workspace reads and SMTP acceptance after repairing Python's CA certificates. Actual Resume/Pause operations remain unverified by this delivery. Complete the actual host's read-only and inbox delivery checks before arming it.
