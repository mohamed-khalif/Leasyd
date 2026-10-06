"""Tenant operations (T5): the platform's control plane, one Lambda.

Invoke with {"action": ..., ...}; infra/tenant.sh wraps it.

  create  {tenant, plan, buffer_seconds?}  ingest streams, tenant record, first API key (returned once)
  rotate  {tenant, grace_hours, scope?}  a new key of that scope (default ingest); the tenant's
                                 other keys of that scope keep working for grace_hours
                                 (default 24), then are refused
  read-key {tenant}              an extra key that may only query (POST /v1/query), never send;
                                 revoke it with revoke {tenant, key_id}
  invite-user {tenant, email, send_email?, role?, invited_by?}  a person who signs in to the
                                 product (Cognito user pool, U1) as a member of the tenant; they
                                 get an email with a temporary password (send_email=false: none,
                                 for tests). Sent with SES (emails.py) when EMAIL_FROM is set, else
                                 by Cognito. role: owner (manages users and keys) or member
  signup  {tenant, email, company}  self-service sign-up (obs-account-api reserves the tenant
                                 name, then invokes this): a free-plan tenant without keys (its
                                 owner creates them in the app), and the owner's login, welcomed
                                 by email
  add-key {tenant, scope}        one more key of that scope (ingest or read), others unchanged
  set-cap {tenant, daily_gb}     the most data a day ingest accepts (null: the plan's; 0: no cap)
  set-search {tenant, units_per_day}  the daily search allowance (null: the plan's; 0: no limit)
  upgrade {tenant}               a trial (free plan) becomes a paying account: standard plan, its
                                 keys moved to the standard usage plan, no trial end, no daily cap
  extend-trial {tenant, days}    the trial ends this many days from now (data accepted again)
  set-billing {tenant, billing_status, customer?, subscription?, new?}  from Stripe's events
                                 (billing.py): a billed status upgrades a trial; canceled or
                                 unpaid: ingest refuses data
  report-usage {day?}            scheduled daily: subscribed tenants' usage of the last days to
                                 Stripe's meters, each day once (billed#<tenant>#<day>)
  remove-user {tenant, email}    sign them out everywhere and delete the login
  users   {tenant}               the tenant's users
  revoke  {tenant, key_id?}      refuse one key, or all of the tenant's keys
  delete  {tenant}               refuse all keys, cancel its Stripe subscription, remove the streams, then purge every
                                 object and index entry of the tenant (see below)
  tune    {tenant, buffer_seconds}  how long the tenant's Firehose streams buffer before writing
                                 a file (default BUFFER_SECONDS). Shorter for high-volume
                                 tenants: smaller files are parsed sooner and faster
                                 (fresher data), at the cost of more files
  status  {tenant}               tenant record and keys (never the keys themselves)
  refill-keys {}                 top up the pools of aged API Gateway keys that new keys borrow
                                 (scheduled through sweep; see KEY_POOL_SIZE)
  usage   {tenant, start, end}   records and bytes compacted per day and signal
  list    {}                     every tenant
  sweep   {}                     scheduled: expire rotated keys, advance deletions
  retention {}                   scheduled daily: delete every active tenant's data older than
                                 RETENTION_DAYS (see below)
  restore {}                     after the API stack is recreated (infra/up.sh): every active
                                 tenant's streams exist and its live keys are in the current
                                 usage plans. Safe to repeat
  purge   {tenant}               internal: one purge pass (re-invokes itself if long)

Plans: "free" (self-service sign-ups) and "standard"; each has an API Gateway usage plan (rate
limits) and a daily data cap (PLAN_DAILY_GB; the tenant record's daily_cap_bytes, enforced by
ingest, which meters bytes per tenant per day in meter#<tenant>#<day>).

Keys: only their SHA-256 is stored (obs-tenants, pk "key#<hash>"); the
authorizer maps a key to its tenant and scope from there. Scope "ingest"
(the default, and every key made before scopes) may only send data; "read"
may only query. An SDK's key therefore can't be used to read data back. API Gateway also holds each
key, for the tenant's usage plan (rate limits).

Deletion: the tenant is marked "deleting" and its keys revoked, so nothing
new is accepted (API Gateway may honour a cached "allow" for up to a minute).
Its Firehose streams are deleted. A purge pass then deletes everything under
_incoming/tenant=<T>/, _incoming/_errors/tenant=<T>/ and data/tenant=<T>/, and
every obs-index item whose key starts "<T>#" or "_lease#<T>#". A compaction
worker or fast-lane indexer already running for the tenant can still write
after that pass, so passes repeat SETTLE_MINUTES apart (longer than a
worker's lease) until one finds nothing; then the tenant is "deleted".
Usage records are the platform's billing records and are kept. The tenant's
users are signed out and their logins deleted (an ID token already issued
stays valid until it expires, at most an hour, and can only reach the
tenant's purged data).

Retention: data is kept RETENTION_DAYS full days plus today (UTC): every event day before
today - RETENTION_DAYS is deleted, for every signal, passes and failures of synthetic checks
alike (they are the tenant's data). Index entries go first, so a query never lists a file that
is about to disappear; then the day's Parquet files, filters, ID digests and any raw files that
arrived that day, and the index items of those days. Queries never ask for earlier than the same
day (query.py clamps the start), so what a customer sees doesn't change while a sweep runs. An
S3 lifecycle rule expires anything under data/ and _incoming/ a few days later still, as a
backstop. Usage records are kept.
"""

import decimal
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key

import billing
import emails

