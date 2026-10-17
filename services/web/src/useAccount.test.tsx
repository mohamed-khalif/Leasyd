import "./test/dom";
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { useAccount, type AccountLoad } from "./useAccount";
import { answer, calls, install } from "./test/fetchStub";
import { render, settle } from "./test/dom";

before(install);

function Probe(p: { tick?: number; poll?: number; seen: AccountLoad[] }) {
  p.seen.push(useAccount(p.tick ?? 0, p.poll ?? 0));
  return null;
}
const acc = (records: number) => ({ status: 200, body: { tenant: "acme", you: { role: "owner" }, today: { records } } });

test("loads on mount and on Refresh; a failed reload keeps the account and says why", async () => {
  let n = 0;
  answer(() => (++n === 2 ? { status: 503, body: { error: "try again" } } : acc(n)));
  const seen: AccountLoad[] = [];
  const view = await render(<Probe seen={seen} />);
  await settle();
  assert.equal(seen.at(-1)!.account?.today?.records, 1);
  await view.rerender(<Probe tick={1} seen={seen} />);
  await settle();
  assert.equal(seen.at(-1)!.account?.today?.records, 1);
  assert.equal(seen.at(-1)!.error, "try again");
  await seen.at(-1)!.reload();
  await settle();
  assert.equal(seen.at(-1)!.account?.today?.records, 3);
  assert.equal(seen.at(-1)!.error, null);
  await view.unmount();
});

test("polls when asked, and stops on unmount", async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  answer(() => acc(1));
  const seen: AccountLoad[] = [];
  const view = await render(<Probe poll={10_000} seen={seen} />);
  await settle();
  t.mock.timers.tick(10_000); await settle();
  t.mock.timers.tick(10_000); await settle();
  assert.equal(calls.length, 3);
  await view.unmount();
  t.mock.timers.tick(30_000); await settle();
  assert.equal(calls.length, 3);
});
