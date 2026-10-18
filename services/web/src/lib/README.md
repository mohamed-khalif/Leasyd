# `lib/`: plain functions shared by pages

These modules contain no React and no requests, so they're easy to test and safe to import from anywhere.

| Module | What it holds |
|---|---|
| `format.ts` | Every number, percent, duration, date, size and money formatter. The spec is `docs/formatting.md`; its examples are the tests in `format.test.ts`. Lint keeps formatting out of `pages/` and `components/`. |
| `sql.ts` | `sqlString`: a value as a SQL string literal. Use it for every value put into SQL text. |
| `telemetry.ts` | What telemetry values mean: log severity bands (`SEVERITY_RANGES`, `severityRange`) and `isError` for span status. |
| `timeZones.ts` | Time-zone arithmetic: `LOCAL_TZ`, the zone list, and a moment's day and minute in a zone. This is for calculations, not display; display belongs in `format.ts`. |

When a helper is used by two or more pages and has no React in it, it belongs here.
