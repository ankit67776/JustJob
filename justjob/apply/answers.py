"""Drafting answers to application questions.

Each answer comes from the first of these that knows it:
1. your profile (name, email, links, notice period, ...) and fixed rules for the questions most
   forms ask (work authorization, sponsorship, relocation, pay, voluntary disclosures),
2. answers you gave on earlier forms (memory.py), for the same question or one worded alike,
3. an LLM that may use only facts from your profile and resume, and must quote the fact it
   relied on. A quote that isn't actually in your profile or resume sends the answer to you.
   It may also reuse an earlier answer to a question that means the same, naming which one.

Every answer records where it came from and whether you need to look at it. Free text the LLM
wrote, statements you sign, and anything nobody could answer always go to you.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass

from .. import ai
from ..profile import DECLINE
from ..text import trim_for_ai
from .forms import Question

log = logging.getLogger(__name__)

CHOICE_KINDS = ("select", "boolean", "multiselect")


@dataclass
class Answer:
    value: str | list[str] | None
    source: str               # profile | rule | memory | ai | you | none
    needs_you: bool = False
    note: str = ""            # why the agent answered this way, or what you need to do
    ref: str = ""             # memory answers: the saved answer's key ("~key" when the question only resembles it)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Answer:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    @property
    def empty(self) -> bool:
        return self.value in (None, "", [])


def skip(note: str = "Optional; left empty.") -> Answer:
    return Answer(None, "none", False, note)


def ask_you(note: str, value=None, source: str = "none") -> Answer:
    return Answer(value, source, True, note)


# --- matching an answer to a form's options ------------------------------------------------

def norm(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+]+", " ", str(s or "").lower())).strip()


DECLINE_RE = re.compile(r"decline|don ?t wish|do not wish|prefer not|not to (answer|say|disclose|self identify)|"
                        r"rather not|choose not|not specified|i don ?t want")
SYNONYMS = {"male": ("man",), "female": ("woman",), "man": ("male",), "woman": ("female",)}


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def pick(options: list[str], wanted) -> str | None:
    """The option that means `wanted`, or None. Exact match first, then yes/no and 'decline'
    variants, then whole-word containment."""
    if not options:
        return str(wanted) if wanted not in (None, "") else None
    w = norm(wanted)
    if not w:
        return None
    by_norm = {norm(o): o for o in options}
    if w in by_norm:
        return by_norm[w]
    for alt in SYNONYMS.get(w, ()):
        if alt in by_norm:
            return by_norm[alt]
    if DECLINE_RE.search(w):
        return next((o for o in options if DECLINE_RE.search(norm(o))), None)
    first = w.split()[0]
    if first in ("yes", "no"):
        hits = [o for o in options if norm(o) == first or norm(o).startswith(first + " ")]
        if len(hits) == 1 or (hits and len(w.split()) == 1):
            return hits[0]
    hits = [o for o in options if _contains(norm(o), w) or _contains(w, norm(o))]
    return hits[0] if len(hits) == 1 else None


def fit(q: Question, value):
    """`value` the way this question takes it (an option, a list of options, or text), or None
    when the form can't take it."""
    if value in (None, "", []):
        return None
    if q.kind == "multiselect":
        values = value if isinstance(value, list) else [value]
        chosen = [c for c in (pick(q.options, v) for v in values) if c] if q.options else [str(v) for v in values]
        return chosen or None
    if isinstance(value, list):
        return None
    if q.kind in ("select", "boolean"):
        return pick(q.options or ["Yes", "No"], value)
    return str(value)


def _listed(v) -> list:
    return v if isinstance(v, list) else [] if v in (None, "") else [v]


