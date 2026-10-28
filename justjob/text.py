"""Text helpers: HTML to plain text, normalization for dedup, trimming for the AI step."""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone

_LI = re.compile(r"<li\b[^>]*>", re.I)
_BLOCK = re.compile(r"</?(?:p|div|br|ul|ol|li|h[1-6]|tr|table|section|article|header|footer)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_BULLET_GAP = re.compile(r"•[ \t]*\n+[ \t]*")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    if "&lt;" in s and "<" not in s:   # double-escaped HTML (Greenhouse, RemoteOK)
        s = html.unescape(s)
    s = _LI.sub("\n• ", s)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s).replace("\xa0", " ").replace("​", "")
    s = _BULLET_GAP.sub("• ", s)
    out: list[str] = []
    for line in s.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def phrase_re(phrases, plural: bool = False) -> re.Pattern | None:
    """Case-insensitive whole-word match for any of the phrases (they're literal, not regex).
    With plural=True a trailing "s" is allowed too ("LLM" also matches "LLMs")."""
    alts = sorted({p.strip().lower() for p in phrases if p and p.strip()}, key=len, reverse=True)
    if not alts:
        return None
    body = "|".join(re.escape(p).replace(r"\ ", r"[\s\-_/]+") for p in alts)
    suffix = "s?" if plural else ""
    return re.compile(rf"(?<![a-z0-9])(?:{body}){suffix}(?![a-z0-9])", re.I)


_REMOTE = re.compile(r"\bremote\b|\bwfh\b|work from home|home[\s-]based|anywhere|worldwide|telecommut", re.I)
_NOT_REMOTE = re.compile(r"\b(?:no|not|non)[\s-]remote\b", re.I)


def looks_remote(text: str | None) -> bool:
    return bool(text) and bool(_REMOTE.search(text)) and not _NOT_REMOTE.search(text)


# --- dedup -------------------------------------------------------------------------------

_COMPANY_SUFFIX = re.compile(
    r"\b(?:inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|pvt|private|technologies|labs?|"
    r"group|holdings)\b\.?",
    re.I,
)
_TITLE_NOISE = re.compile(r"\b(?:remote|hybrid|on-?site|full[\s-]?time|part[\s-]?time|contract)\b", re.I)


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def norm_company(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", _ascii(s)).strip()
    stripped = " ".join(_COMPANY_SUFFIX.sub(" ", s).split())
    stripped = re.sub(r" ai$", "", stripped)   # "Scale AI" and "Scale" are the same company
    return stripped.replace(" ", "") or s.replace(" ", "")


def norm_title(s: str) -> str:
    s = _ascii(s)
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = _TITLE_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9+#]+", " ", s)
    return " ".join(s.split())


def dedup_key(company: str, title: str) -> str:
    return f"{norm_company(company)}|{norm_title(title)}"


# --- dates -------------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(value) -> str | None:
    """Epoch seconds/milliseconds or an ISO-ish string -> ISO 8601 UTC. None if unparseable."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e11 else value
            dt = datetime.fromtimestamp(ts, timezone.utc)
        else:
            s = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


# --- trimming for the AI step ------------------------------------------------------------

_BOILERPLATE = re.compile(
    r"equal (?:employment )?opportunit|\beeo\b|affirmative action|regardless of|without regard to|"
    r"reasonable accommodation|accommodations? (?:during|for)|privacy (?:notice|policy)|"
    r"background check|e-?verify|401\(?k|paid time off|\bpto\b|parental leave|health(?:care)? insurance|"
    r"dental|vision (?:insurance|coverage)|wellness|perks|stipend|recruit(?:ment|ing) (?:fraud|scam)|"
    r"\bgdpr\b|we celebrate|diverse (?:team|workforce)|protected (?:veteran|class|characteristic)|"
    r"by applying|applicant data|cookie|pay transparency|salary range|pay range|compensation range|"
    r"base (?:pay|salary) range|total rewards|\bbenefits\b",
    re.I,
)
_RELEVANT = re.compile(
    r"\b(?:years?|yrs|experience|required|requirements?|qualifications?|must|you have|you'll|you will|"
    r"skills?|proficien\w*|familiar\w*|degree|bachelor\w*|master\w*|phd|knowledge|responsibilit\w*|"
    r"what you|the role|in this role|nice to have|bonus|preferred|new grad\w*|entry|junior|senior)\b",
    re.I,
)


def trim_for_ai(text: str, limit: int) -> str:
    """Drop boilerplate lines; if still too long, keep requirement-like lines first."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _BOILERPLATE.search(line):
            continue
        lines.append(line[:500])
    if sum(len(l) + 1 for l in lines) <= limit:
        return "\n".join(lines)

    keep = [False] * len(lines)
    budget = limit
    for wanted in (True, False):   # requirement-like lines first, then fill with the rest
        for i, line in enumerate(lines):
            if keep[i] or bool(_RELEVANT.search(line)) != wanted:
                continue
            if len(line) + 1 > budget:
                continue
            keep[i] = True
            budget -= len(line) + 1
    return "\n".join(l for l, k in zip(lines, keep) if k)
