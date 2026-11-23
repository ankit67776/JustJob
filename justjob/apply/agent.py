"""The apply flow behind both the dashboard and `python -m justjob apply`."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .. import profile
from ..db import DB
from ..text import now_iso
from . import browser
from .answers import AUTH_RE, Answer, memory_key, norm, remembers
from .answers import draft as draft_answers
from .forms import FormError, Question, fetch_questions, target


def job_row(db: DB, job_id: str) -> dict | None:
    row = db.q("SELECT id, source, title, company, location, description, url, salary, status, dedup_key"
               " FROM jobs WHERE id = ?", (job_id,))
    return dict(row[0]) if row else None


def parts(app: dict) -> tuple[list[Question], dict[str, Answer]]:
    return ([Question.from_dict(d) for d in app["questions"]],
            {k: Answer.from_dict(v) for k, v in (app["answers"] or {}).items()})


def draft(db: DB, cfg: dict, job_id: str) -> dict:
    """Reads the form and drafts every answer, keeping the ones you edited on an earlier draft."""
    job = job_row(db, job_id)
    if not job:
        raise FormError("Unknown job.")
    t = target(job)
    if not t:
        raise FormError("The agent can fill Greenhouse and Ashby forms so far. Apply to this one yourself.")
    questions = fetch_questions(t)
    answers = draft_answers(questions, profile.load(db, cfg), job, cfg, db)
    old = db.get_application(job_id)
    if old:
        for qid, a in parts(old)[1].items():
            if a.source == "you" and qid in answers:
                answers[qid] = a
    now = now_iso()
    app = {"job_id": job_id, "ats": t.ats, "apply_url": t.apply_url,
           "state": old["state"] if old and old["state"] == "submitted" else "drafted",
           "questions": [q.to_dict() for q in questions], "answers": {k: a.to_dict() for k, a in answers.items()},
           "report": None, "error": None, "screenshot": old["screenshot"] if old else None,
           "created_at": old["created_at"] if old else now, "updated_at": now,
           "submitted_at": old["submitted_at"] if old else None}
    db.save_application(app)
    return app


def approve(db: DB, job_id: str, form: dict[str, list[str]]) -> dict[str, Answer]:
    """Saves your edits from the review page and remembers approved choices for other forms."""
    app = db.get_application(job_id)
    questions, answers = parts(app)
    now = now_iso()
    for q in questions:
        if q.kind == "file" or (q.id not in form and q.kind != "multiselect"):
            continue
        values = [v.strip() for v in form.get(q.id, [])]
        new = [v for v in values if v] if q.kind == "multiselect" else (values[0] if values else "")
        if q.kind in ("select", "boolean") and new and q.options and new not in q.options:
            continue   # the form can't take a value outside its options; keep the old answer
        old = answers.get(q.id) or Answer(None, "none")
        before = old.value if old.value is not None else ([] if q.kind == "multiselect" else "")
        if new != before:
            answers[q.id] = (Answer(new, "you", False, "You edited this.") if new not in ("", [])
                             else Answer(None, "you", q.required, "You cleared this."))
    for q in questions:
        a = answers.get(q.id)
        if a and remembers(q) and not a.empty and (a.source == "you" or (a.source in ("ai", "memory") and not a.needs_you)):
            db.remember_answer(memory_key(q), q.label, a.value, now)
    db.update_application(job_id, now, answers={k: a.to_dict() for k, a in answers.items()})
    return answers


def start_of_today() -> str:
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).isoformat(timespec="seconds")


def over_cap(db: DB, cfg: dict) -> str | None:
    cap = int(cfg.get("apply", {}).get("max_per_day", 25))
    done = db.submitted_since(start_of_today())
    return (f"You've sent {done} applications through the agent today, the daily limit in config.toml "
            f"([apply] max_per_day). Carry on tomorrow.") if cap and done >= cap else None


def fill(db: DB, cfg: dict, job_id: str, db_path: str, data_dir: Path, headless: bool = False) -> str | None:
    """An error message, or None once the browser is starting."""
    if msg := over_cap(db, cfg):
        return msg
    app, job = db.get_application(job_id), job_row(db, job_id)
    if not app or not job:
        return "Draft the answers first."
    t = target(job)
    questions, answers = parts(app)
    prof = profile.load(db, cfg)
    settings = cfg.get("apply", {})
    return browser.start(job_id, t, questions, answers, prof=prof, resume=profile.resume_path(data_dir, prof),
                         db_path=db_path, data_dir=data_dir, browser=settings.get("browser", "msedge"),
                         wait_minutes=float(settings.get("wait_minutes", 45)), headless=headless)


def status(db: DB, job_id: str) -> dict:
    """What the review page shows: the live browser state if a session is open, else the stored one."""
    app = db.get_application(job_id) or {}
    live = browser.LIVE.get(job_id) or {}
    return {"state": live.get("state") or app.get("state") or "none", "message": live.get("message") or app.get("error") or "",
            "todo": (app.get("report") or {}).get("todo", []), "screenshot": bool(app.get("screenshot")),
            "submitted_at": app.get("submitted_at")}


def warnings(db: DB, cfg: dict, job: dict, questions: list[Question], answers: dict[str, Answer], prof: dict) -> list[str]:
    out = []
    if todo := profile.missing(prof):
        out.append("Your profile is missing: " + ", ".join(label for _, label in todo) + ".")
    if job["status"] == "applied":
        out.append("You've already marked this job as applied.")
    twins = db.q("SELECT company FROM jobs WHERE dedup_key = ? AND id != ? AND status = 'applied' LIMIT 1",
                 (job["dedup_key"], job["id"]))
    if twins:
        out.append(f"You already applied to the same role at {twins[0]['company']} through another listing.")
    for q in questions:
        a = answers.get(q.id)
        if a and a.value == "No" and AUTH_RE.search(norm(q.label)):
            out.append(f"This form asks “{q.label}” and you'll answer No. Many employers screen on that.")
    if msg := over_cap(db, cfg):
        out.append(msg)
    return out
