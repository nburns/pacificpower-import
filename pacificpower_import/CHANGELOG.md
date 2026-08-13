# Changelog

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
