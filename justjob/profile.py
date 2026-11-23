"""Your profile: what the filters, the AI and the apply agent know about you.

It's one JSON document in the database, edited on the dashboard's Profile page. Until that
page is saved once, it's seeded from config.toml's [profile] section. After that the page
wins: the fields the filters and the AI read (skills, years, summary, ...) are copied over
cfg["profile"] at startup, so `run`, `refilter` and `analyze` use your edits.

An uploaded resume is kept in data/resume/. Its text is extracted here (PDFs need the optional
`pypdf` package), and an LLM turns it into structured facts: jobs, schooling, projects. Later
steps may only cite those facts, so the parser is told to copy, never to infer, and the
numbers that matter (years of experience) are computed from the dates rather than trusted.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from html import unescape
from pathlib import Path

from . import ai
from .config import resume_text
from .db import DB
from .text import now_iso

log = logging.getLogger(__name__)

DECLINE = "Decline to answer"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"          # text | email | tel | url | number | textarea | list | select
    help: str = ""
    required: bool = False      # needed before the apply agent can fill in a form for you
    options: tuple[str, ...] = ()
    placeholder: str = ""
    wide: bool = False
    autocomplete: str = ""


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    blurb: str
    fields: tuple[Field, ...]


SECTIONS = (
    Section("basics", "About you", "Your name and how employers reach you.", (
        Field("first_name", "First name", required=True, autocomplete="given-name"),
        Field("last_name", "Last name", required=True, autocomplete="family-name"),
        Field("preferred_name", "Preferred name", help="Only if it differs from your first name.", autocomplete="nickname"),
        Field("pronouns", "Pronouns", placeholder="Optional"),
        Field("email", "Email", "email", required=True, autocomplete="email"),
        Field("phone", "Phone", "tel", required=True, placeholder="e.g. +91 98765 43210", autocomplete="tel"),
        Field("location", "Where you live", required=True, placeholder="e.g. Bengaluru, India", wide=True),
    )),
    Section("links", "Links", "Most forms ask for LinkedIn. GitHub and a portfolio help for engineering roles.", (
        Field("linkedin", "LinkedIn", "url", required=True, placeholder="https://www.linkedin.com/in/…"),
        Field("github", "GitHub", "url", placeholder="https://github.com/…"),
        Field("portfolio", "Portfolio or website", "url", wide=True),
    )),
    Section("experience", "Experience and skills",
            "The AI reads these to score every job, and the rules rank jobs by how many of your skills they mention.", (
        Field("current_title", "Current title", placeholder="e.g. AI Engineer", autocomplete="organization-title"),
        Field("current_company", "Current company", autocomplete="organization"),
        Field("years_experience", "Years of experience", "number", required=True,
              help="Full-time work only. Reading your resume fills this from your job dates."),
        Field("summary", "Summary", "textarea", help="Two or three sentences about what you do.", wide=True),
        Field("skills", "Skills", "list", help="Separate with commas.", wide=True),
        Field("target_roles", "Roles you want", "list", help="Separate with commas.", wide=True),
    )),
    Section("search", "What you're looking for", "The filters and the AI use these to decide what reaches your list.", (
        Field("max_years_required", "Hide jobs that need more than", "number", help="Years of experience."),
        Field("relocation", "Open to relocating?", "select", options=("Yes", "No")),
        Field("preferences", "Priorities", "textarea", help="What should rank first, in your words.", wide=True),
        Field("avoid", "Roles to avoid", "list", help="Separate with commas.", wide=True),
    )),
    Section("pay", "Notice period and pay", "Application forms ask for these often.", (
        Field("notice_period_days", "Notice period (days)", "number", required=True, help="0 if you can join now."),
        Field("earliest_start", "Earliest start date", placeholder="e.g. Immediately"),
        Field("current_ctc", "Current CTC", placeholder="e.g. ₹18 LPA"),
        Field("expected_ctc", "Expected CTC", required=True, placeholder="e.g. ₹28 LPA"),
        Field("expected_salary_usd", "Expected salary for roles paid in dollars", placeholder="e.g. $90,000 a year", wide=True,
              help="Leave empty and the agent will ask you each time."),
    )),
    Section("authorization", "Work authorization", "Lets the agent answer the sponsorship questions most forms ask.", (
        Field("authorized_countries", "Countries you can work in without a visa", "list", required=True,
              placeholder="e.g. India"),
        Field("sponsorship_needed", "Need visa sponsorship to work anywhere else?", "select", required=True,
              options=("Yes", "No")),
    )),
    Section("eeo", "Voluntary disclosures",
            "US employers ask these on an optional form that is kept apart from your application. "
            "The agent sends exactly what you choose here.", (
        Field("gender", "Gender", "select", options=(DECLINE, "Male", "Female", "Non-binary")),
        Field("ethnicity", "Race or ethnicity", "select", options=(
            DECLINE, "Asian", "Black or African American", "Hispanic or Latino", "White",
            "Native Hawaiian or Other Pacific Islander", "American Indian or Alaska Native", "Two or more races")),
        Field("veteran", "Veteran status", "select", options=(DECLINE, "I am not a protected veteran",
                                                              "I am a protected veteran")),
        Field("disability", "Disability", "select", options=(DECLINE, "No, I don't have a disability",
                                                             "Yes, I have a disability")),
    )),
    Section("common", "Common questions", "Short answers the agent can reuse on any form.", (
        Field("how_heard", "How did you hear about us?", placeholder="e.g. Company careers page"),
        Field("over_18", "Are you 18 or older?", "select", options=("Yes", "No")),
    )),
)
FIELDS = {f.key: f for s in SECTIONS for f in s.fields}

# Fields copied over cfg["profile"], where the filters and the AI step read them.
CFG_KEYS = ("summary", "years_experience", "max_years_required", "skills", "target_roles", "preferences", "avoid")
# Fields the rule filter uses; changing one means stored jobs must be filtered again.
RULE_KEYS = ("max_years_required", "skills")

RESUME_KINDS = {".pdf": "PDF", ".docx": "DOCX", ".txt": "TXT", ".md": "TXT"}
MAX_RESUME_BYTES = 8 * 1024 * 1024
MAX_RESUME_CHARS = 30_000


class ResumeError(ValueError):
    """A problem worth showing the user as is."""


# --- storage -----------------------------------------------------------------------------

def _empty(f: Field):
    return [] if f.kind == "list" else (None if f.kind == "number" else "")


def defaults(cfg: dict) -> dict:
    """A new profile: config.toml's [profile] values, and 'decline' for the voluntary questions."""
    out = {f.key: _empty(f) for f in FIELDS.values()}
    seed = cfg.get("profile", {})
    for key in CFG_KEYS:
        if seed.get(key) not in (None, ""):
            out[key] = list(seed[key]) if isinstance(seed[key], list) else seed[key]
    for f in FIELDS.values():
        if f.options and f.options[0] == DECLINE:
            out[f.key] = DECLINE
    out["how_heard"] = "Company careers page"
    out.update(resume=None, resume_text=resume_text(cfg, MAX_RESUME_CHARS), facts=None, facts_at=None, facts_model=None)
    return out


