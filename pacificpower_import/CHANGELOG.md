# Changelog

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
