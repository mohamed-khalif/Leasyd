// One smoke test per page (Phase 5 safety net): the real App at each route, against the mock
// backend. A page passes when it renders, its requests finish, and React reports no error.
// This guards the refactor in Phase 6; what each page shows is checked by hand in the preview.
import "./test/dom";
import { test, before, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { installMockBackend, inFlight, requests } from "./test/mockBackend";
import { render, settle } from "./test/dom";

const ROUTES = ["/", "/logs", "/traces", "/metrics", "/services", "/web", "/lambda", "/kubernetes", "/gpus",
  "/dashboards", "/dashboards/builtin-services", "/query", "/sql", "/alerts", "/alerts/rules/new", "/synthetics",
  "/slos", "/ai", "/start", "/drop-rules", "/settings", "/usage", "/agents", "/agents?tab=issues",
  ...["profile", "security", "organization", "team", "usage", "billing", "api-keys", "gpu-prices", "ai-tools"].map((p) => `/settings/${p}`)];

const errors: string[] = [];
const original = console.error;
before(installMockBackend);
beforeEach(() => {
  errors.length = 0;
  console.error = (...args: unknown[]) => { errors.push(args.map(String).join(" ")); };
});
afterEach(() => { console.error = original; });

/** Wait until no request has been in flight for a moment (the mock answers in 150-400 ms). */
async function idle() {
  for (let quiet = 0, i = 0; quiet < 3 && i < 100; i++) {
    await settle(100);
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}

for (const route of ROUTES) {
  test(`page ${route} renders against the mock backend`, async () => {
    location.hash = route;
    requests.length = 0;
    const view = await render(<App />);
    await idle();
    await act(async () => { dispatchEvent(new HashChangeEvent("hashchange")); });
    await idle();
    const text = view.container.textContent ?? "";
    assert.ok(text.length > 50, `page ${route} rendered almost nothing: ${JSON.stringify(text.slice(0, 80))}`);
    assert.ok(requests.some((r) => r.startsWith("GET /v1/app/me")), "the app asked who is signed in");
    const reactErrors = errors.filter((e) => /error occurred|Uncaught|is not a function|Cannot read prop/i.test(e));
    assert.deepEqual(reactErrors, [], `React errors on ${route}`);
    await view.unmount();
  });
}