def load(db: DB, cfg: dict) -> dict:
    """The saved profile, with defaults for any field added since it was saved."""
    prof = defaults(cfg)
    prof.update(db.get_profile() or {})
    return prof


def save(db: DB, prof: dict) -> None:
    db.save_profile(prof, now_iso())


def apply_to_cfg(cfg: dict, prof: dict) -> None:
    """Copy the profile over cfg["profile"], so the filters and the AI step see the page's values."""
    target = cfg.setdefault("profile", {})
    for key in CFG_KEYS:
        if prof.get(key) not in (None, ""):
            target[key] = prof[key]
    if prof.get("resume_text"):
        target["resume_text"] = prof["resume_text"]


def rules_changed(old: dict, new: dict) -> bool:
    return any(old.get(k) != new.get(k) for k in RULE_KEYS)


def missing(prof: dict) -> list[tuple[str, str]]:
    """(anchor id, label) for everything the apply agent needs that's still empty."""
    out = [] if prof.get("resume") else [("resume", "Resume")]
    out += [(f"f-{f.key}", f.label) for f in FIELDS.values() if f.required and prof.get(f.key) in (None, "", [])]
    return out


def required_count() -> int:
    return 1 + sum(f.required for f in FIELDS.values())   # + the resume


# --- the Profile form --------------------------------------------------------------------

