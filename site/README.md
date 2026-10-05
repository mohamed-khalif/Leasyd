# leasyd.com website

Static pages for the public website (the app is app.leasyd.com). Hosted by `obs-site`
(`infra/site.yaml`: S3 + CloudFront at www.leasyd.com); deploy with `infra/deploy-site.sh`.

- `index.html`: the landing page (light theme). `hero.js` builds and animates its tilted dashboard
  (no data fetched; still for reduced motion); `home.js` runs the Products list and sizes the dashboard.
  `img/app-*.jpg` are screenshots of the app (light theme, mock data).
- `pricing.html` (+ `pricing.js`, the estimator): served at /pricing. Prices must match
  `services/web/src/pricing.ts` (the in-app Usage & Cost page).
- `docs.html` (/docs): the product documentation.
- `terms.html` (/terms) and `privacy.html` (/privacy): Terms of Service and Privacy Policy. DRAFTS: the
  [bracketed] items (legal entity, address, jurisdiction...) must be completed, and a lawyer should
  review both, before launch.
- `404.html`: any missing page.


DNS: www.leasyd.com is delegated to its Route 53 zone (4 NS records at GoDaddy); the bare
leasyd.com is forwarded to https://www.leasyd.com by GoDaddy, so email (MX) is untouched.
