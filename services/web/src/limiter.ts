// Queries in flight at once, per page (the app shows one page at a time): Home alone asks about 20
// at once. The rest wait their turn, in order. The server has its own per-account limit
// (INFLIGHT_LIMITS in query.py), which this keeps well clear of.
export const MAX_IN_FLIGHT = 6;

const queue: (() => void)[] = [];
let running = 0;

// Questions running or waiting, for the header's loading spinner (Shell.tsx, useBusy).
const listeners = new Set<() => void>();
let busy = 0;
function changed(by: number) { busy += by; listeners.forEach((l) => l()); }
export function busyCount(): number { return busy; }
export function onBusy(l: () => void): () => void { listeners.add(l); return () => { listeners.delete(l); }; }
/** Runs f when fewer than MAX_IN_FLIGHT are running. An aborted query leaves the queue, or
 *  (running) is aborted and frees its place. */
export function limited<T>(f: (signal?: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
  changed(1);
  return new Promise<T>((resolve, reject) => {
    const go = () => {
      signal?.removeEventListener("abort", leave);
      if (signal?.aborted) { reject(new DOMException("The operation was aborted.", "AbortError")); queue.shift()?.(); return; }
      running++;
      f(signal).then(resolve, reject).finally(() => { running--; queue.shift()?.(); });
    };
    const leave = () => {
      const i = queue.indexOf(go);
      if (i >= 0) { queue.splice(i, 1); reject(new DOMException("The operation was aborted.", "AbortError")); }
    };
    if (running < MAX_IN_FLIGHT) go();
    else { queue.push(go); signal?.addEventListener("abort", leave, { once: true }); }
  }).finally(() => changed(-1));
}
