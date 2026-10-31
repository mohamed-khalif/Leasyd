"""Any https endpoint: the Outcome as JSON (with its plain text), signed like alerts are:
X-Leasyd-Signature: sha256=<HMAC-SHA256 of the body with the channel's signing secret>."""

import hashlib
import hmac
import json

from adapters.channels import text
from domain.delivery import DeliveryError


class WebhookSender:
    kind = "webhook"

    def __init__(self, post):
        self.post = post

    def render(self, out):
        return {"type": "investigation", **out.to_dict(), "text": text.plain(out)}

    def send(self, channel, out):
        body = json.dumps(self.render(out), separators=(",", ":")).encode()
        sig = hmac.new(channel["secret"].encode(), body, hashlib.sha256).hexdigest()
        status = self.post(channel["url"], body, {"Content-Type": "application/json", "X-Leasyd-Signature": f"sha256={sig}",
                                                  "X-Leasyd-Event": "investigation", "User-Agent": "Leasyd-Alerts/1.0"})
        if not 200 <= status < 300:
            raise DeliveryError(f"the webhook answered HTTP {status}")
