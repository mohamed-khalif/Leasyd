// Queries in flight at once, per page (the app shows one page at a time): Home alone asks about 20
// at once. The rest wait their turn, in order. The server has its own per-account limit
// (INFLIGHT_LIMITS in query.py), which this keeps well clear of.
export const MAX_IN_FLIGHT = 6;

const queue: (() => void)[] = [];
let running = 0;
/** Runs f when fewer than MAX_IN_FLIGHT are running. An aborted query leaves the queue, or
 *  (running) is aborted and frees its place. */
export function limited<T>(f: (signal?: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
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
  });
}