TENANTS = os.environ["TENANTS_TABLE"]
INDEX = os.environ["INDEX_TABLE"]
USAGE = os.environ["USAGE_TABLE"]
BUCKET = os.environ["BUCKET"]
FIREHOSE_ROLE = os.environ["FIREHOSE_ROLE_ARN"]
PLANS = json.loads(os.environ.get("USAGE_PLANS", "{}"))  # plan name -> API Gateway usage plan id
PLAN_DAILY_GB = {"free": 1, "standard": None, "test-tiny": None}   # data a day ingest accepts (None: no cap)
# Search units a day (one per query worker, ~256 MB read), as obs-query-api enforces them
# (services/compaction/query.py DAILY_SEARCH_UNITS); a tenant's search_units_per_day overrides.
PLAN_SEARCH_UNITS = {"free": 2_000, "standard": 20_000}
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "7"))   # a self-service sign-up's free trial (no card)
GB = 10**9
SELF = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "obs-tenant-admin")
BUFFER_SECONDS = int(os.environ.get("BUFFER_SECONDS", "30"))
SETTLE = timedelta(minutes=int(os.environ.get("SETTLE_MINUTES", "20")))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
SIGNALS = ("logs", "traces", "metrics")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
USER_POOL = os.environ.get("USER_POOL_ID", "")   # Cognito user pool of obs-state (empty: no logins)
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
SCOPES = {"ingest", "read"}   # see the authorizer: ingest sends, read queries
_KEY_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
# A new API Gateway key takes minutes (measured up to 12+) to reach every API Gateway node, and is
# refused (403, which OpenTelemetry exporters drop) by some requests until then. So customer keys
# borrow a gateway key made earlier: keypool#<plan> holds keys made at least KEY_POOL_MIN_AGE ago,
# the authorizer passes the borrowed one as usageIdentifierKey, and the customer's own key works at
# once. The sweep (every 15 minutes) tops each pool up to KEY_POOL_SIZE.
KEY_POOL_SIZE = int(os.environ.get("KEY_POOL_SIZE", "10"))
KEY_POOL_MIN_AGE = timedelta(minutes=int(os.environ.get("KEY_POOL_MIN_AGE_MINUTES", "30")))
KEY_POOL_PLANS = ("free", "standard")
RETIRE_AFTER = timedelta(minutes=10)   # a replaced gateway key outlives the authorizer's 60 s cache

ddb = boto3.resource("dynamodb")
tenants = ddb.Table(TENANTS)
index = ddb.Table(INDEX)
usage_t = ddb.Table(USAGE)
cognito = boto3.client("cognito-idp")
s3 = boto3.client("s3")
apigw = boto3.client("apigateway")
firehose = boto3.client("firehose")
lam = boto3.client("lambda")
sns = boto3.client("sns")


class Refused(Exception):
    pass


def handler(event, context):
    action = event.get("action")
    fn = ACTIONS.get(action)
    if fn is None:
        return {"error": f"unknown action {action!r}; one of {sorted(ACTIONS)}"}
    args = {k: v for k, v in event.items() if k != "action"}
    if action not in ("list", "sweep", "restore", "retention", "report-usage", "refill-keys"):
        args["tenant"] = check_tenant(args.get("tenant"))
    try:
        out = fn(context=context, **args)
    except Refused as e:
        out = {"error": str(e)}
    out = json.loads(json.dumps(out, default=_plain))
    # Never log a key.
    print(json.dumps({"action": action, "tenant": args.get("tenant"),
                      "result": {k: v for k, v in out.items() if k != "api_key"}}, default=str))
    return out


def _plain(v):
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, set):
        return sorted(v)
    return str(v)


def check_tenant(tenant):
    if not isinstance(tenant, str) or not _TENANT.match(tenant):
        raise ValueError(f"invalid tenant id {tenant!r}: 3-40 chars of a-z, 0-9, '-'")
    return tenant


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ tenant record

def _record(tenant):
    return tenants.get_item(Key={"pk": f"tenant#{tenant}"}, ConsistentRead=True).get("Item")


def _keys(tenant):
    return _items(tenant, "key#")


def _users(tenant):
    return _items(tenant, "user#")


def _items(tenant, prefix):
    items, kw = [], dict(IndexName="by-tenant", KeyConditionExpression=Key("tenant").eq(tenant)
                         & Key("pk").begins_with(prefix))
    while True:
        page = tenants.query(**kw)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# ------------------------------------------------------------------ actions

def create(tenant, plan="standard", buffer_seconds=None, company=None, issue_key=True, context=None):
    if plan not in PLANS:
        raise Refused(f"unknown plan {plan!r}; one of {sorted(PLANS)}")
    secs = _buffer_seconds(BUFFER_SECONDS if buffer_seconds is None else buffer_seconds)
    rec = _record(tenant)
    if rec and rec["status"] == "deleting":
        raise Refused(f"{tenant} is being deleted; wait until it is deleted")
    if rec and rec["status"] == "active":
        raise Refused(f"{tenant} already exists; use rotate for a new key")
    _provision_streams(tenant, secs)
    item = {"pk": f"tenant#{tenant}", "tenant": tenant, "status": "active", "plan": plan,
            "buffer_seconds": secs, "created_at": _iso(_now()), "company": company or (rec or {}).get("company") or tenant}
    if PLAN_DAILY_GB.get(plan):
        item["daily_cap_bytes"] = PLAN_DAILY_GB[plan] * GB
    tenants.put_item(Item=item)
    if not issue_key:
        return {"tenant": tenant, "plan": plan}
    key, key_id = _issue_key(tenant, plan)
    return {"tenant": tenant, "plan": plan, "key_id": key_id, "api_key": key}


def signup(tenant, email, company, context=None):
    """A self-service sign-up: the free-plan tenant (reserved by obs-account-api as "creating"),
    then its owner's login and welcome email. Its keys are made in the app."""
    rec = _record(tenant)
    if not rec or rec["status"] not in ("creating", "active"):
        raise Refused(f"{tenant} was not reserved for a sign-up")
    if rec["status"] == "creating":
        create(tenant, plan="free", company=company, issue_key=False)
        # The free trial: ingest refuses data after this (obs-ingest), checks stop (synthetics tick).
        tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET trial_ends_at = :t",
                            ExpressionAttributeValues={":t": _iso(_now() + timedelta(days=TRIAL_DAYS))})
    tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET signed_up_by = :e",
                        ExpressionAttributeValues={":e": _email(email)})
    out = invite_user(tenant, email, role="owner", welcome=True)
    return {"tenant": tenant, "email": out["email"], "status": "signed up", "email_sent": out["email_sent"]}


def add_key(tenant, scope="ingest", context=None):
    if scope not in SCOPES:
        raise Refused(f"unknown scope {scope!r}; one of {sorted(SCOPES)}")
    rec = _active(tenant)
    key, key_id = _issue_key(tenant, rec.get("plan", "standard"), scope)
    return {"tenant": tenant, "scope": scope, "key_id": key_id, "api_key": key}


