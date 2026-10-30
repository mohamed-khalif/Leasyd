"""What happens to a trigger, and to an investigation in the background. Pure: everything outside
(storage, the queue, the agent, time) comes in through ports.

open_investigation    a trigger arrives (alert firing, webhook):
                        - the same dedupe key within DEDUPE_S joins the investigation already open
                          for it (one alert storm, one investigation);
                        - else, over the tenant's HOURLY_CAP, it is recorded as skipped (rate limit)
                          and still queued for delivery, so people see it was skipped;
                        - else it is queued to run.
run_investigation     the queue's message: queued/running -> the agent -> completed; an attempt that
                      raises is logged and retried by the queue, and the last one (MAX_ATTEMPTS)
                      marks it failed and delivers that. A finished investigation is left alone, so
                      a message delivered twice does nothing the second time.
"""

import time
from datetime import datetime, timezone

from domain.model import COMPLETED, FAILED, QUEUED, RUNNING, SKIPPED, Investigation
from ports import StaleWrite

DEDUPE_S = 30 * 60
HOURLY_CAP = 20
MAX_ATTEMPTS = 3


class SystemClock:
    def now(self):
        return time.time()


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_investigation(store, queue, clock, tenant, trigger, new_id, cap=HOURLY_CAP):
    """-> (investigation, joined). joined: the trigger was added to an open investigation."""
    now = clock.now()
    held = store.claim_dedupe(tenant, trigger.dedupe_key, new_id, int(now), int(now + DEDUPE_S))
    if held and (inv := store.get(tenant, held)):
        for _ in range(3):        # a worker may be saving it at the same moment: reload and add again
            inv.log(iso(now), "trigger_joined", f"{trigger.source}: {trigger.title}")
            try:
                store.save(inv)
                break
            except StaleWrite:
                inv = store.get(tenant, held)
        return inv, True
    inv = Investigation(id=new_id, tenant=tenant, trigger=trigger, status=QUEUED, created_at=iso(now), updated_at=iso(now))
    inv.log(iso(now), "triggered", f"{trigger.source}: {trigger.title}")
    hour = iso(now)[:13]
    if not store.take_quota(tenant, hour, cap, int(now + 2 * 3600)):
        inv.skip_reason = "rate_limit"
        inv.transition(SKIPPED, iso(now), f"more than {cap} investigations this hour")
        store.create(inv)
        queue.send({"deliver": {"tenant": tenant, "id": inv.id}})
        return inv, False
    store.create(inv)
    queue.send({"run": {"tenant": tenant, "id": inv.id}})
    return inv, False


def run_investigation(store, agent, queue, clock, tenant, inv_id, attempt=1, max_attempts=MAX_ATTEMPTS):
    """-> what happened ("completed", "failed", "done already", "missing", "busy"). Raises when the
    attempt failed and the queue should retry it."""
    inv = store.get(tenant, inv_id)
    if inv is None:
        return "missing"
    if inv.final:
        return "done already"
    if inv.status == QUEUED:
        inv.transition(RUNNING, iso(clock.now()), f"agent {agent.name}")
    else:
        inv.log(iso(clock.now()), "retry", f"attempt {attempt}")
    inv.attempts += 1
    try:
        store.save(inv)                  # claims the run: a second worker holding a stale copy stops here
    except StaleWrite:
        return "busy"
    try:
        result = agent.investigate(inv).validate()
    except Exception as e:  # noqa: BLE001  logged on the investigation, then retried or failed
        inv.log(iso(clock.now()), "attempt_failed", f"{type(e).__name__}: {e}")
        if attempt < max_attempts:
            store.save(inv)
            raise
        inv.transition(FAILED, iso(clock.now()), f"gave up after {attempt} attempts")
        store.save(inv)
        queue.send({"deliver": {"tenant": tenant, "id": inv.id}})
        return FAILED
    inv.result = result
    inv.versions = {"agent": agent.name, **getattr(agent, "versions", {})}
    inv.transition(COMPLETED, iso(clock.now()), f"confidence {result.confidence:.2f}")
    store.save(inv)
    queue.send({"deliver": {"tenant": tenant, "id": inv.id}})
    return COMPLETED
