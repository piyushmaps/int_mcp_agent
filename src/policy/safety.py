"""Conservative local screening; these rules are not a complete enterprise DLP classifier."""
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit


class UnsafeContentError(ValueError):
    pass

_INJECTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|system)\s+(?:instructions|prompts)|"
    r"(?:reveal|print|exfiltrate|send)\s+(?:the\s+|all\s+)?(?:system prompt|secrets|credentials)|"
    r"\[/?(?:system|developer)\]|<\|(?:im_start|system|developer)|"
    r"(?:override|bypass|disable)\s+(?:the\s+)?(?:safety|security|access|policy|guardrail)", re.I
)
_SECRETS = [
    re.compile(r"\b(?:sk-(?:proj-)?|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:api[_ -]?key|password|secret|access[_ -]?token)\s*[:=]\s*['\"]?[^\s'\";,]{4,}"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
]

def sanitize_text(text: str) -> str:
    if not isinstance(text, str):
        raise UnsafeContentError("invalid_text")
    value = unicodedata.normalize("NFKC", text)
    value = "".join(c for c in value if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf"})
    if _INJECTION.search(value):
        raise UnsafeContentError("untrusted_instructions")
    for pattern in _SECRETS:
        value = pattern.sub("[REDACTED]", value)
    return value.strip()

def safe_url(url: str) -> str:
    if not url:
        return ""
    if any(c in url for c in "\r\n\\\t") or sanitize_text(url) != url:
        raise UnsafeContentError("unsafe_url")
    if re.fullmatch(r"/api/documents/[A-Za-z0-9_.%~-]+/content", url):
        return url
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise UnsafeContentError("unsafe_url")
    if parts.query:
        # Source access tokens and sensitive values must never become citation destinations.
        raise UnsafeContentError("url_query_not_allowed")
    return urlunsplit(parts)

def screen_input(text: str) -> str:
    return sanitize_text(text)

