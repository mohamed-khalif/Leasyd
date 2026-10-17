// A DOM for component and hook tests (happy-dom), and helpers that render through React's act().
// Import this first in a test file: React must see the DOM when it loads.
import { after } from "node:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";

if (!("document" in globalThis)) {
  GlobalRegistrator.register({ url: "http://app.test/" });
  after(() => GlobalRegistrator.unregister());   // else happy-dom's timers keep the test process alive
}
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { act } = await import("react");
const { createRoot } = await import("react-dom/client");

export async function render(element: React.ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(element));
  return {
    container,
    rerender: (next: React.ReactNode) => act(async () => root.render(next)),
    unmount: () => act(async () => root.unmount()),
  };
}

/** Let pending promises and timers (up to ms) run, inside act. */
export const settle = (ms = 0) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

/** A promise resolved from outside: a request the test answers when it chooses. */
export function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}