def set_cap(tenant, daily_gb=None, context=None):
    rec = _active(tenant)
    if daily_gb is None:
        daily_gb = PLAN_DAILY_GB.get(rec.get("plan", "standard"))
    try:
        cap = int(float(daily_gb) * GB) if daily_gb else 0
    except (TypeError, ValueError):
        raise Refused("daily_gb: a number of GB a day (0: no cap)")
    if cap < 0:
        raise Refused("daily_gb: a number of GB a day (0: no cap)")
    if cap:
        tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET daily_cap_bytes = :c",
                            ExpressionAttributeValues={":c": cap})
    else:
        tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="REMOVE daily_cap_bytes")
    return {"tenant": tenant, "daily_cap_bytes": cap or None}


def upgrade(tenant, context=None):
    """Trial -> paying: the standard plan for the tenant and its live keys (each moved to the
    standard usage plan), no trial end, and no daily data cap."""
    rec = _active(tenant)
    moved = []
    for k in _keys(tenant):
        if k["status"] in ("active", "expiring") and k.get("plan", "standard") != "standard":
            # An aged gateway key of the standard plan takes over at once; moving the key's own
            # gateway key between usage plans would be refused by some requests for minutes.
            pooled = _pool_take("standard")
            if pooled:
                tenants.update_item(Key={"pk": k["pk"]}, UpdateExpression="SET #p = :s, api_key_id = :i, gateway_key = :g",
                                    ExpressionAttributeNames={"#p": "plan"},
                                    ExpressionAttributeValues={":s": "standard", ":i": pooled["id"], ":g": pooled["value"]})
                _retire_later(k["api_key_id"])
                moved.append(pooled["id"])
                continue
            # A key may be in only one usage plan of the stage: out of the old one, then into the new
            # (put back if that fails, so the key keeps working).
            try:
                apigw.delete_usage_plan_key(usagePlanId=PLANS[k["plan"]], keyId=k["api_key_id"])
            except apigw.exceptions.NotFoundException:
                pass
            try:
                apigw.create_usage_plan_key(usagePlanId=PLANS["standard"], keyId=k["api_key_id"], keyType="API_KEY")
            except Exception:
                apigw.create_usage_plan_key(usagePlanId=PLANS[k["plan"]], keyId=k["api_key_id"], keyType="API_KEY")
                raise
            tenants.update_item(Key={"pk": k["pk"]}, UpdateExpression="SET #p = :s",
                                ExpressionAttributeNames={"#p": "plan"}, ExpressionAttributeValues={":s": "standard"})
            moved.append(k["api_key_id"])
    tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET #p = :s, upgraded_at = :n REMOVE trial_ends_at, daily_cap_bytes",
                        ExpressionAttributeNames={"#p": "plan"}, ExpressionAttributeValues={":s": "standard", ":n": _iso(_now())})
    if moved:
        _invoke_self({"action": "refill-keys"})
    return {"tenant": tenant, "plan": "standard", "was": rec.get("plan"), "keys_moved": moved}


def extend_trial(tenant, days=7, context=None):
    _active(tenant)
    try:
        days = float(days)
    except (TypeError, ValueError):
        raise Refused("days: how many days from now the trial ends")
    if not 0 < days <= 90:
        raise Refused("days: how many days from now the trial ends (up to 90)")
    end = _iso(_now() + timedelta(days=days))
    tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET trial_ends_at = :t",
                        ExpressionAttributeValues={":t": end})
    return {"tenant": tenant, "trial_ends_at": end}


def set_billing(tenant, billing_status, customer=None, subscription=None, new=False, context=None):
    """From Stripe's events (obs-account-api's webhook). new: a checkout just started this subscription,
    which becomes the tenant's; otherwise an event of an older subscription is ignored. A billed
    status (active, trialing, past_due) upgrades a trial; canceled or unpaid makes ingest refuse data."""
    rec = _active(tenant)
    current = rec.get("stripe_subscription_id")
    if subscription and current and subscription != current and not new:
        return {"tenant": tenant, "ignored": f"not the current subscription ({current})"}
    sets, values = ["billing_status = :s", "billing_updated_at = :n"], {":s": billing_status, ":n": _iso(_now())}
    if customer:
        sets.append("stripe_customer_id = :c")
        values[":c"] = customer
    if subscription:
        sets.append("stripe_subscription_id = :sub")
        values[":sub"] = subscription
    if new or not rec.get("billing_started_at"):
        sets.append("billing_started_at = :n")
    tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET " + ", ".join(sets),
                        ExpressionAttributeValues=values)
    out = {"tenant": tenant, "billing_status": billing_status}
    if billing_status in billing.BILLED_STATUSES and (rec.get("plan") != "standard" or rec.get("trial_ends_at")):
        out["upgraded"] = upgrade(tenant)
    return out


def report_usage(day=None, days_back=3, context=None):
    """Scheduled daily: each subscribed tenant's usage of each of the last days_back days (default:
    yesterday and the 2 before, so a failed day is retried) not reported yet, to Stripe's meters.
    A day is marked billed#<tenant>#<day> once Stripe has every event of it."""
    if not billing.settings():
        return {"skipped": "billing isn't set up (SSM /obs/stripe/*)"}
    today = _now().date()
    days = [day] if day else [(today - timedelta(days=i)).isoformat() for i in range(int(days_back), 0, -1)]
    subscribed, kw = [], dict(FilterExpression=Attr("pk").begins_with("tenant#") & Attr("stripe_customer_id").exists())
    while True:
        page = tenants.scan(**kw)
        subscribed += [it for it in page["Items"] if it.get("billing_status") in billing.BILLED_STATUSES]
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    reported, failed = {}, {}
    for rec in subscribed:
        tenant = rec["tenant"]
        for d in days:
            if d < (rec.get("billing_started_at") or "")[:10] and not day:
                continue    # before the subscription: those days were the free trial
            if tenants.get_item(Key={"pk": f"billed#{tenant}#{d}"}).get("Item"):
                continue
            m = tenants.get_item(Key={"pk": f"meter#{tenant}#{d}"}).get("Item") or {}
            u, ukw = [], dict(KeyConditionExpression=Key("tenant").eq(tenant) & Key("sk").between(d, d + "#~"))
            while True:
                page = usage_t.query(**ukw)
                u += page["Items"]
                if "LastEvaluatedKey" not in page:
                    break
                ukw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
            try:
                sent = billing.report(tenant, rec["stripe_customer_id"], d, billing.day_usage(m, u))
            except billing.BillingError as e:
                failed.setdefault(tenant, {})[d] = str(e)
                continue
            tenants.put_item(Item={"pk": f"billed#{tenant}#{d}", "tenant": tenant, "day": d,
                                   "reported_at": _iso(_now()), "usage": sent})
            reported.setdefault(tenant, {})[d] = sent
    if failed:
        print(json.dumps({"billing": "report failed", "failed": failed}))
    return {"days": days, "tenants": len(subscribed), "reported": reported, "failed": failed}


