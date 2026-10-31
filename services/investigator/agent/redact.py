"""Takes secrets and personal data out of text before it reaches the model (tool results, alert
details). Patterns, not understanding: it errs towards redacting. Each match becomes [REDACTED:<kind>]
so the model still sees that something was there.

Kept on purpose: IP addresses, hostnames, service names, trace and span ids, numbers (they are the
evidence). Not covered: names of people and street addresses in free text (no reliable pattern).
"""

import re

_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|ant|proj)?[-_]?[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("url_password", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    ("secret", re.compile(r"(?i)\b((?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|auth(?:orization)?|session[_-]?id|cookie)"
                          r"[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED)([^\s\"',;&]{4,})")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"(?<![\w.])\+\d[\d ()-]{8,}\d\b")),
]


def _luhn(digits):
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total, alt = total + n, not alt
    return total % 10 == 0


def redact(text):
    """-> (redacted text, {kind: count})."""
    counts = {}

    def sub(kind):
        def repl(m):
            if kind == "card":
                digits = re.sub(r"\D", "", m.group(0))
                if not 13 <= len(digits) <= 19 or not _luhn(digits):
                    return m.group(0)      # a long number that isn't a card number: keep it (ids, counts)
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "secret":
                return m.group(1) + "[REDACTED:secret]"
            return f"[REDACTED:{kind}]"
        return repl
    for kind, pat in _PATTERNS:
        text = pat.sub(sub(kind), text)
    return text, counts
