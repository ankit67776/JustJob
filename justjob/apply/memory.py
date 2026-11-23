"""How the apply agent learns from you.

When you press Submit, the browser step reads back what every field finally held (browser.py).
Whatever you typed, picked or changed is saved here, keyed by the question's wording and its
options, and the next form that asks the same question gets your answer:

- It comes back outlined in orange for you to check until you've given or kept that answer
  `trust_after` times ([apply] in config.toml, 2 by default). After that it fills without asking.
- A question worded almost the same way gets it too, always outlined. Questions that mean the
  same in other words are matched by the LLM (answers.py), which must name the saved answer it
  reused; that answer's value is what goes in, never the LLM's own.
- Written answers are only ever drafts for you to read, and are flagged when they name the
  company they were written for.
- When you change an answer that came from your profile or a rule, your answer wins for that
  question from then on.
- Questions whose answer depends on where the job is (work authorization, visas) aren't saved:
  answering one adds the country to your profile instead. Statements you sign and voluntary
  disclosures are never saved.

The dashboard's Answers page lists what was saved, and lets you edit or delete it.
"""
from __future__ import annotations

import re

from ..db import DB
from .answers import AUTH_RE, CHOICE_KINDS, SIGN_RE, Answer, authorized, country_in, fit, norm, same
from .forms import Question

DEFAULT_TRUST = 2
SIMILAR = 0.6       # share of meaningful words two questions must have in common to count as the same
MISSING = object()  # a field the browser couldn't read back

STOP = {"the", "and", "you", "your", "are", "for", "with", "this", "that", "please", "our", "any", "from", "into",
        "its", "can", "may", "does", "did", "has", "have", "had", "will", "would", "what", "which", "who", "how"}


def trust_after(cfg: dict) -> int:
    try:
        return max(1, int(cfg.get("apply", {}).get("trust_after", DEFAULT_TRUST)))
    except (TypeError, ValueError):
        return DEFAULT_TRUST


def key(q: Question) -> str:
    return norm(q.label) + "|" + "|".join(sorted(norm(o) for o in q.options))


def about_place(q: Question) -> bool:
    """Work authorization and visa questions: the right answer depends on the job's country."""
    t = norm(q.label)
    return bool(AUTH_RE.search(t) or re.search(r"\bsponsor|\bvisa\b", t))


def learnable(q: Question) -> bool:
    """Whether an answer to this question is worth reusing on other forms."""
    if q.group != "custom" or q.kind in ("file", "location", "date"):
        return False
    return not (SIGN_RE.search(norm(q.label)) or about_place(q))


def _family(kind: str) -> str:
    return "choice" if kind in CHOICE_KINDS else "text"


def words(text: str) -> set[str]:
    out = set()
    for w in norm(text).split():
        if len(w) < 3 or w in STOP:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > len(suffix) + 3 and w.endswith(suffix):
                w = w[:-len(suffix)]
                break
        out.add(w)
    return out