def set_search(tenant, units_per_day=None, context=None):
    _active(tenant)
    if units_per_day is None:
        tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="REMOVE search_units_per_day")
    else:
        try:
            units = int(units_per_day)
        except (TypeError, ValueError):
            raise Refused("units_per_day: a whole number of search units a day (0: no limit)")
        if units < 0:
            raise Refused("units_per_day: a whole number of search units a day (0: no limit)")
        tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET search_units_per_day = :u",
                            ExpressionAttributeValues={":u": units})
    return {"tenant": tenant, "searches": _searches(_record(tenant), tenant)}


def _searches(rec, tenant):
    """Today's search units used, and the allowance (None: no limit). Takes effect in obs-query-api
    within 5 minutes."""
    allowance = rec.get("search_units_per_day")
    allowance = int(allowance) if allowance is not None else PLAN_SEARCH_UNITS.get(rec.get("plan", "standard"), PLAN_SEARCH_UNITS["standard"])
    used = tenants.get_item(Key={"pk": f"usage#search#{tenant}#{_now():%Y-%m-%d}"}).get("Item") or {}
    return {"units_today": int(used.get("n", 0)), "units_per_day": allowance or None}


def rotate(tenant, grace_hours=24, scope="ingest", context=None):
    if scope not in SCOPES:
        raise Refused(f"unknown scope {scope!r}; one of {sorted(SCOPES)}")
    rec = _active(tenant)
    old = [k for k in _keys(tenant) if k["status"] == "active" and k.get("scope", "ingest") == scope]
    key, key_id = _issue_key(tenant, rec.get("plan", "standard"), scope)
    expires = _iso(_now() + timedelta(hours=float(grace_hours)))
    for k in old:
        tenants.update_item(Key={"pk": k["pk"]}, UpdateExpression="SET #s = :e, expires_at = :x",
                            ConditionExpression="#s = :a", ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":e": "expiring", ":x": expires, ":a": "active"})
    return {"tenant": tenant, "key_id": key_id, "api_key": key,
            "old_keys_expire_at": expires if old else None, "old_key_ids": [k["api_key_id"] for k in old]}


def read_key(tenant, context=None):
    rec = _active(tenant)
    key, key_id = _issue_key(tenant, rec.get("plan", "standard"), "read")
    return {"tenant": tenant, "scope": "read", "key_id": key_id, "api_key": key}


def revoke(tenant, key_id=None, context=None):
    keys = [k for k in _keys(tenant) if k["status"] in ("active", "expiring")
            and (key_id is None or k["api_key_id"] == key_id)]
    if key_id and not keys:
        raise Refused(f"no live key {key_id} for {tenant}")
    for k in keys:
        _revoke_key(k)
    return {"tenant": tenant, "revoked": [k["api_key_id"] for k in keys]}


def delete(tenant, context=None):
    rec = _record(tenant)
    legacy = rec is None and _keys(tenant)  # created before T5: keys but no tenant record
    if rec is None and not legacy:
        raise Refused(f"no tenant {tenant}")
    if rec and rec["status"] == "deleted":
        return {"tenant": tenant, "status": "deleted"}
    tenants.put_item(Item={**(rec or {"pk": f"tenant#{tenant}", "tenant": tenant, "plan": "standard"}),
                           "status": "deleting", "delete_requested_at": _iso(_now()), "passes": 0,
                           # if the first pass fails, the sweep retries it after this
                           "next_pass_at": _iso(_now() + SETTLE)})
    revoked = [k["api_key_id"] for k in _keys(tenant) if k["status"] in ("active", "expiring")]
    for k in _keys(tenant):
        _revoke_key(k, delete_from_gateway=True)
    if rec and rec.get("stripe_subscription_id") and rec.get("billing_status") in billing.BILLED_STATUSES:
        try:    # stop billing now: the usage so far is invoiced
            billing.stripe("DELETE", f"/subscriptions/{rec['stripe_subscription_id']}", {"invoice_now": True})
        except billing.BillingError as e:
            print(json.dumps({"billing": "subscription not cancelled; cancel it in Stripe", "tenant": tenant, "error": str(e)}))
    for sig in SIGNALS:
        try:
            firehose.delete_delivery_stream(DeliveryStreamName=f"obs-t-{tenant}-{sig}")
        except firehose.exceptions.ResourceNotFoundException:
            pass
    for u in _users(tenant):
        if u["status"] == "active":
            _remove_login(u)
    # Synthetic checks (S1): stop running them; and their excluded runs, maintenance windows, SLOs,
    # alert rules and channels (A1; an email channel's SNS topic too).
    for prefix in ("check#", "exclude#", "window#", "slo#", "alert#", "astate#", "channel#", "dash#", "meter#", "ai#", "drop#"):
        for c in _items(tenant, prefix):
            if c.get("topic_arn"):
                try:
                    sns.delete_topic(TopicArn=c["topic_arn"])
                except sns.exceptions.NotFoundException:
                    pass
            tenants.delete_item(Key={"pk": c["pk"]})
    _invoke_self({"action": "purge", "tenant": tenant})
    return {"tenant": tenant, "status": "deleting", "revoked": revoked,
            "note": f"data purge started; passes repeat every {int(SETTLE.total_seconds() // 60)} min "
                    "until one finds nothing"}


ROLES = {"owner", "member"}