def same(q: Question, a, b) -> bool:
    """Whether two answers to `q` say the same thing. Forms reformat what you type: Greenhouse
    shows '+919876543210' as '+91 98765 43210'."""
    if isinstance(a, list) or isinstance(b, list):
        return sorted(map(norm, _listed(a))) == sorted(map(norm, _listed(b)))
    if q.kind == "phone":
        return re.sub(r"\D", "", str(a or "")) == re.sub(r"\D", "", str(b or ""))
    if q.kind == "location":   # a city search box shows the suggestion it took: "Pune, Maharashtra, India"
        return norm(str(a or "").split(",")[0]) == norm(str(b or "").split(",")[0])
    return norm(a) == norm(b)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def pick_number(options: list[str], n: float) -> str | None:
    """The option whose range contains n: '0-2 years', '3 to 5', '5+', 'Less than 1 year', '10 or more'."""
    for o in options:
        t = norm(o)
        if m := re.search(r"(\d+(?:\.\d+)?) (?:to )?(\d+(?:\.\d+)?)\b", t):
            lo, hi = _num(m.group(1)), _num(m.group(2))
        elif m := re.search(r"(?:less than|under|fewer than|below) (\d+(?:\.\d+)?)", t):
            lo, hi = 0, _num(m.group(1)) - 1e-9
        elif m := re.search(r"(\d+(?:\.\d+)?) ?(?:\+|or more|and above|plus)|(?:more than|over|at least) (\d+(?:\.\d+)?)", t):
            lo, hi = _num(m.group(1) or m.group(2)), float("inf")
        elif m := re.fullmatch(r"(\d+(?:\.\d+)?)(?: years?)?", t):
            lo = hi = _num(m.group(1))
        else:
            continue
        if lo <= n <= hi or (lo == hi and int(n) == lo):
            return o
    return None


# --- where a job is, and where you can work --------------------------------------------------

COUNTRIES = {
    "United States": r"united states|usa|u s a|the us|the u s|america|us based|us only",
    "United Kingdom": r"united kingdom|uk|u k|england|britain|scotland|london",
    "Canada": r"canada|toronto|vancouver|montreal",
    "India": r"india|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|gurgaon|gurugram|noida|chennai|kolkata|ahmedabad",
    "Germany": r"germany|berlin|munich|hamburg", "France": r"france|paris", "Netherlands": r"netherlands|amsterdam",
    "Ireland": r"ireland|dublin", "Spain": r"spain|madrid|barcelona", "Portugal": r"portugal|lisbon",
    "Poland": r"poland|warsaw|krakow", "Singapore": r"singapore", "Australia": r"australia|sydney|melbourne",
    "Israel": r"israel|tel aviv", "Brazil": r"brazil|sao paulo", "Mexico": r"mexico", "Japan": r"japan|tokyo",
    "United Arab Emirates": r"united arab emirates|uae|dubai|abu dhabi", "Switzerland": r"switzerland|zurich",
    "Sweden": r"sweden|stockholm",
}
US_PLACES = (r"san francisco|palo alto|mountain view|menlo park|sunnyvale|san jose|new york|nyc|seattle|austin|boston|"
             r"los angeles|chicago|denver|atlanta|miami|washington dc|bay area|silicon valley|san diego|portland|"
             r"philadelphia|pittsburgh|dallas|houston|salt lake")
US_STATE = re.compile(r",\s*(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|"
                      r"T[NX]|UT|V[AT]|W[AIVY])\b")
_COUNTRY_RES = {c: re.compile(rf"\b(?:{p})\b") for c, p in COUNTRIES.items()}


def country_in(text: str | None) -> str | None:
    """The country a label or a job location names, if exactly one is clear."""
    raw = text or ""
    t = norm(raw)
    found = {c for c, rx in _COUNTRY_RES.items() if rx.search(t)}
    if US_STATE.search(raw) or re.search(rf"\b(?:{US_PLACES})\b", t):
        found.add("United States")
    return found.pop() if len(found) == 1 else None


def authorized(prof: dict) -> list[str]:
    return [country_in(c) or c for c in prof.get("authorized_countries") or []]


# --- answers straight from your profile, and the fixed rules ----------------------------------

def _p(prof: dict, key: str, label: str) -> Answer:
    value = prof.get(key)
    if value in (None, "", []):
        return ask_you(f"Add your {label} on the Profile page, or answer here.")
    return Answer(f"{value:g}" if isinstance(value, float) else str(value), "profile")


def _full_name(prof: dict) -> str:
    return " ".join(x for x in (prof.get("first_name"), prof.get("last_name")) if x)


def _city(prof: dict) -> str:
    return (prof.get("location") or "").split(",")[0].strip()


def home_country(prof: dict) -> str:
    return country_in(prof.get("location")) or (prof.get("location") or "").split(",")[-1].strip()


def _notice(prof: dict, q: Question) -> Answer:
    days = prof.get("notice_period_days")
    if days is None:
        return ask_you("Add your notice period on the Profile page, or answer here.")
    if q.kind == "select":
        choice = _pick_days(q.options, days)
        return Answer(choice, "profile", note=f"Your notice period is {days:g} days.") if choice else \
            ask_you(f"Your notice period is {days:g} days; none of the options clearly fits.")
    if q.kind == "number":
        return Answer(f"{days:g}", "profile")
    return Answer("Immediately" if days == 0 else f"{days:g} days", "profile")


