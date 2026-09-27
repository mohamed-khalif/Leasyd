"""Where each tenant's data and index entries live. The only place these
formats are spelled out, so compaction, lookups and queries can't drift.

S3
  _incoming/tenant=<T>/<signal>/dt=<D>/hour=<H>/...            raw, from the collector
  data/tenant=<T>/<signal>/dt=<D>/hour=<H>/service=<S>/...     compacted Parquet
  data/tenant=<T>/<signal>/_bloom/...                          bloom filters too big for the index

obs-index partition keys
  <T>#<signal>#<service>        one item per Parquet file
  <T>#_services#<signal>        services seen, for lookups across all of them
  _plan#<T>#<signal>#<D>#<H>    compaction chunk plans (internal)
  _lease#...                    leases (internal)

Everything a tenant may read starts with "data/tenant=<T>/" or "<T>#"; the
obs-tenant-reader IAM role allows exactly those prefixes for its session's
tenant tag.
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
    return f"_plan#{tenant}#{signal}#{dt}#{hour}"


def worker_lease_pk(tenant, signal, dt, hour, batch_id):
    return f"_lease#{tenant}#{signal}#{dt}#{hour}#{batch_id}"