def invite_user(tenant, email, send_email=True, role="owner", invited_by=None, welcome=False, context=None):
    """role: owners manage the tenant's users and API keys in the app; members use everything else.
    (Users invited before roles existed, by the platform, are owners.)"""
    if not USER_POOL:
        raise Refused("logins are not set up (no USER_POOL_ID; deploy obs-state, then obs-phaseT5)")
    rec_t = _active(tenant)
    email = _email(email)
    if role not in ROLES:
        raise Refused(f"unknown role {role!r}; one of {sorted(ROLES)}")
    rec = tenants.get_item(Key={"pk": f"user#{email}"}, ConsistentRead=True).get("Item")
    if rec and rec["status"] == "active":
        # A login belongs to exactly one tenant (its username is the email).
        raise Refused(f"{email} is already a user of {'this tenant' if rec['tenant'] == tenant else 'another tenant'}")
    ours = bool(send_email) and emails.enabled()       # our own email (SES), not Cognito's
    password = _temp_password()
    kw = {"MessageAction": "SUPPRESS", "TemporaryPassword": password} if ours or not send_email else {}
    try:
        cognito.admin_create_user(
            UserPoolId=USER_POOL, Username=email, DesiredDeliveryMediums=["EMAIL"],
            UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"},
                            {"Name": "custom:tenant", "Value": tenant}], **kw)
    except cognito.exceptions.UsernameExistsException:
        raise Refused(f"{email} already has a login")
    tenants.put_item(Item={"pk": f"user#{email}", "tenant": tenant, "status": "active", "email": email,
                           "role": role, "created_at": _iso(_now()), **({"invited_by": invited_by} if invited_by else {})})
    if ours:
        company = rec_t.get("company") or tenant
        if welcome:
            emails.welcome(email, password, company)
        else:
            emails.invitation(email, password, company, invited_by)
    return {"tenant": tenant, "email": email, "role": role, "status": "invited", "email_sent": bool(send_email)}