def _pick_days(options: list[str], days: float) -> str | None:
    """The shortest option that still covers your notice period."""
    best = None
    for o in options:
        t = norm(o)
        if re.search(r"immediate|right away|now\b", t):
            span = 0
        elif m := re.search(r"(\d+) ?(day|week|month)", t):
            span = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        else:
            continue
        if span >= days and (best is None or span < best[0]):
            best = (span, o)
    return best[1] if best else None


def _pay(prof: dict, q: Question, job: dict, current: bool) -> Answer:
    in_dollars = bool(re.search(r"\$|usd|dollar", q.label.lower())) or country_in(job.get("location")) == "United States" \
        or "$" in (job.get("salary") or "") or "USD" in (job.get("salary") or "")
    if current:
        return _p(prof, "current_ctc", "current CTC")
    if in_dollars:
        if prof.get("expected_salary_usd"):
            return Answer(prof["expected_salary_usd"], "profile", q.kind == "number",
                          "This role pays in dollars; this is your dollar expectation.")
        return ask_you("This role pays in dollars. Add an expected dollar salary on the Profile page, or answer here.")
    ans = _p(prof, "expected_ctc", "expected CTC")
    if q.kind == "number" and not ans.empty:
        return ask_you("The form wants a plain number; enter your expected CTC as one.", ans.value, "profile")
    return ans


GENERIC_YEARS_RE = re.compile(
    r"(how many )?(total |overall )?years( of)? (total |overall |professional |relevant |work |industry |full time |"
    r"software |engineering )*experience( do you have)?( in total)?|(total |overall |professional |relevant |work )*"
    r"experience( in years)?")


def _years_generic(label: str) -> bool:
    """'How many years of experience do you have?', not 'Years of experience with Kubernetes?'"""
    return bool(GENERIC_YEARS_RE.fullmatch(norm(label)))


def _years(prof: dict, q: Question) -> Answer:
    years = prof.get("years_experience")
    if years is None:
        return ask_you("Add your years of experience on the Profile page.")
    if q.kind == "select":
        choice = pick_number(q.options, float(years))
        return Answer(choice, "profile", note=f"You have {years:g} years.") if choice else \
            ask_you(f"You have {years:g} years; none of the options clearly fits.")
    return Answer(f"{years:g}", "profile")


def _worked_here(prof: dict, job: dict) -> Answer:
    company = norm(job.get("company"))
    past = [j["company"] for j in ((prof.get("facts") or {}).get("experience") or [])]
    if not prof.get("facts"):
        return ask_you("Upload your resume so the agent can check where you've worked.")
    hit = next((c for c in past if company and (_contains(norm(c), company) or _contains(company, norm(c)))), None)
    return Answer("Yes" if hit else "No", "rule",
                  note=f"Your resume lists {hit}." if hit else "This company isn't on your resume.")


def _eeo(prof: dict, q: Question) -> Answer | None:
    t = norm(q.label + " " + q.id)
    if "hispanic" in t or "latino" in t:
        e = prof.get("ethnicity") or DECLINE
        return Answer(DECLINE if e == DECLINE else ("Yes" if e == "Hispanic or Latino" else "No"), "profile")
    for key, rx in (("gender", r"gender|sex\b"), ("ethnicity", r"race|ethnic"), ("veteran", r"veteran"),
                    ("disability", r"disabilit")):
        if re.search(rx, t):
            return Answer(prof.get(key) or DECLINE, "profile")
    return None


SIGN_RE = re.compile(r"\b(consent|i agree|agree to|agreement|acknowledge|certify|attest|arbitrat\w*|policy|"
                     r"terms (and|&) conditions|gdpr|data (processing|retention|protection)|true and (complete|accurate))\b")
AUTH_RE = re.compile(r"authori[sz]ed to work|work authori[sz]ation|eligible to work|right to work|legally (able|permitted|allowed)"
                     r" to work|permission to work|work permit")
GENERIC_PLACE_RE = re.compile(r"^(location|current location|where are you (currently )?(based|located)|"
                              r"(what is your |your )?(current )?(city and country|city country|location city)|"
                              r"where do you (currently )?live|current address)$")


