"""Rule-based filtering and ranking. No AI here.

Every stored job gets a rule_status:
  pass     - looks like a fit, experience requirement is within the limit
  unclear  - title/location fit but the experience requirement couldn't be read reliably
  reject   - wrong title, wrong location, or clearly needs more experience than the limit
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text import looks_remote, phrase_re

_WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
}
_N = r"(\d{1,2}|" + "|".join(_WORD_NUM) + r")"
_YRS = r"(?:years?|yrs?)"
_DASH = r"\s*(?:-|–|—|to)\s*"

# Ordered most specific first; each pattern's first group is the minimum years it states.
_YEAR_PATTERNS = [
    re.compile(rf"\b{_N}{_DASH}{_N}\s*\+?\s*{_YRS}", re.I),                        # 2-4 years
    re.compile(rf"\bbetween\s+{_N}\s+(?:and|to|-)\s+{_N}\s+{_YRS}", re.I),         # between 2 and 4 years
    re.compile(rf"\b{_N}\s*(?:\+|plus)\s*{_YRS}", re.I),                           # 3+ years
    re.compile(rf"\b(?:at\s+least|minimum(?:\s+of)?|min\.?)\s+{_N}\s*\+?\s*{_YRS}", re.I),
    re.compile(rf"\b{_N}\s*{_YRS}['’]?\s+(?:of\s+)?(?:[\w/+#.-]+\s+){{0,4}}?(?:experience|exp)\b", re.I),
    re.compile(rf"\bexperience\s*(?:of|:|-|–)?\s*{_N}\s*\+?\s*{_YRS}", re.I),     # Experience: 3 years
]
# A mention is about the company or a degree, not the candidate, when these follow it...
_NOT_EXP_AFTER = re.compile(
    r"\b(?:degree|ago|old|history|in business|track record|anniversary|warranty|vesting|cliff)\b", re.I
)
# ...or these come right before it.
_NOT_EXP_BEFORE = re.compile(
    r"\b(?:founded|we've been|we have been|for over|over the (?:past|last)|in the (?:past|last)|since|"
    r"next|within)\b[^.\n]*$",
    re.I,
)

_ENTRY_TITLE = re.compile(
    r"\b(?:new[\s-]grad(?:uate)?s?|entry[\s-]level|junior|jr\.?|early[\s-]career|graduate|"
    r"university grad(?:uate)?|associate|fresher|apprentice|(?:engineer|developer|sde|swe)[\s-]+(?:i|1))\b",
    re.I,
)
_ENTRY_TEXT = re.compile(
    r"\b(?:new[\s-]grad(?:uate)?s?|entry[\s-]level|early[\s-]career|recent(?:ly)? graduat\w*|freshers?|"
    r"no (?:prior )?experience (?:is )?(?:required|needed|necessary))\b",
    re.I,
)

# Words that don't narrow down where a remote job can be done from.
_GENERIC_REMOTE = re.compile(
    r"\b(?:remote|fully|anywhere|worldwide|world|global(?:ly)?|work from home|wfh|home|flexible|first|"
    r"friendly|or|and|the|in|from|only|ok|based|possible|optional)\b|[^a-z]+",
    re.I,
)


def _num(s: str) -> float:
    return float(_WORD_NUM.get(s.lower(), s))


def year_mentions(text: str) -> list[float]:
    """Minimum years from every experience-like mention in the text."""
    spans: list[tuple[int, int]] = []
    years: list[float] = []
    for pat in _YEAR_PATTERNS:
        for m in pat.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in spans):
                continue
            if _NOT_EXP_AFTER.search(text, m.start(), m.end() + 15):
                continue
            if _NOT_EXP_BEFORE.search(text[max(0, m.start() - 30): m.start()]):
                continue
            value = _num(m.group(1))
            if value > 20:
                continue
            spans.append((m.start(), m.end()))
            years.append(value)
    return years


@dataclass
class RuleResult:
    status: str             # pass | unclear | reject
    reason: str
    exp_years: float | None
    score: int
    skills: list[str] = field(default_factory=list)   # your skills mentioned in the posting


class Rules:
    def __init__(self, cfg: dict):
        t = cfg["filters"]["title"]
        self.role = phrase_re(t.get("role_words", []))
        self.domain = phrase_re(t.get("domain_words", []))
        self.standalone = phrase_re(t.get("standalone", []))
        self.exclude = phrase_re(t.get("exclude", []))
        self.soft_exclude = phrase_re(t.get("exclude_unless_years_fit", []))

        ranking = cfg.get("ranking", {})
        self.title_top = phrase_re(ranking.get("title_top", []))
        self.title_next = phrase_re(ranking.get("title_next", []))

        loc = cfg["filters"]["location"]
        onsite = loc.get("onsite", ["*"])
        regions = loc.get("remote_regions", ["*"])
        self.onsite_any = "*" in onsite
        self.onsite = phrase_re(p for p in onsite if p != "*")
        self.allow_remote = loc.get("remote", True)
        self.regions_any = "*" in regions
        self.regions = phrase_re(p for p in regions if p != "*")

        profile = cfg["profile"]
        self.max_years = float(profile.get("max_years_required", 3))
        self.skill_list = [s for s in profile.get("skills", []) if s.strip()]
        self.skills = phrase_re(self.skill_list, plural=True)

    def title_ok(self, title: str) -> tuple[bool, str]:
        """Whether a title is worth storing. Titles with a soft-excluded word ("Senior") pass
        here and are decided in classify() once the description has been read."""
        # Standalone phrases are removed before the exclude check so that e.g.
        # "Member of Technical Staff" isn't rejected for containing "staff".
        rest = self.standalone.sub(" ", title) if self.standalone else title
        if self.exclude and (m := self.exclude.search(rest)):
            return False, f"title has '{m.group(0)}'"
        if self.standalone and self.standalone.search(title):
            return True, ""
        if not (self.role and self.role.search(title)):
            return False, "not an engineering title"
        if not (self.domain and self.domain.search(title)):
            return False, "title outside target area"
        return True, ""

    def soft_excluded(self, title: str) -> str | None:
        m = self.soft_exclude.search(title) if self.soft_exclude else None
        return m.group(0) if m else None

    def location_ok(self, location: str, remote: bool | None) -> tuple[bool, str]:
        location = (location or "").strip()
        is_remote = bool(remote) or looks_remote(location)
        if is_remote and self.allow_remote:
            region_locked = bool(_GENERIC_REMOTE.sub("", location))
            if not region_locked or self.regions_any or (self.regions and self.regions.search(location)):
                return True, ""
        if not location:
            return (not is_remote or self.allow_remote), "location not stated"
        if self.onsite_any or (self.onsite and self.onsite.search(location)):
            return True, ""
        return False, f"location: {location[:60]}"

    def experience(self, title: str, text: str) -> tuple[str, str, float | None]:
        years = year_mentions(text)
        entry_title = bool(_ENTRY_TITLE.search(title))
        lo = min(years) if years else None
        if years and all(y > self.max_years for y in years):
            if entry_title:
                return "unclear", f"entry-level title but asks {lo:g}+ yrs", lo
            return "reject", f"needs {lo:g}+ yrs", lo
        if years and all(y <= self.max_years for y in years):
            return "pass", f"asks {lo:g}+ yrs", lo
        if years:
            return "unclear", "mixed: " + ", ".join(f"{y:g}" for y in sorted(set(years))) + " yrs", lo
        if entry_title:
            return "pass", "entry-level title", 0.0
        if _ENTRY_TEXT.search(text):
            return "pass", "entry-level wording", 0.0
        return "unclear", "years not stated", None

    def matched_skills(self, title: str, text: str) -> list[str]:
        """Your skills mentioned in the posting, spelled as in config.toml, in config order."""
        if not self.skills:
            return []
        norm = lambda s: re.sub(r"[\s\-_/]+", " ", s.lower())   # noqa: E731
        found = {norm(m.group(0)) for m in self.skills.finditer(f"{title}\n{text}")}
        return [s for s in self.skill_list if norm(s) in found or f"{norm(s)}s" in found]

    def score(self, title: str, skills: list[str], exp_status: str) -> int:
        """0-100 without AI: base 5, title area up to 30, skills found up to 40, experience
        clarity up to 20 (the remote bonus is added on top when ranking). Missing skills lower
        the rank but never drop a job."""
        s = 5
        if self.title_top and self.title_top.search(title):
            s += 30
        elif self.title_next and self.title_next.search(title):
            s += 18
        if self.skill_list:
            enough = min(8.0, max(3.0, len(self.skill_list) * 0.4))
            s += round(40 * min(1.0, len(skills) / enough))
        s += {"pass": 20, "unclear": 5}.get(exp_status, 0)
        return min(100, s)

    def classify(self, title: str, location: str, remote: bool | None, description: str) -> RuleResult:
        ok, why = self.title_ok(title)
        if not ok:
            return RuleResult("reject", why, None, 0)
        ok, why = self.location_ok(location, remote)
        if not ok:
            return RuleResult("reject", why, None, 0)
        status, why, years = self.experience(title, description)
        soft = self.soft_excluded(title)
        if soft and status != "pass":
            return RuleResult("reject", f"'{soft}' title without a fitting years requirement", years, 0)
        if status == "reject":
            return RuleResult(status, why, years, 0)
        skills = self.matched_skills(title, description)
        return RuleResult(status, why, years, self.score(title, skills, status), skills)
