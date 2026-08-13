# Pacific Power Import

Pull your Pacific Power (PacifiCorp) hourly electricity usage into Home
Assistant's Energy dashboard.

- Downloads Green Button ESPI XML from the Pacific Power portal on a
  schedule (2-year backfill + daily incremental).
- Parses hourly `IntervalReading` entries.
- Inserts them into HA's long-term statistics via
  `recorder/import_statistics`.

See **DOCS** tab for setup and options.

Source: https://github.com/nburns/pacificpower-import
