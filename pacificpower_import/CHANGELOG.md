# Changelog

## 0.3.5

- `diagnostics_enabled` toggle is now live-editable. Flipping it in the HA
  Configuration tab and saving takes effect immediately - no add-on restart
  needed. Both the scraper's dump gate and the ingress viewer re-read
  `/data/options.json` on each check.
- Fix misleading storage-dir log line. Previous versions used
  `Path.iterdir()`, which skipped Chromium's `Default/` subdirectory and made
  every run look like the persistent context was empty (`0.0 KB total`).
  Now uses `rglob("*")` so the reported file count and size reflect what's
  actually on disk.

## 0.3.4

- Add Home Assistant ingress web page (port 8099) exposing a live log tail and
  reverse-chronological debug captures. Access via the "Pacific Power" sidebar
  entry.
- New `diagnostics_enabled` config option (default `false`). When off, the
  scraper does not write debug dumps and the ingress page shows a placeholder
  telling you how to enable it. Flip it on when you want to investigate a
  failure, flip it off again to stop accruing disk usage.
- Capture screenshot + HTML dump automatically on `mat-select` timeout
  (`download_greenbutton`) and billing table timeout (`fetch_bill_history`)
  when diagnostics is enabled. Captures are pruned to the 20 most recent pairs
  under `/data/debug/`.
- **Credential scrub before every capture.** A JS scrub runs against the live
  page immediately before both the screenshot and the HTML dump: every input
  value is blanked (property + attribute) and every `<script>` textContent is
  stripped. If the scrub itself fails, the whole capture is refused to fail
  safe. Login-page captures are still allowed - they just no longer contain
  the username or password.
- New session-state logging: storage dir file count + size at scraper startup;
  total and `pacificpower.net`-scoped cookie count after each login. Helps
  diagnose the "re-login every run" symptom.
- `run.sh` tees the supercronic loop to `/data/logs/main.log` (capped at
  ~10 MB, truncated to 5 MB on startup). Web server stdout also goes there.

## 0.3.2

- Add add-on icon (128×128 red triangle).

## 0.3.1

- Add `_goto_with_retry` helper to `scraper.py`. All four `page.goto` calls
  (dashboard, login, energy-usage, billing-history) now retry up to 3 times on
  transient Chromium network errors (`ERR_NETWORK_CHANGED`, `ERR_INTERNET_DISCONNECTED`,
  `ERR_TIMED_OUT`, `ERR_CONNECTION_RESET`, `ERR_ABORTED`, `ERR_NAME_NOT_RESOLVED`)
  with 5s then 15s backoff. Non-matching errors and final failures re-raise.
  Prevents container-boot race between Chromium launch and the network stack
  coming up from killing an entire scheduled run.
- Convert silent empty-result returns to `raise RuntimeError(...)` throughout
  `__main__.py`. Affected paths: `run()` when no downloads succeed, `run()` when
  no readings parse, `run_hourly_switchover()` when no XML files or no readings
  are found, and `run_hourly_daily()` when the XML parse fails or yields zero
  readings. Process now exits nonzero on these failures so cron retries within
  the hour instead of waiting ~24h.

## 0.3.0

- Add opt-in hourly-granularity mode (`hourly_mode: true`). When enabled,
  a trickle job downloads one "One Day" XML per trigger (24 hourly readings)
  and accumulates them on disk without touching HA statistics. Once the
  requested window is covered, a one-time switchover clears existing
  statistics and re-imports everything at hourly grain. After switchover,
  the daily cron fetches yesterday's one-day XML (24 rows per run).
- New options: `hourly_mode`, `hourly_backfill_days_per_hour` (default 4,
  one download every 15 minutes), `hourly_backfill_window_days` (default
  730). At the default rate the trickle covers two years in ~7.5 days.
- Flipping `hourly_mode` in either direction automatically clears statistics
  and triggers a fresh backfill at the new granularity on next start. No
  manual state reset needed.
- `state.json` gains three new fields: `hourly_backfill_cursor`,
  `hourly_backfill_complete`, and `last_mode`. Old state files load cleanly
  (missing fields default to off/false/null).

