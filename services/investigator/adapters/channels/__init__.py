"""One adapter per kind of channel. Each renders a domain.delivery.Outcome in its own format, always
with a plain-text version (text.plain), keeps the feedback links in the message body, and raises
DeliveryError when the channel refuses it. Adding a channel: one module here, its tests, an entry in
senders()."""

from adapters.channels.email import EmailSender
from adapters.channels.slack import SlackSender
from adapters.channels.teams import TeamsSender
from adapters.channels.webhook import WebhookSender


def senders(post, ses_send=None, sns_publish=None):
    """kind -> sender. post(url, body bytes, headers) -> HTTP status; ses_send(to, subject, text, html);
    sns_publish(topic_arn, subject, text)."""
    return {s.kind: s for s in (SlackSender(post), TeamsSender(post), WebhookSender(post), EmailSender(ses_send, sns_publish))}
