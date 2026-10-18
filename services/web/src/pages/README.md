# `pages/`: one file per page

`App.tsx` maps each link (`#/<route>`) to a page. Each page's code is a separate chunk. The page a link opens is downloaded at start-up; the others when first visited.

## Pages

**Telemetry**

| Route | File | What it is |
|---|---|---|
| `#/` | `Insights.tsx` | Home: services, failing checks, volumes, folding sections per signal |
| `#/logs` | `Logs.tsx` | Logs explorer: views, severity chart, table, groups, patterns |
| `#/traces`, `#/traces/<id>` | `Traces.tsx` | Traces explorer, duration heat map, waterfall of one trace |
| `#/metrics` | `Metrics.tsx` | Metrics list and detail |

**Applications and infrastructure**

| Route | File | What it is |
|---|---|---|
| `#/services` | `ServiceMap.tsx` | Service map from spans |
| `#/web` | `Web.tsx` | Real-user monitoring |
| `#/lambda` | `Lambda.tsx` | AWS Lambda functions |
| `#/kubernetes` | `Kubernetes.tsx` | Clusters, nodes, workloads, pods, events (queries in `infra/queries.ts`) |
| `#/gpus` | `Gpus.tsx` | NVIDIA GPU fleet, health and cost |

**Analyse**

| Route | File | What it is |
|---|---|---|
| `#/dashboards` | `Dashboards.tsx` | Built-in and saved dashboards, panel editor |
| `#/query` | `QueryBuilder.tsx` | One chart from PromQL or built queries |
| `#/sql` | `Sql.tsx` | Read-only SQL |

**Alerting, uptime and AI**

| Route | File | What it is |
|---|---|---|
| `#/alerts` | `Alerts.tsx` | Alert rules, channels, history |
| `#/synthetics` | `Synthetics.tsx`, `CheckDetail.tsx`, `Maintenance.tsx` | Synthetic checks, one check, maintenance windows |
| `#/slos` | `Slos.tsx` | SLOs |
| `#/ai` | `Ai.tsx` | Leasyd AI |

**Account and setup**

| Route | File | What it is |
|---|---|---|
| `#/start` | `GetStarted.tsx` | First data: key, what to monitor, waiting for data |
| `#/drop-rules` | `DropRules.tsx` | Data discarded as it arrives |
| `#/settings/<page>` (and the old `#/usage`) | `Settings.tsx` + `settings/` | Account and organization settings (below) |
| (signed out) | `SignIn.tsx` | Sign-in, sign-up, password reset |

## Page folders

**`synthetics/shared.tsx`** holds what Synthetics, CheckDetail and Maintenance share.

**`settings/`** holds the Settings pages:
- `menu.ts`: the menu, as ids and labels. The header reads it without loading the pages.
- `parts.tsx`: the page and section layout (title and explanation left, controls right) and `SettingsProps`.
- `Account.tsx`: Profile and Security.
- `Organization.tsx`: Organization and Team.
- `UsageCost.tsx`: Usage & cost.
- `Billing.tsx`: plan, Stripe, this month so far, prices.
- `ApiKeys.tsx`: API keys and AI tools.
- `GpuPrices.tsx`: GPU prices.

## Rules for a page

These follow from `src/README.md`:
- Take `ctx` (range, tick, now) from `App`.
- Get windows from `useRangeWindow(ctx)`.
- Load data through `useLoad` or its wrappers.
- Put filters in the link with `useHashParam`.
- Show failures as errors.
- Don't import another page; lint stops it. Exceptions:
  - Synthetics opens CheckDetail and Maintenance.
  - `Settings.tsx` imports its `settings/` pages.
  - Files in a page folder may import that folder's shared modules.
