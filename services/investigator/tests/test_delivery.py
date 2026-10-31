"""Delivering investigations: where they go, retries and the fallback, nothing sent twice, the
skipped notice once an hour, and each channel's format (with its plain text and the feedback links)."""
import json

from adapters.channels import senders
from adapters.channels.email import EmailSender
from adapters.channels.slack import SlackSender
from adapters.channels.teams import TeamsSender
from adapters.channels.text import plain
from adapters.channels.webhook import WebhookSender
from domain.delivery import DeliveryError, choose_channels, deliver, outcome, settings_from
from domain.model import COMPLETED, FAILED, SKIPPED, Evidence, Investigation, Result, Trigger
from domain.workflow import give_feedback, iso
from tests.fakes import Clock, MemoryChannels, MemoryStore

LINKS = {"right": "https://ingest.leasyd.com/v1/investigations/feedback/tok?v=right",
         "wrong": "https://ingest.leasyd.com/v1/investigations/feedback/tok?v=wrong"}


def finished(status=COMPLETED, rule_id="r1", service="checkout", root="payment times out calling fraud-api"):
    inv = Investigation(id="a" * 16, tenant="acme", status=status, created_at="2026-10-30T02:14:05Z", updated_at="t",
                        trigger=Trigger(source="alert", title="Checkout errors: checkout", dedupe_key="k", started_at="2026-10-30T02:14:00Z",
                                        subject="checkout", service=service, rule_id=rule_id, url="https://app.leasyd.com/#/alerts"))
    if status == COMPLETED:
        inv.result = Result(title=inv.trigger.title, summary="Checkout fails <b>because</b> payment times out.", root_cause=root,
                            confidence=0.82 if not root.startswith("not sure") else 0.2, ruled_out=["database: normal"], next_step="Roll back payment.",
                            evidence=[Evidence(id="ev1", kind="traces", title="Spans of payment with errors", summary="", link="https://app.leasyd.com/#/traces?service=payment",
                                               query={}, start="s", end="e", claim="312 of 340 failed traces end in fraud-api timeouts")])
    return inv


class Sender:
    def __init__(self, kind, fail=0, error=DeliveryError("HTTP 500")):
        self.kind, self.fail, self.error, self.got = kind, fail, error, []

    def send(self, channel, out):
        self.got.append((channel["id"], out))
        if self.fail:
            self.fail -= 1
            raise self.error


def setup(inv, channels, settings=None, rules=None):
    store = MemoryStore()
    store.create(inv)
    chans = MemoryChannels({("acme", c["id"]): c for c in channels}, {"acme": settings or {}}, rules or {("acme", "r1"): {"channels": ["c1"]}})
    return store, chans


def run(store, chans, snd, links=lambda: LINKS):
    waits = []
    out = deliver(store, chans, snd, Clock(1793327400), iso, "acme", "a" * 16, links, waits.append)
    return out, waits


def test_channels_come_from_the_rule_then_the_service_then_the_settings_then_the_alerts_own():
    inv, there = finished(), {"c1", "c2", "c3", "fb"}.__contains__
    assert choose_channels(inv, {}, {"channels": ["c1"]}, there) == (["c1"], None)
    assert choose_channels(inv, {"channels": ["c2"], "fallback": "fb"}, {"channels": ["c1"]}, there) == (["c2"], "fb")
    assert choose_channels(inv, {"channels": ["c2"], "services": {"Checkout": ["c3"]}}, {"channels": ["c1"]}, there) == (["c3"], None)
    assert choose_channels(inv, {"channels": ["c2"]}, {"channels": ["c1"], "investigation_channels": ["c1", "gone"]}, there) == (["c1"], None)
    assert choose_channels(finished(rule_id=None), {}, None, there) == ([], None)             # a webhook with no settings: nowhere
    assert choose_channels(inv, {"channels": ["gone"], "fallback": "c1"}, None, there) == ([], "c1")


def test_a_finding_goes_to_each_channel_once_with_feedback_links():
    slack, mail = Sender("slack"), Sender("email")
    store, chans = setup(finished(), [{"id": "c1", "type": "slack", "name": "#ops"}, {"id": "c2", "type": "email", "name": "On-call"}],
                         rules={("acme", "r1"): {"channels": ["c1", "c2"]}})
    made = []
    out, _ = run(store, chans, {"slack": slack, "email": mail}, lambda: made.append(1) or LINKS)
    assert out == {"sent": ["c1", "c2"], "failed": [], "fallback": None} and made == [1]
    o = slack.got[0][1]
    assert o.kind == "finding" and o.feedback == LINKS and o.confidence_label == "high" and o.evidence[0]["link"].endswith("service=payment")
    inv = store.get("acme", "a" * 16)
    assert {c: d["status"] for c, d in inv.deliveries.items()} == {"c1": "sent", "c2": "sent"}
    assert [e["type"] for e in inv.events][-2:] == ["delivered", "delivered"]
    assert run(store, chans, {"slack": slack, "email": mail})[0] == {"sent": [], "failed": [], "fallback": None}   # redelivered: nothing twice
    assert len(slack.got) == 1


