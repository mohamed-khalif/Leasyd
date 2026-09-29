"""What a synthetic check may reach, and what it may show. Shared by the HTTP runner
(synthetics.py) and the browser runner (browser.py), which has nothing else from the platform.

- Only public addresses: a URL's host is resolved and refused if any of its addresses is private,
  loopback, link-local (169.254.169.254), carrier-grade NAT, multicast or reserved. The caller then
  connects to an address it got from resolve(), so a name can't be re-resolved to somewhere else.
- Secrets and extracted values are masked ("••••") in anything shown or recorded.
"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,39})\}")
MASK = "••••"


class Refused(ValueError):
    """A request we won't carry out (bad input, or an address that isn't public)."""


def target(url):
    """(scheme, host, port, path) of a URL a check may request; raises Refused."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise Refused("url: not a valid URL")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Refused("url: must start with http:// or https://")
    if u.username or u.password:
        raise Refused("url: credentials in the URL are not allowed; use authentication")
    host = host_ok(u.hostname)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return u.scheme, host, port or (443 if u.scheme == "https" else 80), path


def host_ok(host):
    """host, lowercased, if it may be requested before DNS (names are checked after); raises Refused."""
    host = host.lower().rstrip(".").strip("[]")
    if host == "localhost" or host.endswith((".localhost", ".internal", ".local")):
        raise Refused("url: must be a public address")
    try:   # an IP literal must be public too (names are checked after DNS)
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not public(literal):
        raise Refused("url: must be a public address")
    return host


def public(ip):
    """Only globally routable unicast addresses (not private, loopback, link-local such as the
    169.254.169.254 metadata address, carrier-grade NAT, multicast or reserved)."""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host, port):
    """The addresses to use for host, refusing it if any of them isn't public (a name that also
    points inside must not be usable to reach inside)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Refused(f"could not resolve {host}")
    ips = list(dict.fromkeys(i[4][0] for i in infos))
    for ip in ips:
        if not public(ipaddress.ip_address(ip.split("%")[0])):
            raise Refused(f"{host} resolves to a non-public address")
    return ips


def sub(text, values):
    """{name} -> its value (unknown names stay as they are)."""
    return PLACEHOLDER.sub(lambda m: str(values[m[1]]) if m[1] in values else m[0], text)


def mask(text, masked):
    for v in sorted(masked, key=len, reverse=True):
        if v:
            text = text.replace(v, MASK)
    return text