def _temp_password():
    """Meets the pool's policy (12+ characters, upper, lower, digit); easy to type."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(14))
        if any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p):
            return p


def remove_user(tenant, email, context=None):
    email = _email(email)
    rec = tenants.get_item(Key={"pk": f"user#{email}"}, ConsistentRead=True).get("Item")
    if not rec or rec["tenant"] != tenant or rec["status"] != "active":
        raise Refused(f"{email} is not a user of {tenant}")
    _remove_login(rec)
    return {"tenant": tenant, "email": email, "status": "removed"}


def users(tenant, context=None):
    return {"tenant": tenant, "users": sorted(({"email": u["email"], "status": u["status"], "role": u.get("role", "owner"),
                                                "created_at": u.get("created_at"), "invited_by": u.get("invited_by")}
                                               for u in _users(tenant)),
                                              key=lambda u: u["email"])}


def _remove_login(rec):
    """Sign the user out everywhere (refresh tokens stop working) and delete the login."""
    for call in (cognito.admin_user_global_sign_out, cognito.admin_delete_user):
        try:
            call(UserPoolId=USER_POOL, Username=rec["email"])
        except cognito.exceptions.UserNotFoundException:
            pass
    tenants.update_item(Key={"pk": rec["pk"]}, UpdateExpression="SET #s = :r, removed_at = :n",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":r": "removed", ":n": _iso(_now())})


def _email(email):
    email = str(email).strip().lower()
    if not _EMAIL.match(email):
        raise Refused(f"not an email address: {email!r}")
    return email


def tune(tenant, buffer_seconds, context=None):
    secs = _buffer_seconds(buffer_seconds)
    rec = _active(tenant)
    for sig in SIGNALS:
        name = f"obs-t-{tenant}-{sig}"
        d = firehose.describe_delivery_stream(DeliveryStreamName=name)["DeliveryStreamDescription"]
        dest = d["Destinations"][0]
        hints = dest["ExtendedS3DestinationDescription"]["BufferingHints"]
        firehose.update_destination(
            DeliveryStreamName=name, CurrentDeliveryStreamVersionId=d["VersionId"],
            DestinationId=dest["DestinationId"],
            ExtendedS3DestinationUpdate={"BufferingHints": {"SizeInMBs": hints["SizeInMBs"],
                                                            "IntervalInSeconds": secs}})
    if "pk" in rec:   # tenants created before T5 have no record
        tenants.update_item(Key={"pk": f"tenant#{tenant}"}, UpdateExpression="SET buffer_seconds = :s",
                            ExpressionAttributeValues={":s": secs})
    return {"tenant": tenant, "buffer_seconds": secs}


def _buffer_seconds(v):
    secs = int(v)
    if not 0 <= secs <= 900:   # Firehose's range
        raise Refused(f"buffer_seconds must be 0-900, not {v!r}")
    return secs


def status(tenant, context=None):
    rec = _record(tenant) or {"status": "unknown (no tenant record)"}
    keys = [{k: v for k, v in key.items() if k not in ("pk", "gateway_key")} for key in _keys(tenant)]
    meter = tenants.get_item(Key={"pk": f"meter#{tenant}#{_now():%Y-%m-%d}"}).get("Item") or {}
    return {"tenant": tenant, **{k: v for k, v in rec.items() if k not in ("pk", "tenant")},
            "today": {"bytes": int(meter.get("bytes", 0)), "records": int(meter.get("records", 0)),
                      "refused_bytes": int(meter.get("refused_bytes", 0)),
                      **{k: int(v) for k, v in meter.items() if k.startswith(("in_", "dropped_"))}},
            "searches": _searches(rec, tenant),
            "keys": sorted(keys, key=lambda k: k.get("created_at", ""))}


def usage(tenant, start=None, end=None, context=None):
    """Per day (of arrival) and signal: records, raw bytes received (gzipped
    OTLP JSON, as Firehose delivered it) and Parquet bytes stored. Counted
    when compaction commits, so the current hour shows up about an hour later."""
    end = end or _now().strftime("%Y-%m-%d")
    start = start or (_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    days, kw = {}, dict(KeyConditionExpression=Key("tenant").eq(tenant) & Key("sk").between(start, end + "#~"))
    while True:
        page = usage_t.query(**kw)
        for it in page["Items"]:
            d = days.setdefault(it["dt"], {}).setdefault(it["signal"], {"records": 0, "raw_bytes": 0,
                                                                       "stored_bytes": 0, "files": 0})
            for f in d:
                d[f] += int(it.get(f, 0))
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    totals = {}
    for sigs in days.values():
        for sig, d in sigs.items():
            t = totals.setdefault(sig, dict.fromkeys(d, 0))
            for f in d:
                t[f] += d[f]
    return {"tenant": tenant, "start": start, "end": end, "days": dict(sorted(days.items())), "totals": totals}


def list_tenants(context=None):
    out, kw = [], dict(FilterExpression=Attr("pk").begins_with("tenant#"))
    while True:
        page = tenants.scan(**kw)
        out += [{"tenant": it["tenant"], "status": it["status"], "plan": it.get("plan"),
                 "created_at": it.get("created_at")} for it in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return {"tenants": sorted(out, key=lambda t: t["tenant"])}
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def sweep(context=None):
    """Scheduled. Refuse rotated keys whose grace has ended; start the next
    purge pass of deletions that are due; finish deletions whose last pass
    (after at least one settle interval) found nothing."""
    now = _iso(_now())
    expired, advanced, finished = [], [], []
    kw = dict(FilterExpression=Attr("status").is_in(["expiring", "deleting"]))
    while True:
        page = tenants.scan(**kw)
        for it in page["Items"]:
            if it["pk"].startswith("key#") and it["status"] == "expiring" and it["expires_at"] <= now:
                _revoke_key(it)
                expired.append(it["api_key_id"])
            elif it["pk"].startswith("tenant#") and it["status"] == "deleting":
                # Passes run SETTLE apart, so a clean pass after the first
                # means nothing was still being written.
                if int(it.get("passes", 0)) >= 2 and int(it.get("last_pass_deleted", 1)) == 0:
                    tenants.update_item(Key={"pk": it["pk"]}, UpdateExpression="SET #s = :d, deleted_at = :n",
                                        ExpressionAttributeNames={"#s": "status"},
                                        ExpressionAttributeValues={":d": "deleted", ":n": now})
                    finished.append(it["tenant"])
                elif it["next_pass_at"] <= now:
                    _invoke_self({"action": "purge", "tenant": it["tenant"]})
                    advanced.append(it["tenant"])
        if "LastEvaluatedKey" not in page:
            break
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    try:
        pools = refill_keys()
    except Exception as e:  # noqa: BLE001  a failed top-up mustn't stop the sweep
        pools = {"error": str(e)[:300]}
        print(json.dumps({"key_pool_refill_failed": pools["error"]}))
    return {"expired_keys": expired, "purging": advanced, "deleted": finished, "key_pools": pools}


def retention_cutoff(now=None, days=None):
    """The first event day kept (YYYY-MM-DD): today - RETENTION_DAYS, UTC. query.py uses the same."""
    now = now or _now()
    return (now - timedelta(days=RETENTION_DAYS if days is None else days)).strftime("%Y-%m-%d")


def retention(after=None, context=None):
    """Scheduled daily: delete each active tenant's data from before the cutoff day. Carries on
    in a new invocation (after=<last tenant done>) if it runs short of time."""
    cutoff = retention_cutoff()
    deadline = time.time() + (context.get_remaining_time_in_millis() / 1000 - 60 if context else 600)
    done, deleted = [], 0
    for t in sorted(list_tenants()["tenants"], key=lambda t: t["tenant"]):
        if t["status"] != "active" or (after and t["tenant"] <= after):
            continue
        if time.time() >= deadline:
            _invoke_self({"action": "retention", "after": done[-1] if done else after})
            return {"cutoff": cutoff, "tenants": done, "deleted": deleted, "continuing": True}
        deleted += _expire_tenant(t["tenant"], cutoff)
        done.append(t["tenant"])
    return {"cutoff": cutoff, "tenants": done, "deleted": deleted}


def _expire_tenant(tenant, cutoff):
    """Everything of one tenant from before the cutoff day -> number of objects and items deleted."""
    n, before = 0, f"{cutoff}T00:00:00"
    for signal in SIGNALS:
        # 1. Index entries of files whose rows are all older than the cutoff (then those files).
        services = index.get_item(Key={"pk": f"{tenant}#_services#{signal}", "sk": "all"}).get("Item", {}).get("services", set())
        files = []
        for service in services:
            kw = dict(KeyConditionExpression=Key("pk").eq(f"{tenant}#{signal}#{service}") & Key("sk").lt(before))
            while True:
                page = index.query(**kw)
                old = [it for it in page["Items"] if str(it.get("max_ts", "9")) < before]
                with index.batch_writer() as w:
                    for it in old:
                        w.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
                files += [it["file_path"].split(f"s3://{BUCKET}/", 1)[-1] for it in old if "file_path" in it]
                files += [it["bloom_s3_key"] for it in old if "bloom_s3_key" in it]
                n += len(old)
                if "LastEvaluatedKey" not in page:
                    break
                kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        for i in range(0, len(files), 1000):
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in files[i:i + 1000]], "Quiet": True})
        n += len(files)
        # 2. Whole days before the cutoff: files, filters, digests, raw arrivals.
        days = set()
        for base in (f"data/tenant={tenant}/{signal}/", f"data/tenant={tenant}/{signal}/_bloom/day/",
                     f"data/tenant={tenant}/{signal}/_bloom/hour/", f"data/tenant={tenant}/{signal}/_ids/",
                     f"_incoming/tenant={tenant}/{signal}/"):
            for day in _day_folders(base):
                if day < cutoff:
                    days.add(day)
                    n += _purge_prefix(f"{base}dt={day}/", time.time() + 600)[0]
        # 3. Index items kept per day or hour: sealed-filter state, compaction plans, raw-file lists.
        for pk, sk_before in ((f"{tenant}#_day#{signal}", cutoff), (f"{tenant}#_hour#{signal}", cutoff)):
            items = index.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").lt(sk_before))["Items"]
            with index.batch_writer() as w:
                for it in items:
                    w.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
            n += len(items)
        for day in sorted(days):
            for hour in range(24):
                for pk in (f"{tenant}#_plan#{signal}#{day}#{hour:02d}", f"{tenant}#_raw#{signal}#{day}#{hour:02d}"):
                    items = index.query(KeyConditionExpression=Key("pk").eq(pk), ProjectionExpression="pk, sk")["Items"]
                    with index.batch_writer() as w:
                        for it in items:
                            w.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
                    n += len(items)
    # Browser checks' screenshots expire by the bucket's lifecycle rule (30 days).
    return n


def _day_folders(prefix):
    """The YYYY-MM-DD of each dt=... folder directly under prefix."""
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{prefix}dt=", Delimiter="/"):
        out += [p["Prefix"][len(prefix) + 3:].rstrip("/") for p in page.get("CommonPrefixes", [])]
    return out


def restore(context=None):
    """Tenants, keys and streams outlive the compute stacks (infra/down.sh keeps them), but a
    recreated API has new usage plans, which hold none of the keys. Put every live key back in
    its plan and make sure every active tenant has its streams."""
    tenants_done, keys_added = [], []
    in_plan = {plan_id: {k["id"] for page in apigw.get_paginator("get_usage_plan_keys").paginate(usagePlanId=plan_id)
                         for k in page["items"]}
               for plan_id in PLANS.values()}
    for t in list_tenants()["tenants"]:
        if t["status"] != "active":
            continue
        rec = _record(t["tenant"])
        _provision_streams(t["tenant"], int(rec.get("buffer_seconds", BUFFER_SECONDS)))
        for k in _keys(t["tenant"]):
            if k["status"] not in ("active", "expiring"):
                continue
            plan_id = PLANS[k.get("plan", "standard")]
            if k["api_key_id"] not in in_plan[plan_id]:
                apigw.create_usage_plan_key(usagePlanId=plan_id, keyId=k["api_key_id"], keyType="API_KEY")
                in_plan[plan_id].add(k["api_key_id"])
                keys_added.append(k["api_key_id"])
        tenants_done.append(t["tenant"])
    for plan in KEY_POOL_PLANS:
        if plan not in PLANS:
            continue
        for k in _pool(plan).get("keys", []):
            if k["id"] not in in_plan[PLANS[plan]]:
                try:
                    apigw.create_usage_plan_key(usagePlanId=PLANS[plan], keyId=k["id"], keyType="API_KEY")
                    keys_added.append(k["id"])
                except apigw.exceptions.NotFoundException:
                    pass
    return {"tenants": tenants_done, "keys_added_to_plans": keys_added}


def purge(tenant, deleted=0, context=None):
    """One purge pass. Deletes until nothing is left or the invocation is
    nearly out of time, then re-invokes itself to carry on the same pass."""
    rec = _record(tenant)
    if not rec or rec["status"] != "deleting":
        raise Refused(f"{tenant} is not being deleted")
    deadline = time.time() + (context.get_remaining_time_in_millis() / 1000 - 60 if context else 600)
    done = True
    for prefix in (f"_incoming/tenant={tenant}/", f"_incoming/_errors/tenant={tenant}/", f"data/tenant={tenant}/",
                   f"synthetics/tenant={tenant}/",     # browser checks' screenshots
                   f"_ai/tenant={tenant}/"):           # AI SRE conversations
        n, finished = _purge_prefix(prefix, deadline)
        deleted += n
        done = done and finished
    if done:
        n, done = _purge_index(tenant, deadline)
        deleted += n
    if not done:
        _invoke_self({"action": "purge", "tenant": tenant, "deleted": deleted})
        return {"tenant": tenant, "deleted_so_far": deleted, "continuing": True}
    tenants.update_item(
        Key={"pk": f"tenant#{tenant}"},
        UpdateExpression="SET passes = if_not_exists(passes, :z) + :one, last_pass_deleted = :d, "
                         "last_pass_at = :n, next_pass_at = :next",
        ExpressionAttributeValues={":z": 0, ":one": 1, ":d": deleted, ":n": _iso(_now()),
                                   ":next": _iso(_now() + SETTLE)})
    return {"tenant": tenant, "deleted": deleted, "pass_complete": True}


def refill_keys(context=None):
    """Tops each plan's pool of gateway keys up to KEY_POOL_SIZE (they are usable once
    KEY_POOL_MIN_AGE old), and deletes replaced gateway keys whose time has come."""
    added = {}
    for plan in KEY_POOL_PLANS:
        if plan not in PLANS:
            continue
        missing = KEY_POOL_SIZE - len(_pool(plan).get("keys", []))
        for _ in range(max(0, missing)):
            value = "pool_" + "".join(secrets.choice(_KEY_CHARS) for _ in range(40))
            key_id = _gateway_key(f"pool-{plan}-{_now():%Y%m%dT%H%M%S}-{secrets.token_hex(2)}", value, plan, {"pool": plan})
            tenants.update_item(Key={"pk": f"keypool#{plan}"}, UpdateExpression="SET #k = list_append(if_not_exists(#k, :e), :n)",
                                ExpressionAttributeNames={"#k": "keys"},
                                ExpressionAttributeValues={":e": [], ":n": [{"id": key_id, "value": value, "created_at": _iso(_now())}]})
            added[plan] = added.get(plan, 0) + 1
    retired = []
    now = _iso(_now())
    for i, r in reversed(list(enumerate(_pool("retired").get("keys", [])))):
        if r["after"] <= now:
            try:
                apigw.delete_api_key(apiKey=r["id"])
            except apigw.exceptions.NotFoundException:
                pass
            try:
                tenants.update_item(Key={"pk": "keypool#retired"}, UpdateExpression=f"REMOVE #k[{i}]",
                                    ConditionExpression=f"#k[{i}].id = :id", ExpressionAttributeNames={"#k": "keys"},
                                    ExpressionAttributeValues={":id": r["id"]})
            except tenants.meta.client.exceptions.ConditionalCheckFailedException:
                pass
            retired.append(r["id"])
    return {"added": added, "retired": retired}


def _retire_later(key_id):
    tenants.update_item(Key={"pk": "keypool#retired"}, UpdateExpression="SET #k = list_append(if_not_exists(#k, :e), :n)",
                        ExpressionAttributeNames={"#k": "keys"},
                        ExpressionAttributeValues={":e": [], ":n": [{"id": key_id, "after": _iso(_now() + RETIRE_AFTER)}]})


ACTIONS = {"refill-keys": refill_keys, "create": create, "rotate": rotate, "read-key": read_key, "signup": signup, "add-key": add_key,
           "set-cap": set_cap, "set-search": set_search, "upgrade": upgrade, "extend-trial": extend_trial,
           "set-billing": set_billing, "report-usage": report_usage,
           "invite-user": invite_user, "remove-user": remove_user, "users": users, "revoke": revoke, "delete": delete, "tune": tune, "status": status,
           "usage": usage, "list": list_tenants, "sweep": sweep, "restore": restore, "purge": purge,
           "retention": retention}


# ------------------------------------------------------------------ helpers

def _active(tenant):
    rec = _record(tenant)
    if rec is None and _keys(tenant):
        return {"plan": "standard"}  # created before T5
    if not rec or rec["status"] != "active":
        raise Refused(f"{tenant} is not an active tenant")
    return rec


def _issue_key(tenant, plan, scope="ingest"):
    key = "obs_" + "".join(secrets.choice(_KEY_CHARS) for _ in range(40))
    item = {"pk": f"key#{key_hash(key)}", "tenant": tenant, "status": "active", "scope": scope,
            "plan": plan, "created_at": _iso(_now())}
    pooled = _pool_take(plan)
    if pooled:
        item.update(api_key_id=pooled["id"], gateway_key=pooled["value"])
        _invoke_self({"action": "refill-keys"})
    else:   # no aged gateway key: the key itself, refused by some requests for a few minutes
        print(json.dumps({"key_pool_empty": plan, "tenant": tenant}))
        item["api_key_id"] = _gateway_key(f"{tenant}-{_now():%Y%m%dT%H%M%S}-{secrets.token_hex(2)}", key, plan,
                                          {"tenant": tenant})
    tenants.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    return key, item["api_key_id"]


def _gateway_key(name, value, plan, tags=None):
    """An API Gateway key in the plan's usage plan -> its id."""
    key_id = apigw.create_api_key(name=name, value=value, enabled=True, tags={"project": "obs", **(tags or {})})["id"]
    try:
        apigw.create_usage_plan_key(usagePlanId=PLANS[plan], keyId=key_id, keyType="API_KEY")
    except Exception:
        apigw.delete_api_key(apiKey=key_id)
        raise
    return key_id