def rule_answer(q: Question, prof: dict, job: dict) -> Answer | None:
    """The profile or a fixed rule's answer, or None when only the LLM (or you) can answer."""
    t, qid = norm(q.label), q.id
    if q.group == "eeo" or qid in ("gender", "race", "hispanic_ethnicity", "veteran_status", "disability_status"):
        return _eeo(prof, q) or (ask_you("A voluntary question the agent doesn't recognise.") if q.required
                                 else skip("Voluntary; left empty."))
    if SIGN_RE.search(t):
        agree = next((c for c in (pick(q.options, w) for w in ("Yes", "I agree", "I acknowledge", "Confirmed")) if c), None)
        return ask_you("A statement you sign. Read it and tick it yourself.", agree, "rule")
    if q.kind == "file":
        if qid in ("resume", "_systemfield_resume") or re.search(r"\b(resume|cv)\b", t):
            return Answer("resume", "profile") if prof.get("resume") else ask_you("Upload a resume on the Profile page.")
        if "cover letter" in t or qid == "cover_letter":
            return ask_you("This form requires a cover letter. Attach one in the browser.") if q.required else skip()
        return ask_you(f"Attach the file it asks for ({q.label}) in the browser.") if q.required else skip()

    if qid == "first_name" or re.fullmatch(r"(legal )?first name|given name", t):
        return _p(prof, "first_name", "first name")
    if qid == "last_name" or re.fullmatch(r"(legal )?(last name|surname|family name)", t):
        return _p(prof, "last_name", "last name")
    if qid in ("preferred_name",) or re.fullmatch(r"preferred (first )?name", t):
        if prof.get("preferred_name"):
            return Answer(prof["preferred_name"], "profile")
        return Answer(prof.get("first_name"), "profile") if q.required and prof.get("first_name") else skip()
    if qid == "_systemfield_name" or re.fullmatch(r"(your |full |legal |full legal )?name", t):
        name = _full_name(prof)
        return Answer(name, "profile") if name else ask_you("Add your name on the Profile page.")
    if qid in ("email", "_systemfield_email") or q.kind == "email" or re.fullmatch(r"(your )?e ?mail( address)?", t):
        return _p(prof, "email", "email")
    if qid in ("phone", "_systemfield_phone") or q.kind == "phone" or re.fullmatch(r"(your )?(phone|mobile)( number)?", t):
        return _p(prof, "phone", "phone number")
    if "linkedin" in t:
        return _p(prof, "linkedin", "LinkedIn link")
    if "github" in t:
        return _p(prof, "github", "GitHub link")
    if re.search(r"\b(website|portfolio|personal site|blog)\b", t) and q.kind in ("text", "url"):
        if prof.get("portfolio") or prof.get("github"):
            return Answer(prof.get("portfolio") or prof.get("github"), "profile")
        return skip() if not q.required else ask_you("Add a portfolio or GitHub link on the Profile page.")
    if q.kind == "location" or GENERIC_PLACE_RE.fullmatch(t):
        return _p(prof, "location", "location")
    if re.fullmatch(r"(current )?city( of residence)?", t):
        return Answer(_city(prof), "profile") if _city(prof) else ask_you("Add where you live on the Profile page.")
    if re.fullmatch(r"(current )?country( of residence)?", t):
        country = home_country(prof)
        if q.kind == "select":
            choice = pick(q.options, country)
            return Answer(choice, "profile") if choice else ask_you(f"You live in {country}; it isn't one of the options.")
        return Answer(country, "profile") if country else ask_you("Add where you live on the Profile page.")
    if re.fullmatch(r"(us )?state( province)?|province|state or province", t):
        return ask_you("Asks for a state or province, which your profile doesn't hold.")

    if AUTH_RE.search(t):
        place = country_in(q.label) or country_in(job.get("location"))
        if not place:
            return ask_you("Neither the question nor the job says which country this is about.")
        ok = place in authorized(prof)
        if not prof.get("authorized_countries"):
            return ask_you("Add the countries you can work in on the Profile page.")
        return Answer("Yes" if ok else "No", "rule", note=f"Asks about {place}; you listed "
                      f"{', '.join(prof['authorized_countries'])}.")
    if "sponsor" in t or re.search(r"\bvisa\b", t):
        place = country_in(q.label) or country_in(job.get("location"))
        if not prof.get("authorized_countries"):
            return ask_you("Add the countries you can work in on the Profile page.", prof.get("sponsorship_needed") or None,
                           "rule")
        if place and place in authorized(prof):
            return Answer("No", "rule", note=f"You can work in {place} without a visa.")
        need = prof.get("sponsorship_needed") or None
        if not need:
            return ask_you("Set whether you need visa sponsorship on the Profile page.")
        if not place:
            return ask_you("Couldn't tell which country this job is in; check this one.", need, "rule")
        return Answer(need, "rule", note=f"{place} isn't one of the countries you can work in.")
    if "relocat" in t and (q.kind in ("select", "boolean") or re.match(r"(are|would) you (be )?(open|willing) to relocat", t)):
        return _p(prof, "relocation", "answer about relocating")
    if re.search(r"notice period|how soon can you (start|join)|when can you (start|join)|earliest (start|joining)|"
                 r"available to start|availability to (start|join)|start date|joining date", t):
        if prof.get("earliest_start") and q.kind in ("text", "date") and "notice" not in t:
            return Answer(prof["earliest_start"], "profile")
        return _notice(prof, q)
    if re.search(r"current (ctc|compensation|salary|package)|present (ctc|salary)", t):
        return _pay(prof, q, job, current=True)
    if re.search(r"expected (ctc|compensation|salary|pay)|salary expectation|compensation expectation|desired "
                 r"(salary|compensation|pay)|salary requirement|(salary|pay) range are you", t):
        return _pay(prof, q, job, current=False)
    if q.kind in ("text", "number", "select") and _years_generic(q.label):
        return _years(prof, q)
    if re.search(r"how did you (hear|find|learn)|where did you (hear|find|see)|how did you come across|referral source"
                 r"|^source$", t):
        how = prof.get("how_heard") or "Company careers page"
        if q.kind in ("select", "multiselect"):
            for guess in (how, "Company website", "Careers page", "Company careers page", "Website", "Job board", "Other"):
                if choice := pick(q.options, guess):
                    return Answer([choice] if q.kind == "multiselect" else choice, "profile")
            return ask_you("None of the options match how you found this job.") if q.required else skip()
        return Answer(how, "profile")
    if re.search(r"\b18\b|eighteen", t) and re.search(r"age|old|years", t):
        return _p(prof, "over_18", "answer about being 18 or older")
    if q.kind == "text" and (re.search(r"\b(current|present|most recent) (company|employer|firm|organi[sz]ation)\b", t)
                             or re.fullmatch(r"(current )?employer", t)):
        return _p(prof, "current_company", "current company")
    if re.fullmatch(r"(current|most recent|present) (job )?(title|role|position)|(current )?job title", t):
        return _p(prof, "current_title", "current title")
    if "pronoun" in t:
        return Answer(prof["pronouns"], "profile") if prof.get("pronouns") else skip()
    if re.search(r"(worked|employed) (at|for|by) .{0,40}(before|previously|in the past)|previously (worked|been employed)"
                 r"|former employee|ever been employed", t):
        return _worked_here(prof, job)
    if not q.required and q.kind in ("text", "textarea", "url", "date"):
        return skip()
    return None


