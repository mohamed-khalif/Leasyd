# Leasyd web app

The browser app at app.leasyd.com: React 19, TypeScript and Vite. It is a single-page app with hash routes (`#/logs`, `#/settings/billing`, …), hosted on S3 and CloudFront.

## Run it

| Command | What it does |
|---|---|
| `npm install` | Install dependencies. |
| `npm run mock` | Run locally against the built-in mock backend (`mock/mockApi.ts`). No AWS needed. |
| `python3 mock/engine_server.py 6` | Optional, alongside `npm run mock`: real Kubernetes and GPU data from Leasyd's query engine, over 6 hours of the demo cluster. |
| `npm run dev` | Run against a real backend, as set in `public/config.json`. |
| `npm run build` | Type-check, then build into `dist/`. |
| `infra/deploy-phaseW1.sh` | Deploy, from the repository root. |

## Check it

| Command | What it does |
|---|---|
| `npm test` | All tests, against the mock backend (node:test, bundled by esbuild, happy-dom). |
| `npm test -- <name>` | Only the test files whose name contains `<name>`. |
| `npm run lint` | ESLint, including the project rules listed in `src/README.md`. |
| `npm run typecheck` | `tsc`. |

## Measure it

`scripts/perf/` compares page timing and request counts of two versions, on production builds. `docs/phase7-report.md` has the steps and the last results.

The dev server also keeps a log of every request in `window.__leasydRequests` (`src/devRequests.ts`). It's left out of production builds.

## Where things are

| Path | What's there |
|---|---|
| `src/` | The app. `src/README.md` maps its folders and rules. |
| `mock/` | The mock backend used by `npm run mock` and the tests, and the local engine server. |
| `public/` | `config.json` (region, Cognito, API address) and `global.js`. |
| `scripts/` | Test runner, query export for the engine's contract test, perf scripts. |
| `docs/formatting.md` (repository root) | How every number, date and size is shown. |
| `docs/ARCHITECTURE.md` (repository root) | How data flows, section 0. |
