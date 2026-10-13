# First-time setup

This guide configures A10 polling every 60 seconds. Automatic mode resumes your paused VM when availability is reported, emails you when it reaches running, and requests Pause if you do not confirm within 10 minutes. The confirmation window starts when running is observed, not when Resume is requested.

Use Python 3.10+ on a computer that can stay awake and connected. Run the tool from its downloaded/cloned directory, on a computer independent of the GPU VM. On Windows, replace `python3` below with `python`.

## Prepare three things

1. **SURF personal API token:** open the [SURF portal](https://portal.live.surfresearchcloud.nl/), then **Profile > API tokens**. For automatic mode, your account must have Resume/Pause rights for the target workspace. Read access or Jupyter access alone does not establish these rights.
2. **Sending account credentials:** for Gmail, enable Google **2-Step Verification**, then create an [App Password](https://myaccount.google.com/apppasswords), for example named `SURF GPU Watcher`. Save its 16-character value for the hidden password prompt. Some school/work accounts prohibit app passwords; see [Google's help](https://support.google.com/accounts/answer/185833). The tool supports SMTP passwords/app passwords and authorized relays; it does not implement OAuth login.
3. **Workspace UUID:** open your workspace's details in SURF and copy **Id**. It looks like `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`. The hostname, IP address, Jupyter URL, and collaboration name are different fields.

The sender and recipient can be different accounts. **Gmail sender + university Outlook recipient** works: configure Gmail's SMTP settings, then put the school address in the recipient field.

## Run the English wizard

```bash
python3 surf_watch.py init
```

Paste each value at its own prompt and press Enter. Tokens/passwords display no characters while typing or pasting; this is expected. Invalid answers are explained and requested again. Nothing is saved until all fields pass validation; no email or VM action occurs during setup.

| Step | Field | What to enter for a Gmail sender |
| --- | --- | --- |
| 1 | SURF personal API token | Your own token, without a `Bearer` prefix. Hidden input. |
| 2 | SMTP host | `smtp.gmail.com` |
| 3 | SMTP security | Press Enter for `ssl`. This is the connection mode; no credential is needed here. Alternatively enter `starttls`. |
| 4 | SMTP port | Press Enter for `465` with `ssl`, or `587` with `starttls`. |
| 5 | SMTP username | Your full sending address, e.g. `your.name@gmail.com`. |
| 6 | SMTP password / app password | Your 16-character Google App Password, **without grouping spaces**. Hidden input. Use the app password rather than your normal Google password. |
| 7 | Sender address | Press Enter to use your Gmail username. Another address must be an authorized sender. |
| 8 | Recipient address | Your university Outlook address, or another address where you read alerts. |
| 9 | Workspace ID | The exact workspace UUID. Leave blank only for notification-only mode. |

For another sending provider, obtain its SMTP hostname, TLS mode, port and credentials from that provider or your administrator. With an authorized relay, leave username blank only if the relay explicitly permits sending without authentication; the password step is then skipped.

The wizard saves `config.json`, `surf-token.txt` and, for authenticated SMTP, `smtp-password.txt`. Keep these files private and out of Git. Environment variables `RESEARCH_CLOUD_TOKEN` and `SMTP_PASSWORD`, if already set, override the corresponding files when running commands.

Do not rerun `init` to update an existing installation: it refuses to overwrite configuration/credential files. Edit `config.json` or the relevant credential file locally. Do not share tokens/passwords in screenshots or troubleshooting output.

## Verify connections and the target

Run these commands separately:

```bash
python3 surf_watch.py check
python3 surf_watch.py workspace-check
python3 surf_watch.py test-email
```

If you chose notification-only mode, omit `workspace-check`.

| Result | Meaning and next step |
| --- | --- |
| `a10 = 0; unavailable` | The Catalog query worked. There is no reported A10 capacity now; start the watcher after finishing verification. `check` queries only once. |
| `a10` is available | Capacity is reported now, but another user may acquire it first. The VM's flavour determines the actual allocation; an availability signal cannot guarantee a two-GPU VM will resume. |
| Workspace `paused`, with `resume` allowed | Check its ID, hostname and flavour match the VM you intend to control. This is the starting state for automatic acquisition. |
| Workspace `resuming`/`pausing`, with no allowed actions | This can be normal while SURF processes an action. Wait, then rerun `workspace-check`. |
| Workspace already `running` | Automatic acquisition starts from paused. A new watcher will not adopt that VM. If an existing watcher resumed it, confirm that run before its deadline. |
| SMTP accepted the test email | Look for **SURF GPU watcher test** in the recipient's inbox and spam folder. Acceptance by the sending server does not guarantee delivery; verify receipt before starting. |

## Start watching and confirm use

Once checks succeed and you have received the test email:

```bash
python3 surf_watch.py auto
```

This command makes real Resume/Pause requests and resuming consumes the workspace's budget. The startup log describes the polling interval and confirmation window. Keep the computer awake and the process running; closing the terminal, stopping the process, sleeping or losing connectivity can prevent timely Pause. On macOS, if you launch the tool in a terminal rather than through a supervisor, you can use this instead of the command above to prevent idle system sleep while it runs:

```bash
caffeinate -i python3 surf_watch.py auto
```

The default confirmation link uses `http://127.0.0.1:8765`: open it in a browser **on the computer running the watcher**. A phone or another computer cannot use that localhost link directly. For remote hosting, configure an SSH tunnel or HTTPS domain as described in [README.md](README.md#confirmation-links) before starting automatic mode.

When the VM reaches running, open the email link and click **Confirm use and keep VM running** within the window. Opening the email, opening the link without clicking, or logging in to SSH/Jupyter does not confirm use. As a fallback, before the deadline, run on the watcher computer:

```bash
python3 surf_watch.py auto-status
python3 surf_watch.py confirm
```

If you do not confirm, the watcher requests Pause after the deadline, waits for paused, sends a release notification, and finishes. Actual release can take longer than ten minutes because SURF actions are asynchronous. After confirmation, the watcher also finishes and leaves the VM running. Coordinate use of a shared VM and confirm before starting work.

For notification-only mode, run `python3 surf_watch.py watch`; it emails once on availability and never resumes or pauses a VM.

## Troubleshooting

Start with:

```bash
python3 surf_watch.py doctor
```

`doctor` checks HTTPS/SMTP TLS without credentials, email or VM actions. Use the same Python interpreter as the watcher; repairing another Python installation's certificates will not repair this one.

| Problem | Guidance |
| --- | --- |
| All three connection checks fail immediately | Read the classified cause and run `doctor` to check certificates, DNS and connectivity separately. |
| `CERTIFICATE_VERIFY_FAILED` on python.org Python for Mac | Run `/Applications/Python X.Y/Install Certificates.command` for the version printed by `doctor`. For example, Python 3.13: `"/Applications/Python 3.13/Install Certificates.command"`. Keep TLS verification enabled. Rerun the three verification commands; do not rerun `init`. |
| SURF HTTP 401 | Check whether your personal token is valid/expired and whether an environment variable is overriding the token file. Never paste the token into a help request. |
| Catalog HTTP 403, but workspace queries work | Update the tool with `git pull --ff-only`. Current releases use the `/v1/application-market/health/status/` endpoint and normalize the old unversioned URL automatically. If the current endpoint still returns 403, check endpoint permissions with SURF; it does not by itself prove the token is invalid. |
| Workspace HTTP 403, or no `resume` action while paused | Check the selected workspace and your Resume/Pause rights with its owner/workspace admin. A successful Catalog query does not establish workspace action permission. |
| SMTP authentication rejected | For Gmail, verify the full sender address and its App Password, without grouping spaces. Check account policy if App Passwords are unavailable. An Outlook recipient still uses your Gmail sender's credentials. |
| DNS failure, timeout or connection refused | Check the host, TLS/port pair, network, VPN and firewall. SMTP connects directly and is not tunneled through HTTP/HTTPS proxy environment variables. |
| Confirmation link fails | Verify the watcher process is active and you are opening the link on its host or through a configured tunnel. Before expiry, use the local `confirm` command if needed. |
| Saved run is `confirmed` or `paused` | A completed run will not rearm on restart. After it finishes, set a new `auto.state_file` name in `config.json`, e.g. `auto-state-next.json`, to start another cycle. Do not delete an active run's state/lock files. |

If a run ends as `needs_attention` or `intervened`, inspect the workspace and its recent actions in SURF. Those states do not confirm that the GPU was released.
