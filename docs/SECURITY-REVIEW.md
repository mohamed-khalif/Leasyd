# Security review, 2026-10-01

Scope: the whole platform as built: tenant isolation, sign-in, API keys, sign-up, the query
engines (JSON, PromQL, SQL), synthetic checks, alert channels, the AI SRE, the web app, IAM, storage,
secrets in the repository and dependencies.

## Fixed in this review

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | Signed-in users had no per-customer query limit. A script (or any free sign-up) could start unlimited 5-minute background queries, using up the account's Lambda capacity (shared with every customer's ingest) and running up the bill. | High | Per tenant and minute: 300 queries (free) / 600 (standard), a burst guard since one page of charts runs 20-40 at once, of which 5 / 10 may run in the background; and a daily search allowance of 2,000 / 20,000 search units (one per query worker, ~256 MB read; a query with no data to read costs nothing), changeable per tenant (`obs-tenant-admin set-search`) and shown in Settings; 429 past either. Counters in obs-tenants, expiring by TTL. `services/compaction/query.py` `over_limit` |
| 2 | "Test" and "Run now" on synthetic checks were unlimited: the runner could be used to send floods of requests to any public site from our addresses (and each browser test runs a browser). | High | 10 manual runs a minute per tenant; 429 past it. `services/synthetics/synthetics.py` `_count_manual_run` |
| 3 | Users could change their own email (confirmed on AWS with a test user: the new, unverified address appeared in their sign-in token). Not a way into other tenants (every account check looks the person up by tenant and email), but someone could hold an address that isn't theirs, block its owner's sign-up, and a renamed user could not be removed (removal looks the login up by the old email). | Medium | The web app's sign-in client can no longer write the email (`infra/state.yaml`; live account: `infra/state-settings.py`) |
| 4 | Sign-in tokens lasted 60 minutes and can't be revoked early, so a removed teammate kept read access for up to an hour. | Medium | Tokens last 15 minutes (the app renews them silently; removal already revokes the renewal). |
| 5 | The web app had no Content-Security-Policy, so any future script-injection bug would let a script read the sign-in token. | Low (defence in depth; no injection found) | Strict CSP (own scripts only, no inline scripts, connects only to its own origin and Cognito, can't be framed), plus HSTS and the other security headers. The one inline script moved to `public/global.js`. Verified in Chromium against the built app and real data: every page works; an injected inline script is blocked. |

## Checked and sound

- **Tenant isolation is enforced by AWS, not only by code.** Every read of tenant data goes through
  `obs-tenant-reader`, assumed with a session tag of the one tenant; its policy only reaches
  `data/tenant=<that tenant>/` in S3 and index keys starting `<tenant>#`. A bug in query code can't read another tenant.
- **The tenant comes only from the authorizer** (API key record, or the immutable `custom:tenant`
  claim that users can't write); a tenant in a request body is ignored (covered by each API's tests).
- **API keys:** stored as SHA-256 hashes only; revoked and expiring keys refused; per-key throttling (usage plans).
- **SQL:** a single SELECT over logs/spans/metrics only; table functions, other tables and schemas
  refused by parsing; then DuckDB runs with no file, network or extension access and settings locked,
  over files already downloaded with the tenant's own credentials. Tried: `read_text`, `glob`,
  `getenv`, `duckdb_secrets()`, file paths as tables, catalog tables, CTE tricks: all refused.
- **Outbound requests** (synthetic checks, browser checks, webhook/Slack alerts): only public
  addresses, checked after DNS resolution and connected to the checked address (no DNS rebinding); redirects re-checked.
- **Ingest:** gzip bodies capped at 64 MB decompressed; daily caps per tenant.
- **Storage:** S3 buckets private (public access blocked), encrypted, TLS-only; check secrets
  encrypted with KMS; DynamoDB with point-in-time recovery.
- **IAM:** every role under the `obs-boundary` permissions boundary; wildcard resources only
  where AWS has no resource-level permissions (logs, metrics, ECR login).
- **Repository:** no keys, tokens or passwords in the code or its history. Web dependencies: `npm audit`: 0 vulnerabilities.

## Recommended next (not done)

- Two-factor sign-in (TOTP) as an option, required for owners.
- An audit log customers can see (who invited whom, created or revoked keys, changed checks).
- Bot protection on the public sign-up form (e.g. AWS WAF CAPTCHA) once there's real traffic;
  today it's throttled (1/s), limited per address and per day, and has a hidden bot field.
- An outside penetration test before larger customers.
