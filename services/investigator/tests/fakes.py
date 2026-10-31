"""In-memory ports for tests and evals: behave as the AWS adapters do (dedupe claims, hourly quota,
optimistic locking), with nothing outside the process."""

import copy

from domain.model import Investigation
from ports import StaleWrite


class MemoryStore:
    def __init__(self):
        self.invs, self.claims, self.quota, self.hooks = {}, {}, {}, {}

    def get(self, tenant, inv_id):
        d = self.invs.get((tenant, inv_id))
        return Investigation.from_dict(copy.deepcopy(d)) if d else None

    def create(self, inv):
        if (inv.tenant, inv.id) in self.invs:
            raise StaleWrite("exists")
        inv.version = 1
        self.invs[(inv.tenant, inv.id)] = copy.deepcopy(inv.to_dict())

    def save(self, inv):
        cur = self.invs[(inv.tenant, inv.id)]
        if cur["version"] != inv.version:
            raise StaleWrite("stale")
        inv.version += 1
        self.invs[(inv.tenant, inv.id)] = copy.deepcopy(inv.to_dict())

    def claim_dedupe(self, tenant, key, inv_id, now_epoch, until_epoch):
        held = self.claims.get((tenant, key))
        if held and held[1] >= now_epoch:
            return held[0]
        self.claims[(tenant, key)] = (inv_id, until_epoch)
        return None

    def take_quota(self, tenant, hour, limit, until_epoch):
        n = self.quota.get((tenant, hour), 0)
        if n >= limit:
            return False
        self.quota[(tenant, hour)] = n + 1
        return True

    def tenant_of_webhook(self, token_hash):
        return self.hooks.get(token_hash)

    def list(self, tenant, limit=50):
        return [d for (t, _), d in self.invs.items() if t == tenant][:limit]


class ListQueue:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class Clock:
    def __init__(self, t=1_798_000_000.0):
        self.t = t

    def now(self):
        return self.t


class MemoryChannels:
    """The Channels port: channels by tenant, settings, rules, the hourly skipped notice."""

    def __init__(self, channels=None, settings=None, rules=None):
        self.channels, self.cfg, self.rules, self.notices = channels or {}, settings or {}, rules or {}, set()

    def get(self, tenant, cid):
        return self.channels.get((tenant, cid))

    def exists(self, tenant, cid):
        return (tenant, cid) in self.channels

    def settings(self, tenant):
        return self.cfg.get(tenant, {})

    def rule(self, tenant, rule_id):
        return self.rules.get((tenant, rule_id))

    def first_skip_notice(self, tenant, hour):
        if (tenant, hour) in self.notices:
            return False
        self.notices.add((tenant, hour))
        return True
