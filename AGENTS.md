# AGENTS.md — pacificpower-import

Bridge for pulling Pacific Power (PacifiCorp) Green Button ESPI XML into
Home Assistant long-term statistics. Packaged as a Home Assistant local
add-on. Temporary workaround until PacifiCorp ships Green Button Connect.

## Layout

```
src/pacificpower_import/
  espi.py       ESPI (NAESB Green Button) XML parser
  scraper.py    Playwright: login + trigger download on the portal
  ha_client.py  HA WebSocket client (recorder/import_statistics)
  state.py     /data/state.json bookkeeping (cumulative Wh, last run)
  __main__.py   CLI orchestrator: scraper → parser → HA import
config.yaml     HA add-on manifest (options schema, security flags)
Dockerfile      Add-on container build
run.sh          Entrypoint (options → backfill → supercronic)
entrypoint.sh   Root-level shim that chowns /data + drops to pwuser
apparmor.txt    AppArmor profile
build.yaml      Add-on build metadata
tests/          pytest; fixtures/ has a real captured ESPI XML
```

## Setup

```
uv sync
uv run playwright install chromium   # only for local dev; addon uses system chromium
```

## Common commands

Local dev reads credentials from `.env` (copy from `.env.example`).
`uv run --env-file .env` loads them.

```
uv run pytest                                    # tests (parser only right now)
uv run python -m pacificpower_import --help      # CLI

# Local scraper smoke test (visible browser, one download, no HA):
uv run --env-file .env python -m pacificpower_import.scraper \
  --headed --period "One Day"

# Full local run against a real HA instance:
uv run --env-file .env python -m pacificpower_import \
  --mode backfill --headed
```

## Architecture notes

**Why Playwright (not httpx).** The visible `<form>` on
`csapps.pacificpower.net/secure/my-account/energy-usage` posts to
`/secure/energy-usage/getGreenButtonData`, but Angular *actually* fires
an XHR to `/api/energy-usage/getGreenButtonData` with an **encrypted
base64 request body** (~256 bytes ciphertext). Replaying it needs the
client-side crypto — not worth reversing. Instead we drive the UI.

**Login is Azure B2C inside an iframe.** The login page at
`/idm/guest-pay-login` (or `/idm/login`) mounts an
`<iframe id="loginframe">` pointing at
`login.csapps.pacificpower.net/.../oauth2/v2.0/authorize?...&p=B2C_1A_PAC_SIGNIN`.
The form fields inside are `#signInName`, `#password`, submit `#next`.
Use `page.frame_locator("#loginframe")`. After submit, the outer page
navigates through the OAuth callback to `/secure/my-account/...`.

**Period → granularity.** The "For period of" dropdown values are
`Two Year / One Year / One Month / One Week / One Day`. Empirically
confirmed: `One Month` → daily intervals (duration=86400);
`One Day` → hourly intervals (duration=3600, 24 readings per file).
The parser handles any duration uniformly — check
`IntervalReading.duration` if you care.

**Backfill horizon.** 2 years max (widest period option). Single
download per backfill covers the whole span.

**Ending date max = yesterday.** The date picker's `max` attribute is
yesterday — today's intervals aren't published yet. Never pass
`date.today()` as `ending_on`.

**Key selectors on the energy-usage page** (from a live probe):
- Meter dropdown: `mat-select` (first one on the page).
- Period dropdown: `mat-select` (second one), options open as
  `<mat-option>` elements with visible text matching the choices above.
- Ending date input: `input[matinput][aria-haspopup="true"]`
  (Angular Material datepicker input). The `mat-datepicker-input` CSS
  class is NOT applied — don't rely on it.
- Download trigger: `<a>` inside a `mat-list-item` with visible text
  `DOWNLOAD GREEN BUTTON DATA`. No href → `get_by_role("link", ...)`
  fails. Use `get_by_text("DOWNLOAD GREEN BUTTON DATA", exact=True)`.

**HA long-term statistics.** External statistics require a `statistic_id`
with a colon (e.g. `pacificpower:electric_consumption`). `has_sum=true`,
`state` = kWh for that interval, `sum` = cumulative kWh from the epoch
we picked. Re-imports for the same `(statistic_id, start)` overwrite
in place — the daily job pulls a 3-day rolling window and is safe to
re-run.

**Cumulative sum.** `state.py` tracks `cumulative_wh` and
`latest_interval_start` between runs. Previously-imported intervals are
re-emitted with their original sums to preserve idempotency; new
intervals continue the running total.

**Auth persistence.** Chromium persistent context lives at
`<data_dir>/browser/`. Sessions carry across runs — full re-login only
when cookies expire.

## Add-on packaging

- Base image: `mcr.microsoft.com/playwright/python:v1.62.0-noble`
  (ships Chromium + all system deps; sidesteps Alpine/musl issues).
- `run.sh` reads options from `/data/options.json` with `jq`, runs a
  one-shot backfill on first boot, then `exec`s `supercronic` (so
  SIGTERM propagates cleanly).
