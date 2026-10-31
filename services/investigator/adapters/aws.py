"""The ports on AWS (the only module here that imports boto3).

AwsStore
  the investigation   S3 _investigations/tenant=<T>/<id>.json (the whole record: trigger, events, result)
  its index row       obs-tenants inv#<T>#<id>: status, title, source, times, version (listing, and the
                      optimistic lock: a save names the version it read)
  dedupe claim        invdedupe#<T>#<sha256(key)>: inv_id, until `expires` (DynamoDB's TTL attribute)
  hourly quota        rate#inv#<T>#<YYYY-MM-DDTHH>: n
  webhook token       invhook#<sha256(token)>: tenant (the token itself is never stored);
                      invhookof#<T>: the tenant's current token hash, so a new token retires the old one
  deploys             deploy#<T>#<deployed_at>#<id>: the deploy; kept 90 days (`expires`); read by time
                      range through the by-tenant index (its sort key is pk)
  feedback links      invfb#<sha256(token)>: tenant, inv_id, until `expires`; `used_at` once answered
                      (single use: the answer is a conditional write)

AwsChannels  the alert channels (obs-alerts' channel#<T>#<id>, URLs and secrets decrypted with the
             checks' KMS key under {tenant, channel}), the firing rule (alert#<T>#<id>), the tenant's
             investigation settings (invsettings#<T>) and the once-an-hour skipped notice
             (rate#invskip#<T>#<hour>).
Every item carries `tenant`, so deleting a tenant removes them (by-tenant index); the S3 prefix is
purged with the tenant's data.

SqsQueue  one standard SQS queue (at-least-once: the workflow is idempotent).
"""

import base64
import hashlib
import json
import secrets
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from domain.deploys import KEEP_DAYS, Deploy
from domain.model import Investigation
from ports import StaleWrite


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _conditional_failed(e):
    return e.response["Error"]["Code"] == "ConditionalCheckFailedException"