# --- the LLM -----------------------------------------------------------------------------

SYSTEM = """You fill in job application questions for one candidate and return strict JSON.
Use only the facts under CANDIDATE and RESUME, and the candidate's own PAST ANSWERS. Never invent
employers, titles, dates, numbers, degrees, skills, links or experience.

For every question output:
{"id": "<id exactly as given>", "value": <answer or null>, "confidence": "high" | "medium" | "low",
 "fact": "<the candidate fact you relied on, copied word for word>" or null,
 "past": "<id of the PAST ANSWER you reused>" or null, "needs_human": true | false}
Return {"answers": [ ... ]} with one object per question and nothing else.

Rules:
- select and boolean: value must be exactly one of the listed options. multiselect: a list of listed options.
- number: digits only.
- PAST ANSWERS are the candidate's own answers on earlier forms. When a question asks the same thing as one
  of them, even in other words, reuse that answer (mapped to this question's options) and give its id in
  past. Only when the meaning is the same: a question on a related topic doesn't count.
- If an honest answer needs information that isn't in the candidate facts, use null and needs_human true.
  Don't guess, and don't assume the candidate lacks something just because it isn't listed.
- Experience with a specific tool or field: answer from the resume only when it shows that tool or field.
- textarea (open questions like "Why do you want to work here?"): a specific first-person draft of at most
  120 words built from the resume facts and the job description. No clichés, nothing beyond the facts.
  needs_human true.
- text: at most 200 characters."""


