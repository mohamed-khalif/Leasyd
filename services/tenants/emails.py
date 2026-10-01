"""Emails Leasyd sends people (not alerts): invitations, the welcome after signing up, and "you
already have an account". Sent with Amazon SES from EMAIL_FROM (e.g. "Leasyd
<no-reply@app.leasyd.com>", a domain identity with DKIM, obs-phaseE1). Without EMAIL_FROM nothing
is sent here and invitations fall back to Cognito's own email (about 50 a day).

Each message is plain text plus a simple HTML version; links go to APP_URL.
"""

import html
import os

import boto3

EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
APP_URL = os.environ.get("APP_URL", "https://app.leasyd.com").rstrip("/")

_ses = None


def enabled():
    return bool(EMAIL_FROM)


def send(to, subject, paragraphs, credentials=None, button=None):
    """paragraphs: plain sentences; credentials: [(label, value)] shown as a box; button: (label, url)."""
    global _ses
    _ses = _ses or boto3.client("sesv2")
    text = "\n\n".join(paragraphs)
    if credentials:
        text += "\n\n" + "\n".join(f"{k}: {v}" for k, v in credentials)
    if button:
        text += f"\n\n{button[0]}: {button[1]}"
    text += "\n\n-- \nLeasyd"
    box = ""
    if credentials:
        rows = "".join(f'<tr><td style="color:#6b7280;padding:4px 16px 4px 0">{html.escape(k)}</td>'
                       f'<td style="font-family:ui-monospace,Menlo,monospace;padding:4px 0">{html.escape(v)}</td></tr>'
                       for k, v in credentials)
        box = f'<table style="background:#f3f4f6;border-radius:6px;padding:12px 16px;margin:16px 0">{rows}</table>'
    cta = ""
    if button:
        cta = (f'<p style="margin:24px 0"><a href="{html.escape(button[1])}" style="background:#4f6df5;color:#fff;'
               f'padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600">{html.escape(button[0])}</a></p>')
    body = "".join(f'<p style="margin:0 0 12px">{html.escape(p)}</p>' for p in paragraphs)
    page = (f'<div style="font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#111827;max-width:560px">'
            f'<p style="font-weight:700;font-size:18px;margin:0 0 20px">Leasyd</p>{body}{box}{cta}'
            f'<p style="color:#9ca3af;font-size:12px;margin-top:32px">You received this because someone used this '
            f'address on Leasyd. If that wasn\'t you, you can ignore this email.</p></div>')
    _ses.send_email(FromEmailAddress=EMAIL_FROM, Destination={"ToAddresses": [to]},
                    Content={"Simple": {"Subject": {"Data": subject, "Charset": "UTF-8"},
                                        "Body": {"Text": {"Data": text, "Charset": "UTF-8"},
                                                 "Html": {"Data": page, "Charset": "UTF-8"}}}})


def invitation(to, temp_password, company, invited_by=None):
    who = f"{invited_by} invited you" if invited_by else "You have been invited"
    send(to, f"You're invited to {company} on Leasyd",
         [f"{who} to {company} on Leasyd, where your team sees its applications' logs, traces and metrics.",
          "Sign in with this temporary password; you'll choose your own the first time. It works for 7 days."],
         credentials=[("Email", to), ("Temporary password", temp_password)],
         button=("Sign in to Leasyd", APP_URL))


def welcome(to, temp_password, company):
    send(to, "Your Leasyd account is ready",
         [f"Welcome to Leasyd! The account for {company} is ready.",
          "Sign in with this temporary password; you'll choose your own the first time. It works for 7 days.",
          "Then create an API key under Settings and point your OpenTelemetry SDK or Collector at Leasyd: "
          "your logs, traces and metrics show up within a minute."],
         credentials=[("Email", to), ("Temporary password", temp_password)],
         button=("Sign in to Leasyd", APP_URL))


def already_registered(to):
    send(to, "You already have a Leasyd account",
         ["Someone (hopefully you) tried to sign up to Leasyd with this email address, which already has an account.",
          "Sign in instead. If you forgot your password, use \"Forgot password?\" on the sign-in page."],
         button=("Sign in to Leasyd", APP_URL))