class AwsStore:
    def __init__(self, table, bucket, s3=None, ddb=None):
        self.table = (ddb or boto3.resource("dynamodb")).Table(table)
        self.bucket, self.s3 = bucket, s3 or boto3.client("s3")

    @staticmethod
    def _key(tenant, inv_id):
        return f"_investigations/tenant={tenant}/{inv_id}.json"

    def _row(self, inv):
        t = inv.trigger
        return {"pk": f"inv#{inv.tenant}#{inv.id}", "tenant": inv.tenant, "status": inv.status, "title": t.title,
                "source": t.source, "service": t.service, "severity": t.severity, "created_at": inv.created_at,
                "updated_at": inv.updated_at, "confidence": str(inv.result.confidence) if inv.result else None,
                "version": inv.version}

    def _put_body(self, inv):
        self.s3.put_object(Bucket=self.bucket, Key=self._key(inv.tenant, inv.id), ContentType="application/json",
                           Body=json.dumps(inv.to_dict()).encode())

    def get(self, tenant, inv_id):
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=self._key(tenant, inv_id))["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        inv = Investigation.from_dict(json.loads(body))
        return inv if inv.tenant == tenant else None

    def create(self, inv):
        inv.version = 1
        try:
            self.table.put_item(Item={k: v for k, v in self._row(inv).items() if v is not None},
                                ConditionExpression="attribute_not_exists(pk)")
        except ClientError as e:
            if _conditional_failed(e):
                raise StaleWrite(f"investigation {inv.id} exists")
            raise
        self._put_body(inv)

    def save(self, inv):
        row = {k: v for k, v in self._row(inv).items() if k not in ("pk", "version")}
        sets = ", ".join(f"#{k} = :{k}" for k, v in row.items() if v is not None)
        try:
            self.table.update_item(
                Key={"pk": f"inv#{inv.tenant}#{inv.id}"}, UpdateExpression=f"SET {sets}, version = :next",
                ConditionExpression="version = :read",
                ExpressionAttributeNames={f"#{k}": k for k, v in row.items() if v is not None},
                ExpressionAttributeValues={**{f":{k}": v for k, v in row.items() if v is not None},
                                           ":read": inv.version, ":next": inv.version + 1})
        except ClientError as e:
            if _conditional_failed(e):
                raise StaleWrite(f"investigation {inv.id} changed since it was read")
            raise
        inv.version += 1
        self._put_body(inv)

    def claim_dedupe(self, tenant, key, inv_id, now_epoch, until_epoch):
        pk = f"invdedupe#{tenant}#{_h(key)[:40]}"
        try:
            self.table.put_item(Item={"pk": pk, "tenant": tenant, "inv_id": inv_id, "expires": until_epoch},
                                ConditionExpression="attribute_not_exists(pk) OR expires < :now",
                                ExpressionAttributeValues={":now": now_epoch})
            return None
        except ClientError as e:
            if not _conditional_failed(e):
                raise
        return (self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item") or {}).get("inv_id")

    def take_quota(self, tenant, hour, limit, until_epoch):
        try:
            self.table.update_item(Key={"pk": f"rate#inv#{tenant}#{hour}"}, UpdateExpression="ADD n :one SET tenant = :t, expires = :e",
                                   ConditionExpression="attribute_not_exists(n) OR n < :max",
                                   ExpressionAttributeValues={":one": 1, ":max": limit, ":t": tenant, ":e": until_epoch})
            return True
        except ClientError as e:
            if _conditional_failed(e):
                return False
            raise

    def tenant_of_webhook(self, token_hash):
        item = self.table.get_item(Key={"pk": f"invhook#{token_hash}"}).get("Item") or {}
        return item.get("tenant")

    def new_webhook_token(self, tenant, by, now_iso):
        """A new token for the tenant's webhook URL (shown once); the previous one stops working."""
        token = secrets.token_urlsafe(24)
        old = (self.table.get_item(Key={"pk": f"invhookof#{tenant}"}).get("Item") or {}).get("hash")
        self.table.put_item(Item={"pk": f"invhook#{_h(token)}", "tenant": tenant, "created_by": by, "created_at": now_iso})
        self.table.put_item(Item={"pk": f"invhookof#{tenant}", "tenant": tenant, "hash": _h(token), "created_at": now_iso})
        if old:
            self.table.delete_item(Key={"pk": f"invhook#{old}"})
        return token

    def list(self, tenant, limit=50):
        items, kw = [], dict(IndexName="by-tenant", KeyConditionExpression=Key("tenant").eq(tenant) & Key("pk").begins_with(f"inv#{tenant}#"))
        while True:
            page = self.table.query(**kw)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                break
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
        return [{"id": i["pk"].rsplit("#", 1)[1], **{k: (float(v) if k == "confidence" else v) for k, v in i.items()
                                                     if k not in ("pk", "tenant", "version")}} for i in items[:limit]]


    def put_deploy(self, tenant, d: Deploy, now_epoch):
        """Records a deploy; the same deploy posted again (same id and time) replaces itself."""
        self.table.put_item(Item={"pk": f"deploy#{tenant}#{d.deployed_at}#{d.id}", "tenant": tenant, **d.to_dict(),
                                  "expires": int(now_epoch) + KEEP_DAYS * 86400})

    def deploys(self, tenant, start_iso, end_iso, service=None, limit=200):
        """The tenant's recorded deploys between two ISO times, oldest first."""
        lo, hi = f"deploy#{tenant}#{start_iso}", f"deploy#{tenant}#{end_iso}~"
        items, kw = [], dict(IndexName="by-tenant", KeyConditionExpression=Key("tenant").eq(tenant) & Key("pk").between(lo, hi))
        while len(items) < 2000:
            page = self.table.query(**kw)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                break
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        out = [{k: v for k, v in i.items() if k not in ("pk", "tenant", "expires")} for i in items
               if not service or i.get("service", "").lower() == service.lower()]
        return sorted(out, key=lambda d: d["deployed_at"])[-limit:]


    def issue_feedback(self, tenant, inv_id, now_epoch, ttl_s):
        """-> a new feedback token for the investigation (only its hash is stored)."""
        token = secrets.token_urlsafe(24)
        self.table.put_item(Item={"pk": f"invfb#{_h(token)}", "tenant": tenant, "inv_id": inv_id, "expires": int(now_epoch + ttl_s)})
        return token

    def feedback_token(self, token, now_epoch, use=False):
        """-> {tenant, inv_id} while the token is unused and unexpired, else None. use=True marks it used
        (once: a second use, even at the same moment, gets None)."""
        pk = f"invfb#{_h(token)}"
        if not use:
            it = self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
            ok = it and "used_at" not in it and int(it["expires"]) > now_epoch
            return {"tenant": it["tenant"], "inv_id": it["inv_id"]} if ok else None
        try:
            it = self.table.update_item(Key={"pk": pk}, UpdateExpression="SET used_at = :now",
                                        ConditionExpression="attribute_exists(pk) AND attribute_not_exists(used_at) AND expires > :now",
                                        ExpressionAttributeValues={":now": int(now_epoch)}, ReturnValues="ALL_NEW")["Attributes"]
        except ClientError as e:
            if _conditional_failed(e):
                return None
            raise
        return {"tenant": it["tenant"], "inv_id": it["inv_id"]}


class AwsChannels:
    def __init__(self, table, kms_key, ddb=None, kms=None):
        self.table = (ddb or boto3.resource("dynamodb")).Table(table)
        self.kms_key, self.kms = kms_key, kms or boto3.client("kms")

    def _item(self, pk, tenant):
        it = self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
        return it if it and it.get("tenant") == tenant else None

    def get(self, tenant, channel_id):
        it = self._item(f"channel#{tenant}#{channel_id}", tenant)
        if not it:
            return None
        plain = {k: self.kms.decrypt(CiphertextBlob=base64.b64decode(v),
                                     EncryptionContext={"tenant": tenant, "channel": channel_id})["Plaintext"].decode()
                 for k, v in (it.get("secrets") or {}).items()}
        return {**{k: v for k, v in it.items() if k not in ("pk", "secrets")}, "id": channel_id, **plain}

    def exists(self, tenant, channel_id):
        return self._item(f"channel#{tenant}#{channel_id}", tenant) is not None

    def settings(self, tenant):
        it = self._item(f"invsettings#{tenant}", tenant) or {}
        return {k: it[k] for k in ("channels", "services", "fallback") if it.get(k)}

    def save_settings(self, tenant, settings, by, now_iso):
        self.table.put_item(Item={"pk": f"invsettings#{tenant}", "tenant": tenant, **settings, "updated_by": by, "updated_at": now_iso})

    def rule(self, tenant, rule_id):
        return self._item(f"alert#{tenant}#{rule_id}", tenant)

    def first_skip_notice(self, tenant, hour):
        try:
            self.table.put_item(Item={"pk": f"rate#invskip#{tenant}#{hour}", "tenant": tenant,
                                      "expires": int(datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).timestamp()) + 3 * 3600},
                                ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as e:
            if _conditional_failed(e):
                return False
            raise


class TenantDeploys:
    """One tenant's recorded deploys (the agent's Deploys port; the tenant is fixed here)."""

    def __init__(self, store, tenant):
        self.store, self.tenant = store, tenant

    def between(self, start_iso, end_iso, service=None):
        return self.store.deploys(self.tenant, start_iso, end_iso, service)


class SqsQueue:
    def __init__(self, url, sqs=None):
        self.url, self.sqs = url, sqs or boto3.client("sqs")

    def send(self, message):
        self.sqs.send_message(QueueUrl=self.url, MessageBody=json.dumps(message))