def split_list(raw: str) -> list[str]:
    """'python, React;  SQL\\nDocker' -> ['python', 'React', 'SQL', 'Docker'], without repeats."""
    out, seen = [], set()
    for part in re.split(r"[,;\n]", raw):
        item = re.sub(r"\s+", " ", part).strip()[:80]
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out[:80]


def _number(raw: str):
    n = float(raw.replace(",", ""))
    if not 0 <= n <= 1000:
        raise ValueError(raw)
    return int(n) if n.is_integer() else round(n, 2)


def from_form(form: dict[str, list[str]], current: dict) -> tuple[dict, list[str]]:
    """The profile with the form's values. Returns (profile, keys of fields that were invalid
    and kept their old value)."""
    out, bad = dict(current), []
    for f in FIELDS.values():
        if f.key not in form:
            continue   # not on the submitted form: keep what's stored
        raw = form[f.key][0].strip()
        if f.kind == "number":
            try:
                out[f.key] = _number(raw) if raw else None
            except ValueError:
                bad.append(f.key)
        elif f.kind == "list":
            out[f.key] = split_list(raw)
        elif f.kind == "select":
            out[f.key] = raw if raw in f.options else ""
        elif f.kind == "email" and raw and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", raw):
            bad.append(f.key)
        elif f.kind == "url" and raw and not re.match(r"https?://", raw, re.I):
            out[f.key] = "https://" + raw[:500]
        else:
            out[f.key] = raw[:4000 if f.kind == "textarea" else 500]
    return out, bad


# --- resume file -------------------------------------------------------------------------

