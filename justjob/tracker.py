"""The application tracker: where each job you applied to stands, and what that says about your search.

Marking a job applied (yourself, or the apply agent when you press Submit) puts it on the tracker at
the Applied stage. From there it moves through Screening, Interview and Offer, and ends as Accepted,
Rejected, Withdrawn or No response. Every move and every note is kept as an event, so each
application has a timeline, and the Insights page can measure the search: how far applications
get, which sources, role types and match scores get replies, and how long replies take.

Applications made outside Just Job (a referral, LinkedIn Easy Apply) can be added by hand. They're
stored as jobs from the "manual" source, which the fetch, the rules and the AI step leave alone.
"""
from __future__ import annotations

import re
import statistics
import uuid
from datetime import UTC, datetime, timedelta

from .db import DB
from .models import Job
from .sources import source_label

# The way through, in order. A job that reached a stage also passed the ones before it.
STAGES = {"applied": "Applied", "screening": "Screening", "interview": "Interview", "offer": "Offer"}
# How an application ends.
OUTCOMES = {"accepted": "Accepted", "rejected": "Rejected", "withdrawn": "Withdrawn", "no_response": "No response"}
LABELS = {**STAGES, **OUTCOMES}
ORDER = list(STAGES)
REPLIES = ("screening", "interview", "offer", "accepted", "rejected")   # stages only an employer's answer leads to

FOLLOW_UP_DAYS = 7      # a quiet week: time to follow up (Teal and Huntr suggest the same)
NO_REPLY_DAYS = 21      # three weeks at Applied with nothing back: probably no response


