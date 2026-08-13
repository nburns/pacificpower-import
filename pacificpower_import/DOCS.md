# Pacific Power Import — Setup

## Configure

Open the add-on's **Configuration** tab and set:

| Option | Description |
| --- | --- |
| `username` | Your Pacific Power online account User ID. |
| `password` | Your Pacific Power online account password. |
| `meter_id` | (optional) If your account has multiple meters, put the meter number here (e.g. `12345678`). Leave blank for single-meter accounts. |
| `statistic_id` | Internal identifier for the HA statistic. Must contain a colon. Default `pacificpower:electric_consumption` is fine. |
| `statistic_name` | Human-readable name shown in HA. |
| `daily_schedule` | Cron expression for the daily incremental. Default `0 6 * * *` (06:00 local). |
| `run_backfill_on_start` | If `true`, run the 2-year backfill on first start when no state file exists yet. |

Click **Save**, then **Start** the add-on.

## First-run behavior

On first start the add-on:

1. Reads options from the supervisor.
2. Launches a headless Chromium.
3. Signs in to `csapps.pacificpower.net` using your credentials.
4. Downloads the last 2 years of Green Button XML.
5. Parses hourly readings and imports them as long-term statistics.
6. Starts the internal cron; the daily incremental pulls the last 3 days.

Watch the **Log** tab. First-run backfill takes a couple of minutes.

## Add to the Energy dashboard

Once the backfill finishes:

1. Go to **Settings → Dashboards → Energy**.
2. Under **Electricity grid**, click **Add consumption**.
3. Select the statistic named after your `statistic_name` (default
   "Pacific Power electric consumption").
4. Save. Historical bars appear immediately.

## Troubleshooting

- **"Not authenticated — running login flow" every run**: session cookies
  aren't sticking. Check `/data/browser/` exists and is writable; a HA
  backup restore may have wiped it.
- **`Timeout waiting for #signInName`**: Pacific Power changed their
  Azure B2C flow. File an issue with the log.
- **`date input not found`**: portal DOM changed. Same as above.
- **Data stops appearing after a day**: check the log around the
  scheduled time. Pacific Power occasionally locks accounts after too
  many logins — sign in via the website to unlock, then restart the
  add-on.

## Storage

Everything the add-on persists lives under `/data/`:

- `state.json` — cumulative kWh + last-run timestamps.
- `browser/` — persistent Chromium context (cookies, cache).
- `downloads/` — every downloaded Green Button XML. Pruned to the most
  recent 30 files per run.

Deleting `/data/` triggers a full re-login and a full backfill on the
next start.