def _pool(plan):
    return tenants.get_item(Key={"pk": f"keypool#{plan}"}, ConsistentRead=True).get("Item") or {}


def _pool_take(plan):
    """The oldest gateway key of the plan's pool that is at least KEY_POOL_MIN_AGE old, removed from
    the pool ({id, value}), or None."""
    if plan not in KEY_POOL_PLANS or plan not in PLANS:
        return None
    cutoff = _iso(_now() - KEY_POOL_MIN_AGE)
    for _ in range(5):
        keys = _pool(plan).get("keys", [])
        ready = [(i, k) for i, k in enumerate(keys) if k["created_at"] <= cutoff]
        if not ready:
            return None
        i, k = min(ready, key=lambda ik: ik[1]["created_at"])
        try:   # taken by someone else in between: the condition fails, look again
            tenants.update_item(Key={"pk": f"keypool#{plan}"}, UpdateExpression=f"REMOVE #k[{i}]",
                                ConditionExpression=f"#k[{i}].id = :id", ExpressionAttributeNames={"#k": "keys"},
                                ExpressionAttributeValues={":id": k["id"]})
            return {"id": k["id"], "value": k["value"]}
        except tenants.meta.client.exceptions.ConditionalCheckFailedException:
            continue
    return None


def _revoke_key(k, delete_from_gateway=False):
    # The status change makes the authorizer refuse the key once API Gateway's
    # cached answer expires (up to a minute); disabling it in API Gateway
    # usually refuses it sooner.
    if k["status"] != "revoked":
        tenants.update_item(Key={"pk": k["pk"]}, UpdateExpression="SET #s = :r, revoked_at = :n",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":r": "revoked", ":n": _iso(_now())})
    try:
        if delete_from_gateway:
            apigw.delete_api_key(apiKey=k["api_key_id"])
        elif k["status"] != "revoked":
            apigw.update_api_key(apiKey=k["api_key_id"],
                                 patchOperations=[{"op": "replace", "path": "/enabled", "value": "false"}])
    except apigw.exceptions.NotFoundException:
        pass


