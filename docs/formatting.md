# How numbers, times and sizes are shown

Every value the web app shows goes through one module, `services/web/src/lib/format.ts`. Pages and components don't format on their own. Lint enforces this: `toFixed`, `toPrecision`, `toLocaleString`, `toLocaleDateString`, `toLocaleTimeString` and `Intl` are not allowed in `src/pages/` or `src/components/`. Building a query's time window with `toISOString` is fine, because that is data, not display.

A value that is missing or isn't a number shows as **–** (an en dash) everywhere.

## Numbers

| Function | For | Examples |
|---|---|---|
| `fmtNum(n)` | Counts and values in tables, cards and charts. Short form for large numbers, 3 significant digits for small ones. | `950`, `12.5`, `0.372`, `12.3K`, `1.2M`, `3.4B` |
| `fmtInt(n)` | A count that must be exact, such as a bill or a usage meter | `1,234,567` |
| `fmtFixed(v, digits)` | A fixed number of decimals, when the user chose them (Query Builder, dashboard panels) or the measure needs them (CLS, burn rate). Digits are clamped to 0–20. | `fmtFixed(0.1234, 3)` → `0.123` |

## Percent

| Function | For | Examples |
|---|---|---|
| `fmtPercent(v)` | `v` already in 0–100: error rates, utilisation, shares | `0%`, `0.42%`, `3.5%`, `45.3%`, `99.95%`, `100%` |
| `fmtRatio(v)` | `v` as a fraction in 0–1, such as uptime from success/total | `fmtRatio(0.9996)` → `99.96%` |

**Decimals:**
- 2 decimals below 1%, so a small error rate isn't shown as `0%`.
- 2 decimals from 90% up to 100%, so 99.96% is never rounded to `100%`, and 98.96% never to `99%`, which would read as meeting a 99% target.
- 1 decimal otherwise.
- Trailing zeros are dropped: `12%`, not `12.0%`.
- A caller can ask for more decimals: SLO attainment uses 3, as in `99.987%`.

## Durations

| Function | Input | Examples |
|---|---|---|
| `fmtMs(ns)` | Nanoseconds, as spans store them | `0.42 ms`, `1.5 ms`, `15 ms`, `1.23 s` |
| `fmtDuration(ms)` | Milliseconds: page loads, web vitals, query time, check intervals, time spent | `850 ms`, `1.23 s`, `4 min`, `2.5 h`, `3 days` |
| `fmtHours(h)` | A quantity of hours, such as GPU hours | `45 min`, `12.5 h`, `1,250 h` |

`fmtDuration` uses:
- **ms** below 1 s;
- **s** with 2 decimals below 1 minute;
- whole **min** below 90 minutes;
- **h** with up to 1 decimal below 36 hours;
- whole **days** after that.

## Times and dates

All times are in the viewer's time zone and use a 24-hour clock.

| Function | For | Examples |
|---|---|---|
| `fmtTime(iso, range)` | Chart axis labels. The precision follows the range: seconds up to 15 minutes, the date over a day. | `14:05:07`, `14:05`, `17 Oct, 14:05` |
| `fmtClock(t)` | A moment within the visible range | `14:05:07` |
| `fmtDateTime(t)` | A moment that may be on another day: tooltips, "last seen", events. Day and month follow the viewer's locale. | `17 Oct, 14:05` |
| `fmtDay(t)` | A day | `17 Oct` |
| `fmtTs(iso)` | Full precision, for logs and runs | `2026-10-17 14:05:07.123` |
| `fmtAgo(t)` | How long ago | `just now` (under 10 s), `45 s ago`, `12 min ago`, `5 h ago`, `3 days ago` |

## Sizes

Two conventions, each in its own place:

| Function | For | Examples |
|---|---|---|
| `fmtBytes(n)` | Memory, in binary units (Kubernetes and GPU memory). No trailing zeros. | `512 MiB`, `1.5 GiB`, `1 GiB` |
| `fmtMiB(n)` | The same, when the input is in MiB (GPU memory) | `fmtMiB(16384)` → `16 GiB` |
| `fmtUnit(n, "bytes")` | Data volumes and billing, in decimal units, matching prices per GB | `300 MB`, `1.2 GB` |

## CPU

| Function | Examples |
|---|---|
| `fmtCores(n)` | `250m`, `1.50 cores`, `12.0 cores` |

## Money

| Function | For | Examples |
|---|---|---|
| `fmtUsd(v)` | Amounts | `$0.42`, `$12`, `$1,234` |
| `fmtUnitPrice(v)` | Prices per unit: per million records, per GPU hour. 2 decimals, or 3 when the third isn't zero. | `$0.18`, `$0.075`, `$3.43` |

## Units on chart panels

`fmtUnit(v, unit, decimals?)` formats a panel's value in its declared unit (`ms`, `s`, `ns`, `bytes`, `%`, `percentunit`, `/s`, `W`, `°C`). It uses the duration and size rules above, and an optional fixed number of decimals.