- Inside the container HA is reached via `ws://supervisor/core/websocket`
  authenticated with `$SUPERVISOR_TOKEN` — no long-lived token needed.
  `HAClient.for_supervisor()` handles this.
- Add-on files that HA reads from the add-on dir: `config.yaml`,
  `Dockerfile`, `build.yaml`, `README.md`, `DOCS.md`, `CHANGELOG.md`,
  `apparmor.txt`, `icon.png` (128×128), `logo.png` (250×100). Icons
  aren't checked in yet — supervisor falls back to defaults if missing.
- To publish as an installable custom repository, the *parent*
  directory needs a `repository.yaml` (already at
  `../repository.yaml`).

## Security posture

The add-on is deliberately locked down. If you loosen anything, note it
here so it's easy to review later.

- `config.yaml`: `hassio_api`, `auth_api`, `docker_api`, `host_network`,
  `host_pid`, `host_ipc`, `host_dbus`, `usb`, `uart`, `kernel_modules`,
  `full_access` are all `false`/empty. `privileged: []`. Only
  `homeassistant_api: true` is granted — needed for the WS import.
- `ingress: false`, no `webui`, no `ports` — the add-on doesn't expose
  any HTTP surface.
- `apparmor: true` loads `apparmor.txt`. Profile blocks mount
  operations, sensitive `/proc` and `/sys` paths, writes to
  `/etc /usr /opt /home /lib /bin /sbin`, and pins execution to a
  known allowlist. Verified by `aa-status` on the HA host after install.
- Process runs as **non-root** (`pwuser`, uid 1001, from the Playwright
  image). `entrypoint.sh` runs as root only long enough to
  `chown /data` to `pwuser`, then `exec gosu pwuser /run.sh`.
  Everything after — Chromium, Python, supercronic — is unprivileged.
- **Chromium sandbox is off** (`chromium_sandbox=False`). That's
  intentional: Chromium's own sandbox needs `CAP_SYS_ADMIN` / user-ns
  which we don't grant. Isolation comes from container + AppArmor +
  non-root instead. Do not re-enable without also loosening the caps.
- Credentials only ever live in `/data/options.json` (supervisor-managed,
  encrypted at rest) and in-memory. They're never logged. Don't add
  logging of `PP_PASSWORD` or `SUPERVISOR_TOKEN`.

## Local Docker test quirk

On Docker Desktop macOS, files created inside the container on a
bind-mounted `/data` appear as `root:root` in the host's `ls`, even
when the in-container process is `pwuser`. It's an osxfs remap, not a
bug in `entrypoint.sh`. Verify with `gosu pwuser id` inside the
container — you should see `uid=1001(pwuser)`. On a real Linux HA host
ownership will display correctly.

## Local Docker test

Iterate on the container without installing into HA:

```
# Build (context = repo root; add-on manifest lives here).
docker build -t pp-import:dev .

# Simulate supervisor's /data/options.json + SUPERVISOR_TOKEN and run once.
mkdir -p /tmp/pp-addon-data
cat > /tmp/pp-addon-data/options.json <<EOF
{
  "username": "$PP_USERNAME",
  "password": "$PP_PASSWORD",
  "meter_id": "$PP_METER_ID",
  "statistic_id": "pacificpower:electric_consumption",
  "statistic_name": "Pacific Power electric consumption",
  "daily_schedule": "0 6 * * *",
  "run_backfill_on_start": true
}
EOF
docker run --rm -it \
  -e SUPERVISOR_TOKEN=fake \
  -e HA_URL=ws://host.docker.internal:8123/api/websocket \
  -e HA_TOKEN=$HA_TOKEN \
  -v /tmp/pp-addon-data:/data \
  pp-import:dev
```

Watch the container log through backfill + first cron tick. `Ctrl+C`
kills it; `/tmp/pp-addon-data/` holds the state between runs.

## Testing

- `tests/test_espi.py` runs against a real Pacific Power ESPI sample at
  `tests/fixtures/greenbutton_sample.xml`. When adding parser features,
  extend this test rather than mocking the parser.
- No tests for `scraper.py` (browser-driven, external service) or
  `ha_client.py` (needs a live HA instance). Smoke-test both manually.

## Known gaps / next steps

- Verified end-to-end from a laptop: login → date-range set → download →
  parse. Not yet run against a live HA instance (waiting on
  `HA_TOKEN`).
- Not yet deployed to a real HA instance for supervisor-side testing.
  Local Docker build + Chromium smoke test verified.
- No NW Natural support. NW Natural does not appear to expose Green
  Button; if we add it later it'll be a separate scraper module using
  the same espi/ha_client/state plumbing.

## Non-goals

- Cost/rate calculation — HA Energy dashboard handles that from a
  tariff config.
- Real-time updates — Green Button data lags 1 day; live power monitoring
  should come from a panel-mounted CT clamp (Shelly EM / Emporia Vue),
  not this add-on.
