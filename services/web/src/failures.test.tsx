// B3: a failed request must show as a failure, never as "all clear", empty, or wrong numbers.
// Each test fails one endpoint of the mock backend and checks what the page says.
import "./test/dom";
import { test, before, afterEach } from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { App } from "./App";
import { failing, failWhen, inFlight, installMockBackend, requests } from "./test/mockBackend";
import { render, settle } from "./test/dom";

before(installMockBackend);
afterEach(() => { failing.clear(); failWhen.length = 0; });

async function idle() {
  for (let quiet = 0, i = 0; quiet < 3 && i < 100; i++) {
    await settle(100);
    quiet = inFlight === 0 ? quiet + 1 : 0;
  }
}

/** The text of the app at `route` while `fail` ("METHOD /path" -> status) fail. */
async function pageText(route: string, fail: Record<string, number>, keep?: (text: string) => Promise<void>) {
  for (const [k, v] of Object.entries(fail)) failing.set(k, v);
  location.hash = route;
  const view = await render(<App />);
  await idle();
  await act(async () => { dispatchEvent(new HashChangeEvent("hashchange")); });
  await idle();
  const text = view.container.textContent ?? "";
  await keep?.(text);
  await view.unmount();
  return text;
}

test("AI insights: a failed query is an error, not 'Nothing unusual'", async () => {
  const text = await pageText("/ai", { "POST /v1/app/query": 503 });
  assert.doesNotMatch(text, /Nothing unusual/);
  assert.match(text, /Couldn't compute insights/);
});

test("AI insights: an unreadable check list is an error, not hidden failing checks", async () => {
  const text = await pageText("/ai", { "GET /v1/app/checks": 500 });
  assert.doesNotMatch(text, /Nothing unusual/);
  assert.match(text, /Couldn't compute insights/);
});

test("who-am-I failing (not 401) shows an error with Retry, not the sign-in form", async () => {
  const text = await pageText("/", { "GET /v1/app/me": 503 });
  assert.doesNotMatch(text, /Sign in to Leasyd/);
  assert.match(text, /Couldn't reach Leasyd/);
  assert.match(text, /Retry/);
});

test("who-am-I answering 401 still shows the sign-in form", async () => {
  assert.match(await pageText("/", { "GET /v1/app/me": 401 }), /Sign in to Leasyd/);
});

test("GPU prices unreadable: the GPUs page says costs use list prices", async () => {
  assert.match(await pageText("/gpus", { "GET /v1/app/account/gpu-prices": 500 }), /Couldn't read your GPU prices/);
});

test("GPU prices unreadable: Settings shows the error and offers no edits that would overwrite them", async () => {
  const text = await pageText("/settings/gpu-prices", { "GET /v1/app/account/gpu-prices": 500 });
  assert.match(text, /Couldn't read your GPU prices/);
  assert.doesNotMatch(text, /Add price/);
});

test("drop rules: an unreadable account is an error, not 'only owners can change'", async () => {
  const text = await pageText("/drop-rules", { "GET /v1/app/account": 500 });
  assert.doesNotMatch(text, /Only the account's owners/);
  assert.match(text, /Couldn't check your role/);
});

for (const route of ["/slos", "/synthetics/windows", "/"]) {
  test(`checks unreadable on ${route}: an error, not an empty list`, async () => {
    assert.match(await pageText(route, { "GET /v1/app/checks": 500 }), /Couldn't read your synthetic checks/);
  });
}

test("dashboards: check names are asked on each visit; when they can't be read, series keep their ids", async () => {
  const route = "/dashboards/builtin-synthetics";
  requests.length = 0;
  let text = await pageText(route, { "GET /v1/app/checks": 500 });
  assert.equal(requests.filter((r) => r === "GET /v1/app/checks").length, 1);
  assert.doesNotMatch(text, /deleted check/);
  requests.length = 0;
  text = await pageText(route, {});
  assert.equal(requests.filter((r) => r === "GET /v1/app/checks").length, 1, "asked again on the next visit");
});

test("Home: check uptime unreadable is 'couldn't load', not every check healthy", async () => {
  const text = await pageText("/", { "POST /v1/app/query ~synthetics.check.success": 500 });
  assert.match(text, /Degraded–couldn't load/);
  assert.match(text, /Healthy–couldn't load/);
});

test("Home: a card whose query failed says so instead of waiting for ever", async () => {
  const text = await pageText("/", { "POST /v1/app/query ~\"group_by\":[\"service\"]": 500 });
  assert.match(text, /Services–couldn't load/);
});

test("SLOs: the last hour unreadable is 'couldn't load', never 'healthy'", async () => {
  failWhen.push({ path: "/v1/app/query", status: 500,
                  when: (b) => typeof b.start === "string" && Date.parse(String(b.end)) - Date.parse(b.start) <= 3_600_000 });
  const text = await pageText("/slos", {});
  assert.doesNotMatch(text, /healthy/);
  assert.match(text, /couldn't load/);
});

test("Usage & cost: one signal unreadable is 'couldn't load', not a smaller total", async () => {
  const text = await pageText("/settings/usage", { "POST /v1/app/query ~\"signal\":\"logs\"": 500 });
  assert.match(text, /Estimated total, next 30 days–/);
  assert.match(text, /couldn't load logs/);
});

test("Usage & cost: drop shares unreadable say so, not 'nothing dropped'", async () => {
  const text = await pageText("/settings/usage", { "GET /v1/app/account/meters": 500 });
  assert.match(text, /couldn't load what drop rules discard/);
});

test("Kubernetes: unreadable data is 'couldn't load', never 0 problems or all ready", async () => {
  const text = await pageText("/kubernetes", {});      // no engine in tests: every Kubernetes query fails
  assert.doesNotMatch(text, /Pods with problems0/);
  assert.doesNotMatch(text, /All nodes ready/);
  assert.match(text, /Nodes–couldn't load/);
  assert.match(text, /Pods with problems–couldn't load/);
});

test("GPUs: unreadable data is 'couldn't load', never $0 spend, nothing to reclaim or 0 errors", async () => {
  const work = await pageText("/gpus?tab=workloads", {});   // no engine in tests: every GPU query fails
  assert.doesNotMatch(work, /Nothing to reclaim/);
  assert.match(work, /Estimated spend–couldn't load/);
  const health = await pageText("/gpus?tab=health", {});
  assert.match(health, /GPUs with XID errors–couldn't load/);
  assert.doesNotMatch(health, /GPUs with XID errors0/);
});