def test_a_failing_channel_is_retried_then_the_fallback_gets_it():
    slack, mail = Sender("slack", fail=99), Sender("email")
    store, chans = setup(finished(), [{"id": "c1", "type": "slack", "name": "#ops"}, {"id": "fb", "type": "email", "name": "Owners"}],
                         settings={"fallback": "fb"})
    out, waits = run(store, chans, {"slack": slack, "email": mail})
    assert out == {"sent": ["fb"], "failed": ["c1"], "fallback": "fb"} and len(slack.got) == 3 and waits == [1, 3]
    d = store.get("acme", "a" * 16).deliveries
    assert d["c1"] == {"kind": "slack", "status": "failed", "attempts": 3, "at": "2026-10-30T02:30:00Z", "error": "HTTP 500"}
    assert d["fb"]["fallback"] is True and d["fb"]["status"] == "sent"
    slack2 = Sender("slack", fail=1)                       # a flaky channel: the second attempt gets through, no fallback
    store, chans = setup(finished(), [{"id": "c1", "type": "slack", "name": "#ops"}, {"id": "fb", "type": "email", "name": "Owners"}],
                         settings={"fallback": "fb"})
    assert run(store, chans, {"slack": slack2, "email": mail})[0]["sent"] == ["c1"] and len(mail.got) == 1


def test_skipped_is_told_once_an_hour_and_failed_says_so_without_feedback_links():
    snd = Sender("slack")
    for n in range(2):
        store, chans = setup(finished(status=SKIPPED), [{"id": "c1", "type": "slack", "name": "#ops"}])
        chans.notices = {("acme", "2026-10-30T02")} if n else set()
        out, _ = run(store, chans, {"slack": snd}, lambda: 1 / 0)
        assert out == ({"sent": ["c1"], "failed": [], "fallback": None} if n == 0 else "quiet")
    assert snd.got[0][1].kind == "skipped" and snd.got[0][1].feedback is None and "hourly limit" in snd.got[0][1].headline
    store, chans = setup(finished(status=FAILED), [{"id": "c1", "type": "slack", "name": "#ops"}])
    run(store, chans, {"slack": snd}, lambda: 1 / 0)
    assert snd.got[-1][1].kind == "failed" and snd.got[-1][1].feedback is None


def test_no_channel_or_no_adapter_is_recorded_not_raised():
    store, chans = setup(finished(rule_id=None), [])
    assert run(store, chans, {})[0] == {"sent": [], "failed": [], "fallback": None}
    assert store.get("acme", "a" * 16).events[-1]["type"] == "delivery_failed"
    store, chans = setup(finished(), [{"id": "c1", "type": "pager", "name": "x"}])
    assert run(store, chans, {})[0]["failed"] == ["c1"]
    assert store.get("acme", "a" * 16).deliveries["c1"]["error"] == "no adapter for pager channels"


def test_feedback_is_recorded_once_by_a_link_and_can_be_changed_by_a_person():
    store, _ = setup(finished(), [])
    inv, ok = give_feedback(store, Clock(1793327400), "acme", "a" * 16, "right", "yes, fraud-api")
    assert ok and inv.feedback == {"verdict": "right", "note": "yes, fraud-api", "at": "2026-10-30T02:30:00Z", "by": "link"}
    assert give_feedback(store, Clock(1793327400), "acme", "a" * 16, "wrong")[1] is False
    inv, ok = give_feedback(store, Clock(1793327400), "acme", "a" * 16, "wrong", by="ana@acme.io", replace=True)
    assert ok and inv.feedback["verdict"] == "wrong" and [e["type"] for e in inv.events].count("feedback") == 2


def test_settings_only_name_the_tenants_channels():
    there = {"c1", "c2"}.__contains__
    assert settings_from({"channels": ["c1", "c1"], "services": {"checkout": ["c2"], " ": ["c1"]}, "fallback": "c2"}, there) == \
        {"channels": ["c1"], "services": {"checkout": ["c2"]}, "fallback": "c2"}
    for bad in ({"channels": ["zz"]}, {"fallback": "zz"}, {"services": {"x": "c1"}}, []):
        try:
            settings_from(bad, there)
            raise AssertionError(bad)
        except ValueError:
            pass