def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ResumeError("Reading PDFs needs the pypdf package: pip install -r requirements-extras.txt. "
                          "Or upload a DOCX or TXT version.") from None
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    except Exception as e:   # pypdf raises many kinds of errors for damaged files
        raise ResumeError(f"Couldn't read that PDF ({type(e).__name__}). Try exporting it again, or upload a DOCX.") from None


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        raise ResumeError("That doesn't look like a Word .docx file.") from None
    xml = re.sub(r"</w:p>|<w:br/>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return unescape(re.sub(r"<[^>]+>", "", xml))


def extract_text(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext not in RESUME_KINDS:
        raise ResumeError("Upload a PDF, DOCX or TXT file.")
    text = (_pdf_text(data) if ext == ".pdf" else _docx_text(data) if ext == ".docx"
            else data.decode("utf-8", "replace"))
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def resume_path(data_dir: Path, prof: dict) -> Path | None:
    r = prof.get("resume")
    path = data_dir / "resume" / r["file"] if r else None
    return path if path and path.exists() else None


def store_resume(db: DB, cfg: dict, data_dir: Path, name: str, data: bytes) -> dict:
    """Save an uploaded resume and its text. The facts read from the old one are dropped."""
    if not data:
        raise ResumeError("That file is empty.")
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeError("That file is over 8 MB.")
    text = extract_text(name, data)
    if len(text) < 200:
        raise ResumeError("Found almost no text in that file. If it's a scanned PDF, upload a DOCX or TXT version.")
    ext = Path(name).suffix.lower()
    folder = data_dir / "resume"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("resume.*"):
        old.unlink()
    (folder / f"resume{ext}").write_bytes(data)
    prof = load(db, cfg)
    prof["resume"] = {"file": f"resume{ext}", "name": Path(name).name[:200], "size": len(data), "uploaded_at": now_iso()}
    prof["resume_text"] = text[:MAX_RESUME_CHARS]
    prof.update(facts=None, facts_at=None, facts_model=None)
    save(db, prof)
    return prof


# --- reading the resume with an LLM ------------------------------------------------------

PARSE_SYSTEM = """You turn one resume into JSON. Copy facts exactly as the resume states them.
Never infer, embellish or fill gaps: if something isn't in the resume, use null (or [] for lists).

Return one JSON object with exactly these keys:
{"first_name": string|null, "last_name": string|null, "email": string|null, "phone": string|null,
 "location": "City, Country" or null,
 "linkedin": url|null, "github": url|null, "portfolio": url|null,
 "summary": string|null,
 "skills": [string],
 "experience": [{"title": string, "company": string, "location": string|null,
                 "start": "YYYY-MM"|null, "end": "YYYY-MM"|"Present"|null,
                 "internship": true|false, "highlights": [string]}],
 "education": [{"school": string, "degree": string|null, "field": string|null,
                "start": "YYYY"|null, "end": "YYYY"|null, "grade": string|null}],
 "projects": [{"name": string, "summary": string|null, "tech": [string], "url": string|null}],
 "certifications": [string]}

Rules:
- summary: two sentences in the first person, using only facts from the resume.
- skills: languages, frameworks, libraries, tools and techniques named anywhere in the resume, in short canonical
  form ("PyTorch", "FastAPI", "RAG"). No soft skills.
- experience: newest first. internship is true for internships, trainee and apprentice roles.
- highlights: the role's bullet points, lightly shortened, keeping every number and name.
- Dates: use "Present" only when the resume says the role is current."""


def _s(v, limit: int = 300) -> str | None:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s[:limit] if s and s.lower() not in ("null", "none", "n/a", "unknown") else None


def _strs(v, limit: int = 80, cap: int = 60) -> list[str]:
    out, seen = [], set()
    for item in v if isinstance(v, list) else []:
        s = _s(item, limit)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:cap]


def _year(v) -> str | None:
    m = re.search(r"\d{4}", _s(v, 40) or "")
    return m.group(0) if m else None


def _month(v) -> str | None:
    s = _s(v, 20)
    if not s:
        return None
    if s.lower() in ("present", "current", "now"):
        return "Present"
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?", s)
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m.group(2) and 1 <= int(m.group(2)) <= 12 else m.group(1)


def _url(v) -> str | None:
    s = _s(v, 500)
    if not s or " " in s or "." not in s:
        return None
    return s if re.match(r"https?://", s, re.I) else "https://" + s


def clean_facts(data) -> dict:
    """The parser's JSON with every value type-checked; anything malformed becomes null or is dropped."""
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    d = data

    def dicts(key: str) -> list[dict]:
        items = d.get(key)
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []

    jobs = []
    for x in dicts("experience"):
        if _s(x.get("title")) and _s(x.get("company")):
            jobs.append({"title": _s(x["title"], 120), "company": _s(x["company"], 120),
                         "location": _s(x.get("location"), 120), "start": _month(x.get("start")),
                         "end": _month(x.get("end")), "internship": x.get("internship") is True,
                         "highlights": _strs(x.get("highlights"), 400, 12)})
    schools = []
    for x in dicts("education"):
        if _s(x.get("school")):
            schools.append({"school": _s(x["school"], 160), "degree": _s(x.get("degree"), 120),
                            "field": _s(x.get("field"), 120), "start": _year(x.get("start")),
                            "end": _year(x.get("end")), "grade": _s(x.get("grade"), 40)})
    projects = []
    for x in dicts("projects"):
        if isinstance(x, dict) and _s(x.get("name")):
            projects.append({"name": _s(x["name"], 120), "summary": _s(x.get("summary"), 400),
                             "tech": _strs(x.get("tech"), cap=15), "url": _url(x.get("url"))})
    email = _s(d.get("email"), 200)
    return {
        "first_name": _s(d.get("first_name"), 80), "last_name": _s(d.get("last_name"), 80),
        "email": email if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else None,
        "phone": _s(d.get("phone"), 40), "location": _s(d.get("location"), 120),
        "linkedin": _url(d.get("linkedin")), "github": _url(d.get("github")), "portfolio": _url(d.get("portfolio")),
        "summary": _s(d.get("summary"), 800), "skills": _strs(d.get("skills")),
        "experience": jobs[:20], "education": schools[:8], "projects": projects[:15],
        "certifications": _strs(d.get("certifications"), 160, 20),
    }


def _month_index(ym: str | None) -> int | None:
    if not ym or ym == "Present":
        return None
    year, _, month = ym.partition("-")
    return int(year) * 12 + (int(month) - 1 if month else 0)


def years_from(experience: list[dict], today: date | None = None) -> float | None:
    """Full-time years of experience from job dates, counting overlapping jobs once.
    The LLM only copies dates; doing the arithmetic here keeps the number honest."""
    today = today or date.today()
    now = today.year * 12 + today.month - 1
    spans = []
    for job in experience:
        start = _month_index(job.get("start"))
        end = now if job.get("end") == "Present" else _month_index(job.get("end"))
        if job.get("internship") or start is None or end is None or end < start:
            continue
        spans.append((start, end))
    if not spans:
        return None
    months, cur_start, cur_end = 0, None, None
    for start, end in sorted(spans):
        if cur_end is not None and start <= cur_end + 1:
            cur_end = max(cur_end, end)
            continue
        if cur_end is not None:
            months += cur_end - cur_start + 1
        cur_start, cur_end = start, end
    months += cur_end - cur_start + 1
    return round(months / 12, 1)


def parse_resume(cfg: dict, text: str) -> tuple[dict, str]:
    """(facts, model) from the resume text, using the configured AI providers in order."""
    provs = ai.providers(cfg)
    if not provs:
        raise ResumeError("Set an AI key (GEMINI_API_KEY in .env) to have your resume read automatically.")
    messages = [{"role": "system", "content": PARSE_SYSTEM}, {"role": "user", "content": f"RESUME\n\n{text[:20000]}"}]
    for p in provs:
        for attempt in (1, 2):   # one retry on unusable JSON
            try:
                raw = ai.chat(p, messages)
            except Exception as e:
                log.warning("profile: %s failed reading the resume: %s", p.name, e)
                break
            try:
                return clean_facts(ai.extract_json(raw)), f"{p.name}/{p.model}"
            except ValueError as e:
                log.warning("profile: %s returned unusable resume JSON (attempt %d): %s", p.name, attempt, e)
    raise ResumeError("The AI couldn't read your resume just now. Try again in a minute.")


FILL_KEYS = ("first_name", "last_name", "email", "phone", "location", "linkedin", "github", "portfolio", "summary")


def fill_from_facts(prof: dict, facts: dict, model: str, today: date | None = None) -> list[str]:
    """Store the facts and copy them into fields that are still empty. Never overwrites what
    you typed. Returns the labels of the fields it filled."""
    found = {k: facts.get(k) for k in FILL_KEYS}
    found["years_experience"] = years_from(facts["experience"], today)
    current = next((j for j in facts["experience"] if j["end"] == "Present" and not j["internship"]), None)
    if current:
        found.update(current_title=current["title"], current_company=current["company"])
    found["skills"] = facts["skills"]
    filled = []
    for key, value in found.items():
        if value not in (None, "", []) and prof.get(key) in (None, "", []):
            prof[key] = value
            filled.append(FIELDS[key].label)
    prof.update(facts=facts, facts_at=now_iso(), facts_model=model)
    return filled


def new_skills(prof: dict) -> list[str]:
    """Skills on the resume that aren't in your profile's skill list yet."""
    have = {s.lower() for s in prof.get("skills") or []}
    return [s for s in ((prof.get("facts") or {}).get("skills") or []) if s.lower() not in have]
