"""The ports on AWS (the only module here that imports boto3).

AwsStore
  the investigation   S3 _investigations/tenant=<T>/<id>.json (the whole record: trigger, events, result)
  its index row       obs-tenants inv#<T>#<id>: status, title, source, times, version (listing, and the
                      optimistic lock: a save names the version it read)
  dedupe claim        invdedupe#<T>#<sha256(key)>: inv_id, until `expires` (DynamoDB's TTL attribute)
  hourly quota        rate#inv#<T>#<YYYY-MM-DDTHH>: n
  webhook token       invhook#<sha256(token)>: tenant (the token itself is never stored);
                      invhookof#<T>: the tenant's current token hash, so a new token retires the old one
Every item carries `tenant`, so deleting a tenant removes them (by-tenant index); the S3 prefix is
purged with the tenant's data.

SqsQueue  one standard SQS queue (at-least-once: the workflow is idempotent).
"""

import hashlib
import json
import secrets

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

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


class SqsQueue:
    def __init__(self, url, sqs=None):
        self.url, self.sqs = url, sqs or boto3.client("sqs")

    def send(self, message):
        self.sqs.send_message(QueueUrl=self.url, MessageBody=json.dumps(message))