## 0.2.3

- `run.sh` now prefixes its own log lines with a matching timestamp
  (previously bash echo lines had no timestamp, mixed awkwardly with
  the timestamped Python logs).
- On missing/incomplete config, log the message once and then `sleep
  infinity` instead of exiting. Supervisor auto-restarts the add-on
  when you save config, so exit-looping just spammed the log with
  repeated errors.

## 0.2.2

- Bill-history scrape now clicks the portal's "SHOW ALL" link after
  expanding the date range. Previously we only got page 1 (10 rows)
  of a paginated table, which for accounts with a couple of years of
  history meant just 3 bills — leaving most of the cost stat empty.
  Now we pull the full history (~24 bills for 2 years).
- `BACKFILL_VERSION` bumped to 5 to auto-trigger a re-backfill and
  populate the full cost history.

## 0.2.1

- Emit a heartbeat to `input_datetime.pacificpower_last_import` after
  every successful run. Pair with a template binary_sensor + HA alert
  to detect stale imports. No-op if the helper doesn't exist.

## 0.2.0

- Also import **billed cost** as `pacificpower:electric_cost` (USD).
  Scrapes the `Billing & payment history` table (rows tagged
  "Regular Bill" etc.) and spreads each bill's amount across the days
  since the previous bill, so the HA Energy dashboard shows smooth
  daily cost bars instead of monthly spikes.
- Best-effort date-range expansion (From/To → today − 2y) to fetch
  more history than the default view.
- New options: `cost_statistic_id`, `cost_statistic_name`.
- `BACKFILL_VERSION` bumped to 4 → auto-triggers re-backfill on upgrade
  so the new cost stat is populated from historical bills.

## 0.1.4

- Fix backfill: prior versions saved every download to the same filename
  because the portal always names the file with today's date regardless
  of the requested range. All 25 monthly iterations therefore overwrote
  the same file on disk, leaving only the last (current month) at
  parse time — the backfill silently returned just ~30 days.
- Each download now saves as `<ending_date>_<period>_<original>.xml`.
- Bumps `BACKFILL_VERSION` to 3 so the fix triggers a fresh backfill
  automatically on upgrade (no manual state reset needed).

## 0.1.3

- AppArmor profile now actually loads on HA OS. Previous versions
  included `<abstractions/openssl>` and `<abstractions/python>` which
  don't exist on the host's AppArmor setup — parser failed silently
  and the supervisor fell back to the default profile (rating stuck
  at 5/8). Profile trimmed to `base` + `nameservice` only, matching
  the official add-on pattern. Rating should now be 6/8.

## 0.1.2

- Backfill now iterates 25 "One Month" downloads instead of one "Two
  Year" — Pacific Power returns *monthly*-granularity intervals for
  "Two Year" but *daily*-granularity for "One Month", so this produces
  much more useful data in the Energy dashboard (day-by-day bars
  instead of a giant monthly bucket).
- Daily incremental switches from "One Day" (hourly) to "One Month"
  (daily) so backfill and incremental data live at the same
  granularity (no double-counting between the two).
- Adds a `backfill_version` in `/data/state.json`. On upgrade, if the
  stored version is older than the current one, the add-on
  automatically clears prior statistics and runs a fresh backfill —
  no manual state-reset needed.
- Downloads accumulate briefly (25 files during backfill); pruning
  cap raised to 60.

## 0.1.1

- Date input selector is now resilient across period modes ("Two Year"
  backfill was failing where "One Day" worked because Angular re-renders
  the input with different attributes). Falls back to the portal's
  default ending date if no selector matches.
- Dumps `/data/date-input-debug.html` on selector miss for easier
  troubleshooting.

## 0.1.0

Initial release.

- Log in to `csapps.pacificpower.net` (Azure B2C).
- Download Green Button ESPI XML for a chosen period (Two Year / One
  Year / One Month / One Week / One Day).
- Parse hourly `IntervalReading` entries.
- Import into Home Assistant long-term statistics via
  `recorder/import_statistics`.
- One-time backfill of up to 2 years, then daily incremental via
  supercronic.