def similarity(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


# --- using what was saved ------------------------------------------------------------------

def _index(saved: list[dict]) -> dict[str, dict]:
    return {row["key"]: row for row in saved}


def overrides(q: Question, saved: list[dict]) -> bool:
    """Whether you corrected the profile's or a rule's answer to this exact question before."""
    row = _index(saved).get(key(q)) if learnable(q) else None
    return bool(row and row["overrides"])


def names_company(value, written_for: str, company: str) -> str | None:
    """The other company a saved written answer names, if any."""
    if not isinstance(value, str) or not written_for or norm(written_for) == norm(company):
        return None
    return written_for if f" {norm(written_for)} " in f" {norm(value)} " else None


def recall(q: Question, saved: list[dict], cfg: dict, job: dict) -> Answer | None:
    """Your saved answer to this question, or to one worded almost the same way."""
    if not learnable(q):
        return None
    k = key(q)
    row = _index(saved).get(k)
    if row and (value := fit(q, row["value"])) is not None:
        return _from_row(q, value, row, job, trust_after(cfg))
    best, score = None, SIMILAR
    for r in saved:
        if r["key"] == k or _family(r["kind"]) != _family(q.kind) or fit(q, r["value"]) is None:
            continue
        s = similarity(q.label, r["label"])
        if s >= score:
            best, score = r, s
    if not best:
        return None
    where = f" on the {best['company']} form" if best["company"] else ""
    return Answer(fit(q, best["value"]), "memory", True,
                  f'Your answer{where} to a similar question: "{best["label"][:90]}". Check it fits.', "~" + best["key"])


def _from_row(q: Question, value, row: dict, job: dict, trust: int) -> Answer:
    where = f" on the {row['company']} form" if row["company"] else " on an earlier form"
    if other := names_company(value, row["company"], job.get("company") or ""):
        return Answer(value, "memory", True, f"Your answer{where}. It names {other}: edit it for this company.", row["key"])
    if q.kind == "textarea":
        return Answer(value, "memory", True, f"Your answer{where}. Read it again for this job.", row["key"])
    left = trust - row["confirmed"]
    if left > 0:
        more = "once more" if left == 1 else f"{left} more times"
        return Answer(value, "memory", True, f"Your answer{where}. Keep it {more} and it fills without asking.", row["key"])
    return Answer(value, "memory", False, f"Your answer on {row['confirmed']} earlier forms.", row["key"])


def related(saved: list[dict], questions: list[Question], limit: int = 25) -> list[dict]:
    """The saved answers most likely to match these questions, for the LLM to consider."""
    ask = [q for q in questions if learnable(q)]
    if not ask or not saved:
        return []
    scored = sorted(saved, key=lambda r: max(similarity(q.label, r["label"]) for q in ask), reverse=True)
    return scored[:limit]


# --- learning from a submitted form ----------------------------------------------------------

def read_back(q: Question, raw) -> object:
    """What a field held at Submit, from the browser's reading of it (a list of texts), or MISSING."""
    if raw is None:
        return MISSING
    texts = [t.strip() for t in raw if isinstance(t, str) and t.strip()]
    if q.kind == "multiselect":
        if not texts:
            return []
        chosen = fit(q, texts)
        return chosen if chosen else MISSING
    if not texts:
        return ""
    if q.kind in ("select", "boolean"):
        return next((c for c in (fit(q, t) for t in texts) if c), MISSING)
    return texts[0]


AUTH_Q = re.compile(r"^(are|is) you (currently |presently )?(legally )?(authori[sz]ed|eligible|permitted|allowed) to work\b")
SPONSOR_Q = re.compile(r"^(will|do|would) you (now or in the future |now |currently |in the future |ever )?(require|need)\b")


def teach_profile(prof: dict, q: Question, value, job: dict) -> str | None:
    """Work authorization answers teach your profile which countries you can work in."""
    t = norm(q.label)
    first = (norm(value).split() or [""])[0]
    place = country_in(q.label) or country_in(job.get("location"))
    if first not in ("yes", "no") or not place:
        return None
    if AUTH_Q.match(t) and not re.search(r"sponsor|visa", t) and first == "yes" and place not in authorized(prof):
        prof["authorized_countries"] = [*(prof.get("authorized_countries") or []), place]
        return f"You can work in {place} without a visa."
    if SPONSOR_Q.match(t) and "sponsor" in t and place not in authorized(prof) and not prof.get("sponsorship_needed"):
        prof["sponsorship_needed"] = "Yes" if first == "yes" else "No"
        return f"You {'need' if first == 'yes' else 'do not need'} visa sponsorship outside the countries you listed."
    return None


def learn(db: DB, job: dict, questions: list[Question], answers: dict[str, Answer], final: dict, now: str,
          prof: dict | None = None) -> dict:
    """Save what you gave or kept on a submitted form. `final` maps question ids to the browser's
    reading of each field. Returns counts for the application's report:
    kept (the agent's answer went in unchanged), changed, added (the agent had none), what was saved."""
    counts = {"kept": 0, "changed": 0, "added": 0}
    saved, taught = [], []
    stored = db.get_profile()
    stored = dict(stored if stored is not None else prof or {})
    for q in questions:
        if q.kind == "file" or q.group == "eeo":
            continue
        a = answers.get(q.id)
        drafted = None if a is None or a.empty else a.value
        got = read_back(q, final.get(q.id)) if q.id in final else MISSING
        if got is MISSING and a is not None and a.source == "you":
            got = drafted if drafted is not None else MISSING   # your edit on the review page
        if got is MISSING or got in ("", []):
            continue   # unreadable, or left empty; a cleared field can't be told from an unreadable one
        if drafted is None:
            counts["added"] += 1
        elif a.source == "you" or not same(q, drafted, got):
            counts["changed"] += 1
        else:
            counts["kept"] += 1
        if about_place(q):
            if note := teach_profile(stored, q, got, job):
                taught.append(note)
            continue
        if not learnable(q):
            continue
        if drafted is not None and a.source in ("profile", "rule", "ai") and not a.needs_you and same(q, drafted, got):
            continue   # the agent knew this already
        k = key(q)
        old = db.memory_get(k)
        kept_old = bool(old) and same(q, old["value"], got)
        corrected = drafted is not None and a.source in ("profile", "rule") and not same(q, drafted, got)
        db.memory_save(k, q.label, q.kind, q.options, got, old["confirmed"] + 1 if kept_old else 1,
                       corrected or (kept_old and bool(old["overrides"])), job.get("company") or "", job.get("id") or "", now)
        saved.append(q.label)
    if taught:
        db.save_profile(stored, now)
    return {**counts, "fields": sum(counts.values()), "saved": saved, "profile": taught}