def _dt(iso: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def days_between(a: str | None, b: str | None) -> float | None:
    da, db_ = _dt(a), _dt(b)
    return (db_ - da).total_seconds() / 86400 if da and db_ else None


def is_open(stage: str | None) -> bool:
    return (stage or "applied") in STAGES


# --- one application ---------------------------------------------------------------------------

def nudge(row: dict, now: str) -> str | None:
    """What an open application needs from you, judged by how long it's been quiet."""
    stage = row.get("stage") or "applied"
    if not is_open(stage) or stage == "offer":
        return None
    quiet = days_between(row.get("active_at") or row.get("stage_at") or row.get("status_at"), now)
    if quiet is None:
        return None
    if stage == "applied" and quiet >= NO_REPLY_DAYS:
        return "no_reply"
    return "follow_up" if quiet >= FOLLOW_UP_DAYS else None


def set_stage(db: DB, job_id: str, stage: str, now: str, note: str = "") -> bool:
    if stage not in LABELS:
        raise ValueError(f"stage must be one of {', '.join(LABELS)}")
    return db.set_stage(job_id, stage, now, note.strip()[:2000])


def add_note(db: DB, job_id: str, note: str, now: str) -> bool:
    note = note.strip()[:2000]
    return bool(note) and db.add_note(job_id, note, now)


def add_manual(db: DB, company: str, title: str, url: str, location: str, via: str, applied_at: str, now: str) -> str:
    """An application made outside Just Job. Returns its job id."""
    company, title = company.strip()[:200], title.strip()[:300]
    if not company or not title:
        raise ValueError("A company and a job title are needed.")
    url = url.strip()[:1000]
    if url and not re.match(r"https?://", url, re.I):
        url = "https://" + url
    job = Job("manual", uuid.uuid4().hex[:12], title, company, url, location.strip()[:200],
              board=via.strip()[:60] or "Elsewhere")
    db.insert_jobs([job], now)
    db.set_status(job.id, "applied", applied_at or now)
    return job.id


def where_from(row: dict) -> str:
    """Where you found the job: the ATS or job board, or what you typed for one added by hand."""
    if row["source"] == "manual":
        return row["board"] or "Elsewhere"
    return source_label(row["source"], row["board"] or "")


# --- the whole search --------------------------------------------------------------------------

def furthest(events: list[dict]) -> int:
    """Index in ORDER of the furthest stage a job reached. An accepted offer was an offer."""
    seen = {e["stage"] for e in events if e["kind"] == "stage"}
    if "accepted" in seen:
        return ORDER.index("offer")
    return max((ORDER.index(s) for s in seen if s in STAGES), default=0)


def first_reply(events: list[dict]) -> str | None:
    return next((e["at"] for e in events if e["kind"] == "stage" and e["stage"] in REPLIES), None)


def applied_on(row: dict, events: list[dict]) -> str | None:
    return next((e["at"] for e in events if e["kind"] == "stage" and e["stage"] == "applied"), row.get("status_at"))


def score_band(row: dict) -> str:
    score = row["ai_score"] if row["ai_score"] is not None else row["rule_score"]
    if score is None:
        return "Not scored"
    return "80 and up" if score >= 80 else "60 to 79" if score >= 60 else "Under 60"


BAND_ORDER = ("80 and up", "60 to 79", "Under 60", "Not scored")


def _rates(rows: list[dict], key, order: tuple[str, ...] | None = None) -> list[dict]:
    """Reply rate per group: [{name, applied, replied, rate}], biggest group first unless an order is given."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = [{"name": name, "applied": len(g), "replied": sum(r["replied"] for r in g),
            "rate": sum(r["replied"] for r in g) / len(g)} for name, g in groups.items()]
    if order:
        return sorted(out, key=lambda g: order.index(g["name"]) if g["name"] in order else len(order))
    return sorted(out, key=lambda g: (-g["applied"], g["name"]))


def week_start(dt: datetime) -> datetime:
    local = dt.astimezone()
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def insights(db: DB, now: str, days: int | None = None, weeks: int = 12) -> dict:
    """Everything the Insights page shows, for applications made in the last `days` (or all time)."""
    from .web import ROLES, role_of  # the Jobs page owns the role groups

    events: dict[str, list[dict]] = {}
    for ev in db.track_events():
        events.setdefault(ev["job_id"], []).append(ev)
    cutoff = (_dt(now) - timedelta(days=days)) if days else None
    rows = []
    for r in db.tracked():
        evs = events.get(r["id"], [])
        r["applied_on"] = applied_on(r, evs)
        if cutoff and (_dt(r["applied_on"]) or _dt(now)) < cutoff:
            continue
        r["furthest"] = furthest(evs)
        r["first_reply"] = first_reply(evs)
        r["replied"] = r["first_reply"] is not None
        rows.append(r)

    total = len(rows)
    reached = [sum(1 for r in rows if r["furthest"] >= i) for i in range(len(ORDER))]
    funnel = [{"stage": s, "label": STAGES[s], "count": reached[i], "of_total": reached[i] / total if total else 0,
               "of_previous": (reached[i] / reached[i - 1] if i and reached[i - 1] else None)}
              for i, s in enumerate(ORDER)]
    waits = [w for r in rows if r["replied"] and (w := days_between(r["applied_on"], r["first_reply"])) is not None]

    this_week = week_start(_dt(now))
    starts = [this_week - timedelta(weeks=n) for n in range(weeks - 1, -1, -1)]
    per_week = {s: 0 for s in starts}
    for r in rows:
        if (at := _dt(r["applied_on"])) and (ws := week_start(at)) in per_week:
            per_week[ws] += 1
    outcomes = {k: sum(1 for r in rows if r["stage"] == k) for k in OUTCOMES}

    return {
        "total": total,
        "replied": sum(r["replied"] for r in rows),
        "interviews": reached[ORDER.index("interview")],
        "offers": reached[ORDER.index("offer")],
        "open": sum(1 for r in rows if is_open(r["stage"])),
        "waiting": sum(1 for r in rows if nudge(r, now)),
        "median_days": statistics.median(waits) if waits else None,
        "funnel": funnel,
        "outcomes": outcomes,
        "by_source": _rates(rows, where_from),
        "by_role": _rates(rows, lambda r: ROLES[role_of(r["title"])][0], tuple(v[0] for v in ROLES.values())),
        "by_score": _rates(rows, score_band, BAND_ORDER),
        "by_route": _rates(rows, lambda r: "Through the apply agent" if r["agent_at"] else "On your own"),
        "weeks": [{"start": s, "count": c} for s, c in per_week.items()],
    }
