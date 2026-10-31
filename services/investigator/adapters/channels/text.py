"""The plain-text version of an Outcome: every channel's fallback, and the whole of a plain email."""

MAX_EVIDENCE = 6


def confidence(out):
    return f"confidence {out.confidence:.2f} ({out.confidence_label})" if out.confidence is not None else ""


def subject(out):
    word = {"finding": "Investigation", "skipped": "Investigation skipped", "failed": "Investigation failed"}[out.kind]
    return f"[Leasyd] {word}: {out.title}"[:200]


def plain(out):
    lines = [subject(out), ""]
    if out.kind == "finding":
        lines += [f"Root cause: {out.headline}", confidence(out).capitalize(), "", out.summary]
        if out.evidence:
            lines += ["", "Evidence:"] + [f"- {e['claim'] or e['title']}: {e['link']}" for e in out.evidence[:MAX_EVIDENCE]]
        if out.ruled_out:
            lines += ["", "Ruled out:"] + [f"- {r}" for r in out.ruled_out[:5]]
        if out.next_step:
            lines += ["", f"Next step: {out.next_step}"]
        if out.feedback:
            lines += ["", f"Was this right? Right: {out.feedback['right']}  Wrong: {out.feedback['wrong']}",
                      "(Each link opens a page to confirm; nothing is recorded until you press its button.)"]
    else:
        lines += [out.headline + ".", "", out.summary]
    if out.alert_url:
        lines += ["", f"The alert: {out.alert_url}"]
    lines += ["", f"Investigation {out.investigation_id}. Leasyd only read data; it changed nothing."]
    return "\n".join(lines)
