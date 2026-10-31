"""Microsoft Teams, through a Workflows webhook ("When a Teams webhook request is received" then "Post
card in a chat or channel"): a message with one Adaptive Card (1.4), as that template expects. The
card's fallbackText is the plain text; the feedback links are in its text and its actions."""

import json

from adapters.channels import text
from domain.delivery import DeliveryError


def md(s):
    """Adaptive Card text is a small Markdown: keep the data from becoming links or formatting."""
    return str(s or "").replace("[", "(").replace("]", ")").replace("*", "\\*").replace("_", "\\_")


def tb(s, **kw):
    return {"type": "TextBlock", "text": s[:3000], "wrap": True, **kw}


class TeamsSender:
    kind = "teams"

    def __init__(self, post):
        self.post = post

    def render(self, out):
        body = [tb(md(text.subject(out)), weight="Bolder", size="Medium")]
        actions = []
        if out.kind == "finding":
            body.append(tb(f"**Root cause:** {md(out.headline)}"))
            facts = [{"title": "Confidence", "value": text.confidence(out)}]
            if out.service:
                facts.append({"title": "Service", "value": md(out.service)})
            body += [{"type": "FactSet", "facts": facts}, tb(md(out.summary))]
            if out.evidence:
                body.append(tb("**Evidence**"))
                body += [tb(f"- {md(e['claim'] or e['title'])} ([{md(e['title'])}]({e['link']}))", spacing="None")
                         for e in out.evidence[:text.MAX_EVIDENCE]]
            if out.ruled_out:
                body.append(tb("**Ruled out**"))
                body += [tb(f"- {md(r)}", spacing="None") for r in out.ruled_out[:5]]
            if out.next_step:
                body.append(tb(f"**Next step:** {md(out.next_step)}"))
            if out.feedback:
                body.append(tb(f"**Was this right?** [Right]({out.feedback['right']}) · [Wrong]({out.feedback['wrong']})"))
                actions = [{"type": "Action.OpenUrl", "title": "Right", "url": out.feedback["right"]},
                           {"type": "Action.OpenUrl", "title": "Wrong", "url": out.feedback["wrong"]}]
        else:
            body += [tb(f"**{md(out.headline)}.**"), tb(md(out.summary))]
        if out.alert_url:
            actions.append({"type": "Action.OpenUrl", "title": "The alert", "url": out.alert_url})
        body.append(tb(f"Investigation {out.investigation_id}. Leasyd only read data; it changed nothing.", isSubtle=True, size="Small"))
        card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                "fallbackText": text.plain(out), "body": body, "msteams": {"width": "Full"}, **({"actions": actions} if actions else {})}
        return {"type": "message", "summary": text.subject(out),
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}

    def send(self, channel, out):
        status = self.post(channel["url"], json.dumps(self.render(out)).encode(), {"Content-Type": "application/json"})
        if not 200 <= status < 300:
            raise DeliveryError(f"Teams answered HTTP {status}")
