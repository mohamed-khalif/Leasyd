// B6: dashboard variables in panel queries.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fillQuery } from "./pages/Dashboards";

const f = (text: string, vars: Record<string, string[]>) => fillQuery(text, vars, 60);

test("as before: regex matchers get the values, none selected is any, steps filled in", () => {
  assert.equal(f('rate(x{service_name=~"$service_name"}[$__interval])', { service_name: ["api"] }), 'rate(x{service_name=~"api"}[60s])');
  assert.equal(f('x{service_name=~"$service_name"}', { service_name: ["api", "web"] }), 'x{service_name=~"api|web"}');
  assert.equal(f('x{service_name=~"$service_name"}', { service_name: [] }), 'x{service_name=~".*"}');
  assert.equal(f('x{"host.name"=~"${host_name}"}', { host_name: ["a.b"] }), 'x{"host.name"=~"a\\\\.b"}');
});

test("$host does not rewrite the start of $host_name", () => {
  assert.equal(f('x{a=~"$host", b=~"$host_name"}', { host: ["h1"], host_name: ["n1"] }), 'x{a=~"h1", b=~"n1"}');
  assert.equal(f('x{b=~"$host_name"}', { host: ["h1"] }), 'x{b=~".*"}');
});

test("a variable name is not taken as a regex", () => {
  assert.equal(f('x{a=~"$a_b", c=~"$axb"}', { "a.b": ["v"] } as Record<string, string[]>), 'x{a=~".*", c=~".*"}');
});

test("with = one value is matched as written; several switch the matcher to a regex", () => {
  assert.equal(f('x{host="$host"}', { host: ["a.b"] }), 'x{host="a.b"}');
  assert.equal(f('x{host!="$host"}', { host: ["a.b"] }), 'x{host!="a.b"}');
  assert.equal(f('x{host="$host"}', { host: ["a.b", "c"] }), 'x{host=~"a\\\\.b|c"}');
  assert.equal(f('x{host="$host"}', { host: [] }), 'x{host=~".*"}');
});

test("a $word inside a user's own regex is left alone", () => {
  assert.equal(f('x{path=~"^/api$|^/v1$total"}', {}), 'x{path=~"^/api$|^/v1$total"}');
});