# -- the adapters: each format, its plain text, the links in the body

def test_plain_text_has_everything_and_the_links():
    t = plain(outcome(finished(), LINKS))
    assert t.startswith("[Leasyd] Investigation: Checkout errors: checkout")
    for part in ("Root cause: payment times out calling fraud-api", "Confidence 0.82 (high)", "312 of 340 failed traces",
                 "https://app.leasyd.com/#/traces?service=payment", "Ruled out:", "Next step: Roll back payment.", LINKS["right"], LINKS["wrong"],
                 "nothing is recorded until you press", "Leasyd only read data"):
        assert part in t, part
    assert plain(outcome(finished(root="not sure: nothing failed"), LINKS)).split("\n")[2] == "Root cause: Not sure: nothing failed"


def test_slack_blocks_escape_the_data_and_carry_the_links():
    posted = []
    SlackSender(lambda url, body, h: posted.append((url, json.loads(body), h)) or 200).send({"url": "https://hooks.slack.com/x"}, outcome(finished(), LINKS))
    url, msg, h = posted[0]
    assert url == "https://hooks.slack.com/x" and h == {"Content-Type": "application/json"} and msg["text"].startswith("[Leasyd]")
    blob = json.dumps(msg["blocks"])
    assert "&lt;b&gt;because&lt;/b&gt;" in blob and "<b>" not in blob
    assert f"<{LINKS['right']}|Right>" in blob and {"type": "button", "text": {"type": "plain_text", "text": "Wrong"}, "url": LINKS["wrong"]} in msg["blocks"][-2]["elements"]
    try:
        SlackSender(lambda *a: 404).send({"url": "u"}, outcome(finished(), LINKS))
        raise AssertionError("no error")
    except DeliveryError as e:
        assert str(e) == "Slack answered HTTP 404"


def test_teams_gets_one_adaptive_card_with_a_text_fallback():
    msg = TeamsSender(None).render(outcome(finished(), LINKS))
    att = msg["attachments"][0]
    assert msg["type"] == "message" and att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4" and card["fallbackText"].startswith("[Leasyd]")
    assert [a["title"] for a in card["actions"]] == ["Right", "Wrong", "The alert"]
    assert any(f"[Right]({LINKS['right']})" in b.get("text", "") for b in card["body"])
    skipped = TeamsSender(None).render(outcome(finished(status=SKIPPED)))
    assert [a["title"] for a in skipped["attachments"][0]["content"]["actions"]] == ["The alert"]


def test_webhooks_are_signed_and_carry_the_outcome_and_its_text():
    import hashlib
    import hmac
    posted = []
    WebhookSender(lambda url, body, h: posted.append((body, h)) or 202).send({"url": "https://x.example/h", "secret": "s3"}, outcome(finished(), LINKS))
    body, h = posted[0]
    assert h["X-Leasyd-Signature"] == "sha256=" + hmac.new(b"s3", body, hashlib.sha256).hexdigest() and h["X-Leasyd-Event"] == "investigation"
    d = json.loads(body)
    assert d["type"] == "investigation" and d["kind"] == "finding" and d["feedback"] == LINKS and d["text"].startswith("[Leasyd]")


def test_email_goes_by_ses_to_own_users_and_by_the_confirmed_topic_to_others():
    sent, published = [], []
    e = EmailSender(lambda *a: sent.append(a), lambda *a: published.append(a))
    e.send({"delivery": "ses", "email": "ana@acme.io"}, outcome(finished(), LINKS))
    to, subject, text, html = sent[0]
    assert to == "ana@acme.io" and subject == "[Leasyd] Investigation: Checkout errors: checkout" and LINKS["wrong"] in text
    assert "&lt;b&gt;because" in html and "<b>because" not in html and f'href="{LINKS["right"]}"' in html.replace("&amp;", "&")
    e.send({"topic_arn": "arn:aws:sns:x:obs-alert-acme-c2", "email": "x@y.io"}, outcome(finished(), LINKS))
    assert published[0][0] == "arn:aws:sns:x:obs-alert-acme-c2" and len(published[0][1]) <= 99
    try:
        EmailSender(None, None).send({"delivery": "ses", "email": "a@b.c"}, outcome(finished(), LINKS))
        raise AssertionError("no error")
    except DeliveryError:
        pass


def test_every_kind_of_channel_has_an_adapter():
    assert set(senders(lambda *a: 200)) == {"slack", "teams", "webhook", "email"}