def candidate_block(prof: dict) -> str:
    lines = [f"{k}: {v}" for k, v in (
        ("Name", _full_name(prof)), ("Lives in", prof.get("location")),
        ("Years of experience", prof.get("years_experience")), ("Current title", prof.get("current_title")),
        ("Current company", prof.get("current_company")),
        ("Can work without a visa in", ", ".join(prof.get("authorized_countries") or [])),
        ("Needs visa sponsorship elsewhere", prof.get("sponsorship_needed")),
        ("Notice period (days)", prof.get("notice_period_days")), ("Open to relocating", prof.get("relocation")),
        ("Skills", ", ".join(prof.get("skills") or [])), ("Summary", prof.get("summary")),
        ("LinkedIn", prof.get("linkedin")), ("GitHub", prof.get("github")), ("Portfolio", prof.get("portfolio")),
    ) if v not in (None, "", [])]
    return "\n".join(lines)


def resume_block(prof: dict, limit: int = 6000) -> str:
    facts = prof.get("facts")
    if not facts:
        return (prof.get("resume_text") or "")[:limit]
    out = []
    for j in facts.get("experience") or []:
        out.append(f"- {j['title']} at {j['company']} ({j.get('start') or '?'} to {j.get('end') or '?'})"
                   + ("; internship" if j.get("internship") else ""))
        out += [f"    * {h}" for h in j.get("highlights") or []]
    for s in facts.get("education") or []:
        out.append(f"- {', '.join(x for x in (s.get('degree'), s.get('field')) if x)} at {s['school']}"
                   f" ({s.get('start') or '?'} to {s.get('end') or '?'})")
    for p in facts.get("projects") or []:
        out.append(f"- Project {p['name']}: {p.get('summary') or ''} [{', '.join(p.get('tech') or [])}]")
    if facts.get("certifications"):
        out.append("- Certifications: " + "; ".join(facts["certifications"]))
    if facts.get("skills"):
        out.append("- Skills on resume: " + ", ".join(facts["skills"]))
    return "\n".join(out)[:limit]


def build_messages(questions: list[Question], prof: dict, job: dict, past: list[dict] = ()
                   ) -> tuple[list[dict], dict[str, Question], dict[str, dict]]:
    by_local = {f"q{i}": q for i, q in enumerate(questions, 1)}
    by_past = {f"p{i}": row for i, row in enumerate(past, 1)}
    qs = []
    for local, q in by_local.items():
        line = f"{local} | {q.kind}{' | required' if q.required else ''} | {q.label}"
        if q.options:
            line += f" | options: {json.dumps(q.options, ensure_ascii=False)}"
        if q.description:
            line += f" | note: {q.description[:200]}"
        qs.append(line)
    old = "".join(f"\n{pid} | {row['label'][:300]} | {json.dumps(row['value'], ensure_ascii=False)[:400]}"
                  for pid, row in by_past.items())
    user = (f"CANDIDATE\n{candidate_block(prof)}\n\nRESUME\n{resume_block(prof)}\n\n"
            + (f"PAST ANSWERS (id | question | answer){old}\n\n" if old else "")
            + f"JOB\n{job.get('title')} at {job.get('company')} ({job.get('location') or 'location n/a'})\n"
            f"{trim_for_ai(job.get('description') or '', 2500)}\n\nQUESTIONS (id | type | label | options)\n"
            + "\n".join(qs))
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], by_local, by_past


def grounded(fact: str | None, corpus: str) -> bool:
    """Whether the quoted fact really is in the candidate's profile or resume (most of its words, in any order)."""
    words = [w for w in norm(fact).split() if len(w) > 2]
    if not words:
        return False
    have = set(norm(corpus).split())
    return sum(w in have for w in words) / len(words) >= 0.7


def reused(q: Question, item: dict, by_past: dict[str, dict]) -> Answer | None:
    """The LLM's answer when it reused one of your past answers, and really used that answer's value."""
    row = by_past.get(str(item.get("past") or "").strip())
    if not row:
        return None
    want, got = fit(q, row["value"]), fit(q, item.get("value"))
    if want is None or got is None or not same(q, want, got):
        return None
    where = f" on the {row['company']} form" if row.get("company") else ""
    return Answer(want, "memory", True, f'Reused your answer{where} to a question that means the same: '
                                        f'"{row["label"][:90]}". Check it fits.', "~" + row["key"])


