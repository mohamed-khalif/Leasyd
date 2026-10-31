"""POSTs to customers' channels (Slack, Teams, webhooks): public https hosts only, at the address that
was checked after DNS (services/synthetics/safety.py, as alerts send)."""

import http.client
import socket
import ssl

import safety    # copied into the bundle by build-investigator.sh
from domain.delivery import DeliveryError


def post(url, body, headers, timeout=10):
    """-> the HTTP status. Raises DeliveryError for an address that isn't allowed or can't be reached."""
    try:
        scheme, host, port, path = safety.target(url)
        ip = safety.resolve(host, port)[0]
        sock = socket.create_connection((ip, port), timeout=timeout)
        try:
            if scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, port, timeout=timeout)
            conn.sock = sock
            conn.request("POST", path, body=body, headers={**headers, "Host": host})
            return conn.getresponse().status
        finally:
            sock.close()
    except safety.Refused as e:
        raise DeliveryError(f"address not allowed: {e}")
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        raise DeliveryError(f"{type(e).__name__}: {str(e)[:200]}")
