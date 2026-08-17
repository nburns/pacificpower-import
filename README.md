# Pacific Power → Home Assistant

A Home Assistant add-on that pulls your Pacific Power (PacifiCorp) hourly
electricity usage into HA's Energy dashboard.

Pacific Power doesn't offer Green Button **Connect** (OAuth), which is what
a native HA integration would use. This add-on drives the customer portal
with a headless browser, downloads the Green Button ESPI XML, parses hourly
`IntervalReading` entries, and inserts them as long-term statistics.

## Data staleness

Pacific Power publishes Green Button interval data with roughly a
**24-hour lag** — yesterday's hourly readings appear on the portal
mid-day today. The daily cron fires at 06:00 local and pulls a
3-day rolling window, so consumption data is typically **1-2 days
behind real time**. Billed cost updates only when a new bill posts
(**~monthly**), so the cost stat trails consumption by up to 30 days.

## Features

- Automatic hourly-granularity import into HA long-term statistics.
- 2-year historical backfill on first run.
- Daily incremental (default 06:00 local) with a 3-day rolling window to
  catch late-arriving intervals.
- Runs entirely inside your HA host — no cloud service in the middle.
- Locked-down container: non-root, AppArmor profile, no host network / PID
  / IPC, minimal HA API access.

## Install

One-click (uses [my.home-assistant.io](https://my.home-assistant.io) —
opens the "Add repository" dialog in your HA):

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnburns%2Fpacificpower-import)

Or manually:

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/nburns/pacificpower-import` → **Add**

Then:

3. Refresh the add-on store; "Pacific Power Import" appears in the list.
4. Click **Install**.
5. Open the **Configuration** tab; enter your Pacific Power `username` and
   `password`. Optional: `meter_id` if you have multiple meters.
6. **Save**, then **Start**.
7. Watch the **Log** tab — the initial 2-year backfill takes a few minutes.

## Add to the Energy dashboard

1. **Settings → Dashboards → Energy → Electricity grid → Add consumption**
2. Select "Pacific Power electric consumption" (or your custom
   `statistic_name`).
3. Save — historical bars appear immediately.

See [DOCS.md](pacificpower_import/DOCS.md) for full options reference and troubleshooting.

## Requirements

- Home Assistant OS or Supervised (add-ons don't work on Container/Core).
- A Pacific Power online account with a smart meter installed.
- MFA disabled on the Pacific Power account (the login flow is scripted).

## Security

- Add-on security rating: **6/8** (AppArmor + no host access + no dangerous
  caps). Full breakdown in [AGENTS.md](AGENTS.md).
- Credentials live only in supervisor-encrypted options + process memory.
- Everything after startup runs as unprivileged `pwuser`.

## How it works

- **Auth**: Azure B2C login is scripted (username + password inside an
  iframe). Session is persisted between runs.
- **Download**: The portal's own XHR body is client-side encrypted, so we
  can't replay it with plain HTTP — we drive the UI instead.
- **Parse**: ESPI is a NAESB Atom feed; parser is stdlib-only.
- **Import**: HA WebSocket API's `recorder/import_statistics` accepts
  external statistics idempotent by `(statistic_id, start)`.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. This is a workaround; if PacifiCorp ever ships
Green Button **Connect** (OAuth), the right long-term answer is to drop
this add-on in favor of a native HA integration.
