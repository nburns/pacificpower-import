# Changelog

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
