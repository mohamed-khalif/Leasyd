"""Slack (incoming webhook): Block Kit, with the plain text as the notification text. The feedback
links are in the message as links and as buttons (a button with a url just opens it)."""

import json

from adapters.channels import text
from domain.delivery import DeliveryError


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(url, label):
    return f"<{esc(url)}|{esc(label).replace('|', '¦')}>"


def _section(md):
    return {"type": "section", "text": {"type": "mrkdwn", "text": md[:2900]}}


class SlackSender:
    kind = "slack"

    def __init__(self, post):
        self.post = post

    def render(self, out):
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": text.subject(out)[:150]}}]
        if out.kind == "finding":
            blocks.append(_section(f"*Root cause:* {esc(out.headline)}\n_{text.confidence(out)}_"
                                   + (f" · service *{esc(out.service)}*" if out.service else "")))
            blocks.append(_section(esc(out.summary)))
            if out.evidence:
                blocks.append(_section("*Evidence*\n" + "\n".join(f"• {esc(e['claim'] or e['title'])} — {link(e['link'], e['title'])}"
                                                                  for e in out.evidence[:text.MAX_EVIDENCE])))
            if out.ruled_out:
                blocks.append(_section("*Ruled out*\n" + "\n".join(f"• {esc(r)}" for r in out.ruled_out[:5])))
            if out.next_step:
                blocks.append(_section(f"*Next step:* {esc(out.next_step)}"))
            if out.feedback:
                blocks.append(_section(f"*Was this right?* {link(out.feedback['right'], 'Right')} · {link(out.feedback['wrong'], 'Wrong')}"))
                blocks.append({"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Right"}, "url": out.feedback["right"], "style": "primary"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Wrong"}, "url": out.feedback["wrong"]}]})
        else:
            blocks.append(_section(f"*{esc(out.headline)}.*\n{esc(out.summary)}"))
        foot = f"Investigation {out.investigation_id} · Leasyd only read data; it changed nothing"
        if out.alert_url:
            foot = f"{link(out.alert_url, 'The alert')} · {foot}"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": foot}]})
        return {"text": text.plain(out)[:3000], "blocks": blocks}

    def send(self, channel, out):
        status = self.post(channel["url"], json.dumps(self.render(out)).encode(), {"Content-Type": "application/json"})
        if not 200 <= status < 300:
            raise DeliveryError(f"Slack answered HTTP {status}")
