#!/usr/bin/env python3
"""Applies two settings of infra/state.yaml to the live tables and sign-in client, whichever stack
owns them (accounts not yet moved to obs-state keep them in older stacks). Safe to re-run.

  1. obs-tenants: time to live on "expires" (per-minute query counters expire on their own).
  2. The web app's sign-in client: users can't change their own email (WriteAttributes [name]),
     and sign-in tokens last 15 minutes (renewed by the app; someone removed from a team loses
     access within 15 minutes). The client is updated with all of its current settings (an
     update resets any it leaves out).

Usage: AWS_DEFAULT_REGION=us-east-1 infra/state-settings.py
"""
import boto3

ddb = boto3.client("dynamodb")
ttl = ddb.describe_time_to_live(TableName="obs-tenants")["TimeToLiveDescription"]
if ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING") and ttl.get("AttributeName") == "expires":
    print("obs-tenants: time to live on 'expires' already on")
else:
    ddb.update_time_to_live(TableName="obs-tenants", TimeToLiveSpecification={"AttributeName": "expires", "Enabled": True})
    print("obs-tenants: time to live on 'expires' turned on")

idp = boto3.client("cognito-idp")
pool = next(p for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60)
            for p in page["UserPools"] if p["Name"] == "obs-users")
client = next(c for page in idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool["Id"], MaxResults=60)
              for c in page["UserPoolClients"] if c["ClientName"] == "obs-app")
cfg = idp.describe_user_pool_client(UserPoolId=pool["Id"], ClientId=client["ClientId"])["UserPoolClient"]
WANT = {"WriteAttributes": ["name"], "IdTokenValidity": 15, "AccessTokenValidity": 15}
units = cfg.get("TokenValidityUnits") or {}
if all(cfg.get(k) == v for k, v in WANT.items()) and units.get("IdToken") == units.get("AccessToken") == "minutes":
    print("obs-app: already set (email not changeable, 15-minute tokens)")
else:
    keep = {k: v for k, v in cfg.items() if k not in ("ClientSecret", "LastModifiedDate", "CreationDate", *WANT)}
    keep["TokenValidityUnits"] = {**units, "IdToken": "minutes", "AccessToken": "minutes"}
    idp.update_user_pool_client(**keep, **WANT)
    after = idp.describe_user_pool_client(UserPoolId=pool["Id"], ClientId=client["ClientId"])["UserPoolClient"]
    changed = sorted(k for k in set(cfg) | set(after)
                     if k not in ("LastModifiedDate", "TokenValidityUnits", *WANT) and cfg.get(k) != after.get(k))
    print("obs-app: users can no longer change their email; tokens last 15 minutes"
          + (f" (unexpectedly also changed: {changed})" if changed else ""))
