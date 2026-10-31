"""The page a Right/Wrong link opens. Opening it records nothing (mail scanners and link previews open
links); pressing its button POSTs the answer, which uses up the link."""

import html

STYLE = ("body{font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#111827;background:#f9fafb;margin:0;padding:24px 16px}"
         "main{max-width:560px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:24px}"
         "h1{font-size:18px;margin:0 0 16px}.muted{color:#6b7280}label{display:block;margin:8px 0}"
         "textarea{width:100%;box-sizing:border-box;min-height:70px;font:inherit;padding:8px;border:1px solid #d1d5db;border-radius:6px}"
         "button{background:#4f6df5;color:#fff;border:0;border-radius:6px;padding:10px 18px;font:inherit;font-weight:600;cursor:pointer;margin-top:12px}")
HEADERS = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex",
           "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}


def page(status, title, body):
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>{html.escape(title)} · Leasyd</title><style>{STYLE}</style></head><body><main><p class=\"muted\">Leasyd</p>"
           f"<h1>{html.escape(title)}</h1>{body}</main></body></html>")
    return {"statusCode": status, "headers": HEADERS, "body": doc}


def ask(inv, chosen):
    e = html.escape
    r = inv.result
    radios = "".join(f'<label><input type="radio" name="verdict" value="{v}"{" checked" if chosen == v else ""} required> {label}</label>'
                     for v, label in (("right", "Right: this was the cause"), ("wrong", "Wrong: the cause was something else")))
    return page(200, "Was this investigation right?",
                f"<p><b>{e(inv.trigger.title)}</b></p><p>Root cause: {e(r.root_cause)}<br>"
                f'<span class="muted">confidence {r.confidence:.2f}</span></p>'
                f'<form method="post">{radios}<label>What was it, if not this? (optional)<textarea name="note" maxlength="1000"></textarea></label>'
                "<button type=\"submit\">Record my answer</button></form>"
                '<p class="muted" style="font-size:13px">Your answer is kept with the investigation and helps later ones.</p>')


def answered(inv):
    f = inv.feedback
    return page(200, "Already answered", f"<p><b>{html.escape(inv.trigger.title)}</b> was marked <b>{html.escape(f['verdict'])}</b> "
                                          f"on {html.escape(f['at'])}.</p>")


def thanks(verdict):
    return page(200, "Thank you", f"<p>Recorded: <b>{html.escape(verdict)}</b>. You can close this page.</p>")


def gone():
    return page(410, "This link no longer works", "<p>It was already used or has expired (links last 14 days).</p>")


def bad(why):
    return page(400, "Something is missing", f"<p>{html.escape(why)}</p>")
