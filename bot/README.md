# bot/ — forgejo-runner-notify

Pi-side Telegram notifier for the `arm64-runner` self-hosted Forgejo Actions runner.
Fires a `🚀 Pi runner: new CI job` card on every task pickup.

## What it is

- **`forgejo-runner-notify`** — stdlib-only Python (~270 lines)
- **`forgejo-runner-notify.service`** — systemd unit (hardened, ~1.3/10 score)
- **`.env.example`** — non-secret env template (chat id)
- **`Makefile`** — `make install` / `make restart` / `make logs`

## How it works

1. Tails `journalctl -u forgejo-runner.service -f -o json` (cursor-persistent
   across restarts so no duplicate notifs).
2. Regex-matches the runner's own log line `task <N> repo is <owner>/<repo>`.
3. Enriches via Codeberg API: `GET /repos/{r}/actions/runs?limit=10` →
   picks first `in_progress` / `queued` / `running` (fallback: most-recent),
   uses the run's own `title` field for commit message.
4. Renders a rich card and sends via `https://api.telegram.org/.../sendMessage`.

It is **not** a webhook listener. The "per-event payload extractor" framing
from the original `ci-workflows#14` body was wrong — the bot never sees the
Forgejo webhook payload.

## Failure handling (added 2026-05-21 for #14)

- **`cb_get`** retries 3× with exponential backoff (1s, 2s, 4s) so a single
  API blip doesn't blank a notif.
- **`fetch_task_metadata`** poll-retries up to ~6s when the run record isn't
  yet indexed at the instant the runner journal logs the task pickup.
- **`fmt_rich`** uses em-dash (`—`) instead of `?` for missing slots, and
  auto-degrades to a clean `🚧 metadata pending` one-line card when ≥4 slots
  would be empty (instead of a `?`-laden half-card).

## Install (on the Pi host)

```bash
make install   # script + unit + restart + status
```

The script lands at `/usr/local/bin/forgejo-runner-notify` (mode 755,
`nobody:nogroup`); the unit at `/etc/systemd/system/forgejo-runner-notify.service`.

A backup of the previous script is saved under `$HOME/forgejo-runner-notify.bak.<timestamp>`
before any replace.

## Credentials (not committed — provisioned out-of-band on the Pi)

The unit uses `LoadCredentialEncrypted` for the Telegram bot token and the
Codeberg API token (both encrypted via `systemd-creds encrypt` and stored
under `/etc/credstore.encrypted/forgejo-notify-{telegram,api}-token`). The chat id
is non-secret enough to live in `/etc/forgejo-runner-notify.env`; see
`.env.example` for the shape.

## Origin

Bot was originally on-disk only (no source control); it was later captured into
this `bot/` subtree so it lives alongside the templates it supports.
