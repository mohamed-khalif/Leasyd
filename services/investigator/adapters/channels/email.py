"""Email, as alert emails go: to one of the account's own users with Amazon SES (text and HTML); to
any other address through the channel's SNS topic, which the person confirmed (plain text only)."""

import html

from adapters.channels import text
from domain.delivery import DeliveryError


def _a(url, label):
    return f'<a href="{html.escape(url)}" style="color:#4f6df5">{html.escape(label)}</a>'


def _p(s, style=""):
    return f'<p style="margin:0 0 12px;{style}">{s}</p>'


class EmailSender:
    kind = "email"

    def __init__(self, ses_send, sns_publish):
        self.ses_send, self.sns_publish = ses_send, sns_publish

    def render_html(self, out):
        e = html.escape
        parts = [_p(e(text.subject(out)), "font-weight:700;font-size:17px")]
        if out.kind == "finding":
            parts += [_p(f"<b>Root cause:</b> {e(out.headline)}<br><span style=\"color:#6b7280\">{e(text.confidence(out))}</span>"),
                      _p(e(out.summary))]
            if out.evidence:
                items = "".join(f'<li style="margin:0 0 6px">{e(x["claim"] or x["title"])} ({_a(x["link"], x["title"])})</li>'
                                for x in out.evidence[:text.MAX_EVIDENCE])
                parts.append(f'<p style="margin:0 0 4px"><b>Evidence</b></p><ul style="margin:0 0 12px;padding-left:20px">{items}</ul>')
            if out.ruled_out:
                items = "".join(f"<li>{e(r)}</li>" for r in out.ruled_out[:5])
                parts.append(f'<p style="margin:0 0 4px"><b>Ruled out</b></p><ul style="margin:0 0 12px;padding-left:20px">{items}</ul>')
            if out.next_step:
                parts.append(_p(f"<b>Next step:</b> {e(out.next_step)}"))
            if out.feedback:
                parts.append(_p(f"<b>Was this right?</b> {_a(out.feedback['right'], 'Right')} · {_a(out.feedback['wrong'], 'Wrong')}"
                                '<br><span style="color:#6b7280;font-size:13px">Each link opens a page to confirm; nothing is '
                                "recorded until you press its button.</span>"))
        else:
            parts += [_p(f"<b>{e(out.headline)}.</b>"), _p(e(out.summary))]
        if out.alert_url:
            parts.append(_p(_a(out.alert_url, "The alert")))
        parts.append(_p(f"Investigation {e(out.investigation_id)}. Leasyd only read data; it changed nothing.", "color:#9ca3af;font-size:12px"))
        return ('<div style="font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#111827;max-width:620px">'
                + "".join(parts) + "</div>")

    def send(self, channel, out):
        if channel.get("delivery") == "ses":
            if not self.ses_send:
                raise DeliveryError("email sending isn't set up")
            self.ses_send(channel["email"], text.subject(out), text.plain(out), self.render_html(out))
        elif channel.get("topic_arn") and self.sns_publish:
            self.sns_publish(channel["topic_arn"], text.subject(out)[:99], text.plain(out))
        else:
            raise DeliveryError("this email channel can't be used")
