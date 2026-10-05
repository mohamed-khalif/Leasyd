# leasyd.com website

Static pages for the public website (the app is app.leasyd.com). Hosted by `obs-site`
(`infra/site.yaml`: S3 + CloudFront at www.leasyd.com); deploy with `infra/deploy-site.sh`.

- `index.html`: the landing page (dark): hero with a screenshot carousel, cost comparison, a section per
  product with card rows, sign-up boxes that open the app's sign-up with the e-mail filled in. `site.js`
  runs the carousel, card arrows, the typing MCP card and the sign-up boxes. `img/shot-*.webp` (hero),
  `img/card-*.webp` (cards) and `img/phone-home.webp` are screenshots of the app (dark theme, mock data, 1.5x).
- `pricing.html` (+ `pricing.js`, the estimator): served at /pricing. Prices must match
  `services/web/src/pricing.ts` (the in-app Usage & Cost page).
- `docs.html` (/docs): the product documentation.
- `terms.html` (/terms) and `privacy.html` (/privacy): Terms of Service and Privacy Policy. DRAFTS: the
  [bracketed] items (legal entity, address, jurisdiction...) must be completed, and a lawyer should
  review both, before launch.
- `404.html`: any missing page.


DNS: www.leasyd.com is delegated to its Route 53 zone (4 NS records at GoDaddy); the bare
leasyd.com is forwarded to https://www.leasyd.com by GoDaddy, so email (MX) is untouched.
