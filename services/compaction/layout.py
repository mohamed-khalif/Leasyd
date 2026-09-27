"""Where each tenant's data and index entries live. The only place these
formats are spelled out, so compaction, lookups and queries can't drift.

S3
  _incoming/tenant=<T>/<signal>/dt=<D>/hour=<H>/...            raw, from Firehose
  data/tenant=<T>/<signal>/dt=<D>/hour=<H>/service=<S>/...     compacted Parquet
  data/tenant=<T>/<signal>/_bloom/...                          bloom filters too big for the index

obs-index partition keys
  <T>#<signal>#<service>          one item per Parquet file (kind=parquet) and
                                  per (service, event hour) of a raw file not yet
                                  compacted (kind=raw, the fast lane)
  <T>#_services#<signal>          services seen, for lookups across all of them
  <T>#_plan#<signal>#<D>#<H>      compaction chunk plans for arrival hour D/H. Lookups
                                  read them to decide raw vs Parquet visibility.
  <T>#_raw#<signal>#<D>#<H>       one item per raw file: the index items it produced,
                                  so compaction can retire them
  _lease#...                      leases (internal)

Everything a tenant may read starts with "data/tenant=<T>/",
"_incoming/tenant=<T>/" or "<T>#"; the obs-tenant-reader IAM role allows
exactly those prefixes for its session's tenant tag.
"""

import re

_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")


def check_tenant(tenant):
    """3-40 chars of a-z, 0-9 and '-': safe in S3 keys, index keys and IAM tags."""
    if not isinstance(tenant, str) or not _TENANT.match(tenant):
        raise ValueError(f"invalid tenant id {tenant!r}")
    return tenant


def incoming_prefix(tenant, signal, dt=None, hour=None):
    p = f"_incoming/tenant={tenant}/{signal}/"
    if dt is not None:
        p += f"dt={dt}/"
        if hour is not None:
            p += f"hour={hour}/"
    return p


def data_prefix(tenant, signal):
    return f"data/tenant={tenant}/{signal}/"


def index_pk(tenant, signal, service):
    return f"{tenant}#{signal}#{service}"


def services_pk(tenant, signal):
    return f"{tenant}#_services#{signal}"


def plan_pk(tenant, signal, dt, hour):
    return f"{tenant}#_plan#{signal}#{dt}#{hour}"


def raw_files_pk(tenant, signal, dt, hour):
    return f"{tenant}#_raw#{signal}#{dt}#{hour}"


_INCOMING_KEY = re.compile(
    r"^_incoming/tenant=(?P<tenant>[a-z0-9][a-z0-9-]{1,38}[a-z0-9])/(?P<signal>[a-z]+)/"
    r"dt=(?P<dt>\d{4}-\d{2}-\d{2})/hour=(?P<hour>\d{2})/[^/]+$")


def parse_incoming_key(key):
    """(tenant, signal, dt, hour) for a raw file key, or None for anything else
    (e.g. _incoming/_errors/...)."""
    m = _INCOMING_KEY.match(key)
    return (m["tenant"], m["signal"], m["dt"], m["hour"]) if m else None


def worker_lease_pk(tenant, signal, dt, hour, batch_id):
    return f"_lease#{tenant}#{signal}#{dt}#{hour}#{batch_id}"