def _provision_streams(tenant, buffer_seconds=BUFFER_SECONDS):
    """One Firehose stream per signal, obs-t-<tenant>-<signal>, delivering
    gzipped OTLP JSON to _incoming/tenant=<T>/<signal>/. Returns once all are
    ACTIVE (about a minute for new streams)."""
    for sig in SIGNALS:
        name = f"obs-t-{tenant}-{sig}"
        try:
            firehose.create_delivery_stream(
                DeliveryStreamName=name, DeliveryStreamType="DirectPut",
                ExtendedS3DestinationConfiguration={
                    "RoleARN": FIREHOSE_ROLE, "BucketARN": f"arn:aws:s3:::{BUCKET}",
                    "Prefix": f"_incoming/tenant={tenant}/{sig}/dt=!{{timestamp:yyyy-MM-dd}}/hour=!{{timestamp:HH}}/",
                    "ErrorOutputPrefix": f"_incoming/_errors/tenant={tenant}/{sig}/!{{firehose:error-output-type}}"
                                         "/dt=!{timestamp:yyyy-MM-dd}/",
                    "BufferingHints": {"SizeInMBs": 64, "IntervalInSeconds": buffer_seconds},
                    # Ingest gzips each record before sending (Firehose bills received
                    # bytes), so the stream passes them through: its objects are gzip
                    # members back to back, a valid .gz file.
                    "CompressionFormat": "UNCOMPRESSED", "FileExtension": ".json.gz",
                },
                Tags=[{"Key": "tenant", "Value": tenant}, {"Key": "project", "Value": "obs"}])
        except firehose.exceptions.ResourceInUseException:
            pass  # already exists
    for sig in SIGNALS:
        for _ in range(60):
            st = firehose.describe_delivery_stream(DeliveryStreamName=f"obs-t-{tenant}-{sig}")
            if st["DeliveryStreamDescription"]["DeliveryStreamStatus"] == "ACTIVE":
                break
            time.sleep(5)
        else:
            raise Refused(f"stream obs-t-{tenant}-{sig} not active after 5 minutes")


def _purge_prefix(prefix, deadline):
    deleted = 0
    while time.time() < deadline:
        page = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=1000)
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if not keys:
            return deleted, True
        resp = s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys, "Quiet": True})
        if resp.get("Errors"):
            raise RuntimeError(f"delete failed: {resp['Errors'][:3]}")
        deleted += len(keys)
    return deleted, False


def _purge_index(tenant, deadline):
    """Every obs-index item of the tenant. A scan, since the tenant's keys
    span many partitions (per service, plan hour, raw hour); deletions are
    rare and a scan costs cents even for a large index."""
    deleted = 0
    cond = Attr("pk").begins_with(f"{tenant}#") | Attr("pk").begins_with(f"_lease#{tenant}#")
    kw = dict(FilterExpression=cond, ProjectionExpression="pk, sk")
    while True:
        page = index.scan(**kw)
        with index.batch_writer() as w:
            for it in page["Items"]:
                w.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
        deleted += len(page["Items"])
        if "LastEvaluatedKey" not in page:
            return deleted, True
        if time.time() >= deadline:
            return deleted, False  # the next invocation rescans from the start
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _invoke_self(payload):
    lam.invoke(FunctionName=SELF, InvocationType="Event", Payload=json.dumps(payload).encode())