def check_ai(q: Question, item: dict, corpus: str, by_past: dict[str, dict] | None = None) -> Answer:
    """Validate one LLM answer against the question and the candidate's facts."""
    if by_past and (a := reused(q, item, by_past)):
        return a
    value, fact = item.get("value"), (item.get("fact") or None)
    confident = item.get("confidence") in ("high", "medium") and not item.get("needs_human")
    is_grounded = grounded(fact, corpus)
    note = f'AI, based on: "{fact[:160]}"' if fact and is_grounded else "AI draft"
    if value in (None, "", []):
        return ask_you("The AI didn't find this in your profile or resume.") if q.required else skip(
            "Optional, and not covered by your profile; left empty.")
    if q.kind in ("select", "boolean"):
        choice = pick(q.options or ["Yes", "No"], value)
        if not choice:
            return ask_you(f"The AI's answer ({str(value)[:60]}) isn't one of the options.")
        return Answer(choice, "ai", not (confident and is_grounded), note if is_grounded else "AI guess with no supporting fact")
    if q.kind == "multiselect":
        values = value if isinstance(value, list) else [value]
        chosen = [c for c in (pick(q.options, v) for v in values) if c]
        if not chosen:
            return ask_you("None of the AI's choices are options on the form.")
        return Answer(chosen, "ai", not (confident and is_grounded), note)
    if q.kind == "number":
        m = re.search(r"\d+(?:\.\d+)?", str(value))
        if not m:
            return ask_you("The AI's answer isn't a number.")
        return Answer(m.group(0), "ai", not (confident and is_grounded), note)
    return Answer(str(value)[:2000 if q.kind == "textarea" else 300], "ai", True,
                  "AI draft; read and edit it before you submit.")


def ask_llm(questions: list[Question], prof: dict, job: dict, cfg: dict, past: list[dict] = ()) -> dict[str, Answer]:
    provs = ai.providers(cfg)
    if not provs or not questions:
        return {}
    messages, by_local, by_past = build_messages(questions, prof, job, past)
    corpus = candidate_block(prof) + "\n" + resume_block(prof, 20000) + "\n" + (prof.get("resume_text") or "")
    for p in provs:
        for attempt in (1, 2):
            try:
                raw = ai.chat(p, messages)
            except Exception as e:
                log.warning("apply: %s failed drafting answers: %s", p.name, e)
                break
            try:
                data = ai.extract_json(raw)
                items = data.get("answers") if isinstance(data, dict) else data
                if not isinstance(items, list):
                    raise ValueError("no answers list")
            except ValueError as e:
                log.warning("apply: %s returned unusable answers (attempt %d): %s", p.name, attempt, e)
                continue
            out = {}
            for item in items:
                if isinstance(item, dict) and (q := by_local.get(str(item.get("id", "")).strip())):
                    out[q.id] = check_ai(q, item, corpus, by_past)
            return out
    return {}


def draft(questions: list[Question], prof: dict, job: dict, cfg: dict, db) -> dict[str, Answer]:
    """An answer for every question, keyed by question id."""
    from . import memory  # memory.py builds on this module

    saved = db.memory_all()
    answers: dict[str, Answer] = {}
    for_llm = []
    for q in questions:
        a = rule_answer(q, prof, job)
        # Your earlier answer fills what the rules couldn't, and wins where you corrected a rule.
        if a is None or (a.needs_you and a.empty) or memory.overrides(q, saved):
            a = memory.recall(q, saved, cfg, job) or a
        if a is None:
            for_llm.append(q)
            continue
        if q.kind in CHOICE_KINDS and not a.empty and q.options:
            if q.kind == "multiselect":
                a.value = [c for c in (pick(q.options, v) for v in (a.value if isinstance(a.value, list) else [a.value])) if c] or None
            else:
                choice = pick(q.options, a.value)
                if not choice:
                    a = ask_you(f"Your answer ({a.value}) isn't one of the options.")
                else:
                    a.value = choice
        answers[q.id] = a
    llm = ask_llm(for_llm, prof, job, cfg, memory.related(saved, for_llm))
    why = "The AI couldn't answer this one." if ai.providers(cfg) else "No AI key is set, so this one is yours."
    for q in for_llm:
        answers[q.id] = llm.get(q.id) or (ask_you(why) if q.required else skip())
    return answers
