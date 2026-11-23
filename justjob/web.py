"""HTML rendering (shared by the digest file and the dashboard) and the local dashboard server."""
from __future__ import annotations

import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import ai, answers_page, apply_page, profile, profile_page, registry
from .apply import agent as apply_agent
from .apply import browser as apply_browser
from .apply import memory
from .apply.answers import fit
from .apply.forms import FormError
from .apply.forms import target as apply_target
from .db import DB, REFILTER_PENDING, SHOWABLE, STATUSES, score_sql
from .sources import ATS, DISPLAY, source_label
from .text import now_iso
from .ui import CSS, e, top_bar

log = logging.getLogger(__name__)

JS = """
const form = document.getElementById('filters');
const search = document.getElementById('q');
const toast = document.getElementById('toast');
// Every filter applies as soon as it changes; the search box applies on Enter.
document.addEventListener('change', (e) => {
  if (form && e.target.form === form && e.target !== search) form.requestSubmit();
});
document.querySelector('.filters-btn')?.addEventListener('click', () => document.body.classList.toggle('show-filters'));

const rows = () => [...document.querySelectorAll('.row')];
let cur = -1, undo = null, toastTimer = null;
function select(i) {
  const all = rows();
  if (!all.length) return;
  cur = Math.max(0, Math.min(i, all.length - 1));
  all.forEach((r, j) => r.classList.toggle('cur', j === cur));
  all[cur].scrollIntoView({block: 'nearest'});
}
function bump(delta) {
  const c = document.querySelector('.results-head .count');
  if (c) c.textContent = (parseInt(c.textContent.replace(/,/g, '')) + delta).toLocaleString('en-US');
}
async function setStatus(id, status) {
  const r = await fetch('/status', {method: 'POST', body: new URLSearchParams({id, status})});
  if (!r.ok) throw new Error(await r.text());
}
function showToast(text, onUndo) {
  toast.querySelector('span').textContent = text;
  toast.classList.add('on');
  undo = onUndo;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('on'), 6000);
}
toast?.querySelector('button').addEventListener('click', () => { undo?.(); toast.classList.remove('on'); });

document.addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-status]');
  if (!b) return;
  e.preventDefault();
  const card = b.closest('.row'), id = card.dataset.id, status = b.dataset.status;
  const parent = card.parentNode, next = card.nextSibling, index = rows().indexOf(card);
  try { await setStatus(id, status); } catch (err) { showToast('Could not update: ' + err.message); return; }
  card.classList.add('leaving');
  setTimeout(() => card.remove(), 250);
  bump(-1);
  if (cur >= index && cur > 0) cur--;
  const done = {applied: 'Marked as applied', hidden: 'Hidden', new: 'Moved back to matches'}[status];
  showToast(done, async () => {
    await setStatus(id, card.dataset.status);
    card.classList.remove('leaving');
    parent.insertBefore(card, next);
    bump(1);
  });
});
document.addEventListener('toggle', async (e) => {
  const d = e.target;
  if (d.tagName !== 'DETAILS' || !d.classList.contains('desc') || !d.open || d.dataset.loaded) return;
  d.dataset.loaded = 1;
  const r = await fetch('/description?id=' + encodeURIComponent(d.closest('.row').dataset.id));
  d.querySelector('pre').textContent = r.ok ? await r.text() : 'The description could not be loaded. Open the posting instead.';
}, true);
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.target.matches('input, select, textarea')) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === '/') { e.preventDefault(); search?.focus(); search?.select(); return; }
  if (e.key === 'ArrowLeft') return document.querySelector('a[rel=prev]')?.click();
  if (e.key === 'ArrowRight') return document.querySelector('a[rel=next]')?.click();
  if (e.key === 'j') return select(cur + 1);
  if (e.key === 'k') return select(cur - 1);
  const row = rows()[cur];
  if (!row) return;
  if (e.key === 'o') row.querySelector('.apply').click();
  if (e.key === 'f') row.querySelector('.autofill')?.click();
  if (e.key === 'a') row.querySelector('[data-status=applied]')?.click();
  if (e.key === 'h') row.querySelector('[data-status=hidden]')?.click();
  if (e.key === 'd') { const d = row.querySelector('details'); if (d) d.open = !d.open; }
});
"""

SEARCH_ICON = ('<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5" fill="none" '
               'stroke="currentColor" stroke-width="1.6"/><path d="M11 11l3.5 3.5" stroke="currentColor" stroke-width="1.6" '
               'stroke-linecap="round"/></svg>')


# --- labels shared with the CLI, digest and notifications --------------------------------

def _days_old(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        posted = datetime.fromisoformat(iso).astimezone().date()   # calendar days, local time
    except ValueError:
        return None
    return max(0, (datetime.now().astimezone().date() - posted).days)


def ago(iso: str | None) -> str:
    days = _days_old(iso)
    if days is None:
        return ""
    return "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")


def experience_label(r) -> str:
    if r["ai_min_years"] is not None:
        return f"{r['ai_min_years']:g}+ yrs"
    if r["exp_years"] is not None:
        return f"{r['exp_years']:g}+ yrs"
    return "yrs unclear"


def matched_skills(r) -> list[str]:
    try:
        return json.loads(r["matched_skills"] or "[]")
    except ValueError:
        return []


def why_label(r) -> str:
    if r["ai_reason"]:
        return r["ai_reason"]
    skills = matched_skills(r)
    if skills:
        return "Your skills mentioned: " + ", ".join(skills)
    return r["rule_reason"] or ""


def flags(r) -> list[str]:
    try:
        return json.loads(r["ai_flags"] or "[]")
    except ValueError:
        return []


# --- job rows ----------------------------------------------------------------------------

def _score_class(score: int) -> str:
    return "hi" if score >= 70 else ("mid" if score >= 45 else "lo")


def monogram(company: str) -> tuple[str, int]:
    """Two letters and a stable hue per company, so the same employer is easy to spot."""
    words = re.findall(r"[A-Za-z0-9]+", company) or ["?"]
    letters = (words[0][0] + (words[1][0] if len(words) > 1 else words[0][1:2])).upper()
    return letters, zlib.crc32(company.lower().encode()) % 360


def posted_label(r) -> tuple[str, str, bool]:
    """('3d ago', 'Posted 17 Oct 2026', fresh?). Falls back to when we first saw the job."""
    iso, verb = r["posted_at"], "Posted"
    if not iso:
        iso, verb = r["first_seen"], "First seen"
    days = _days_old(iso)
    if days is None:
        return "", "", False
    short = "Today" if days == 0 else f"{days}d ago"
    day = datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y").lstrip("0")
    return short, f"{verb} {day}", days <= 3


def applied_label(iso: str | None) -> tuple[str, str]:
    """('Applied 2d ago', 'Applied 6 Nov 2026, 3:18 pm') from the job's status_at."""
    days = _days_old(iso)
    if days is None:
        return "Applied", ""
    short = "Applied today" if days == 0 else ("Applied yesterday" if days == 1 else f"Applied {days}d ago")
    when = datetime.fromisoformat(iso).astimezone()
    return short, f"Applied {when.strftime('%d %b %Y').lstrip('0')}, {when.strftime('%I:%M %p').lstrip('0').lower()}"


_JUST_REMOTE = re.compile(r"\s*(?:fully\s+)?remote(?:\s+job)?(?:\s*[-,(]\s*(?:anywhere|worldwide|global)\)?)?\s*", re.I)


def render_job(r, interactive: bool, new_since: str | None = None) -> str:
    score = r["score"]
    letters, hue = monogram(r["company"] or "?")
    title = f'<a class="title" href="{e(r["url"])}" target="_blank" rel="noopener">{e(r["title"])}</a>'
    if new_since is not None and r["first_seen"] > new_since:
        title += '<span class="new">New</span>'

    meta = [f'<span class="company">{e(r["company"])}</span>']
    if r["location"] and not (r["remote"] and _JUST_REMOTE.fullmatch(r["location"])):
        meta.append(f"<span>{e(r['location'][:90])}</span>")
    if r["remote"]:
        meta.append('<span class="remote">Remote</span>')
    years = experience_label(r)
    meta.append(f"<span>{'Experience not stated' if years == 'yrs unclear' else e(years)}</span>")
    if r["salary"]:
        meta.append(f"<span>{e(r['salary'])}</span>")
    meta.append(f'<span class="src">via {e(source_label(r["source"], r["board"]))}</span>')
    if r["closed"]:
        meta.append('<span class="closed">Posting closed</span>')

    detail = ""
    if r["ai_reason"]:
        detail += f'<p class="why">{e(r["ai_reason"])}</p>'
    skills = matched_skills(r)
    badges = [f'<span class="flag">{e(f)}</span>' for f in flags(r)] + [f'<span class="skill">{e(s)}</span>' for s in skills[:6]]
    if len(skills) > 6:
        badges.append(f'<span class="rest">+{len(skills) - 6} more</span>')
    if badges:
        lead = '<span class="lead">Mentions</span>' if skills else ""
        detail += f'<div class="skills">{lead}{"".join(badges)}</div>'
    if interactive and r["description"]:
        detail += '<details class="desc"><summary>Read description</summary><pre>Loading…</pre></details>'

    short, full, fresh = posted_label(r)
    side = f'<span class="posted{" fresh" if fresh else ""}" title="{e(full)}">{e(short)}</span>'
    if r["status"] == "applied":
        a_short, a_full = applied_label(r["status_at"])
        side = f'<span class="applied-on" title="{e(a_full)}">{e(a_short)}</span>' + side
    side += f'<a class="btn apply" href="{e(r["url"])}" target="_blank" rel="noopener">Apply</a>'
    if interactive and apply_target(r):
        side += (f'<a class="btn autofill" href="/apply?{e(urllib.parse.urlencode({"job": r["id"]}))}" '
                 'title="Draft answers and fill the form for you (f)">Auto-fill</a>')
    if interactive:
        if r["status"] == "new":
            side += ('<div class="minor"><button class="btn" data-status="applied" title="Mark as applied (a)">Applied</button>'
                     '<button class="btn" data-status="hidden" title="Hide (h)">Hide</button></div>')
        else:
            side += '<button class="btn" data-status="new">Move back</button>'

    kind = "AI score" if r["ai_score"] is not None else "rule score"
    return (
        f'<article class="row" data-id="{e(r["id"])}" data-status="{e(r["status"])}" data-company="{e(r["company"])}">'
        f'<div class="mono" style="--h:{hue}" aria-hidden="true">{e(letters)}</div>'
        f'<div class="body">{title}<div class="meta">{"".join(meta)}</div>{detail}</div>'
        f'<div class="score {_score_class(score)}" title="Match score, {kind}"><span class="num">{score}</span>'
        f'<span class="bar"><i style="width:{max(0, min(100, score))}%"></i></span><span class="lbl">{kind}</span></div>'
        f'<div class="side">{side}</div></article>'
    )


def page(title: str, body: str) -> str:
    """A plain page (the digest file) in the dashboard's style."""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>'
        f"<style>{CSS}</style></head><body class=\"digest\"><header class=\"top\"><span class=\"brand\">{e(title)}</span>"
        f"</header><main>{body}</main></body></html>"
    )


# --- dashboard filters -------------------------------------------------------------------

VIEWS = {   # tab: (label, status shown or None for any, apply digest.min_score)
    "matches": ("Matches", "new", True),
    "applied": ("Applied", "applied", False),
    "hidden": ("Hidden", "hidden", False),
    "all": ("All", None, False),
}
# Role groups by title; the first that matches wins.
ROLES = {
    "ai": ("AI / ML", r"\b(?:ai|ml|mle|machine learning|llms?|genai|gen ai|generative|nlp|deep learning|computer vision|"
                      r"applied scientist|research (?:scientist|engineer)|data scientist|inference|agents?|agentic|rag)\b"),
    "fullstack": ("Full stack", r"full[\s-]?stack"),
    "frontend": ("Frontend", r"\b(?:front[\s-]?end|react|ui|web|javascript|typescript|angular|vue)\b"),
    "backend": ("Backend and platform", r"\b(?:back[\s-]?end|python|golang|go|java|node(?:\.?js)?|api|platform|"
                                        r"infrastructure|distributed|systems?|cloud|devops|sre|server)\b"),
    "data": ("Data", r"\bdata\b"),
    "software": ("Software, general", None),
}
_ROLE_RES = [(k, re.compile(p, re.I)) for k, (_, p) in ROLES.items() if p]
EXPERIENCE = {"": "Any", "1": "Up to 1 year", "2": "Up to 2 years", "3": "Up to 3 years", "4": "Up to 4 years",
              "none": "Not stated"}
POSTED_WINDOWS = {"": "Any time", "1": "Last 24 hours", "3": "Last 3 days", "7": "Last 7 days", "14": "Last 14 days",
                  "30": "Last 30 days"}
SORTS = {"best": "Best match", "newest": "Newest first", "years": "Least experience first"}
POSTED_AT = "COALESCE(posted_at, first_seen)"
YEARS = "COALESCE(ai_min_years, exp_years)"
PAGE = 50                  # jobs per page by default
PER_PAGE = (25, 50, 100)


def role_of(title: str | None) -> str:
    for key, rx in _ROLE_RES:
        if rx.search(title or ""):
            return key
    return "software"


def work_modes(cfg: dict) -> dict[str, str]:
    onsite = [p.lower() for p in cfg["filters"]["location"].get("onsite", [])]
    where = " in India" if "india" in onsite else ""
    return {"remote": "Remote", "onsite": f"On-site or hybrid{where}", "unclear": "Location unclear"}


def _mode_fn(cfg: dict):
    onsite = [p for p in cfg["filters"]["location"].get("onsite", []) if p != "*"]
    rx = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in onsite) + r")\b", re.I) if onsite else None
    anywhere = "*" in cfg["filters"]["location"].get("onsite", [])

    def mode(location: str | None, remote: int | None) -> str:
        if remote == 1:
            return "remote"
        if (anywhere and location) or (rx and rx.search(location or "")):
            return "onsite"
        return "unclear"
    return mode


@dataclass(frozen=True)
class Filters:
    view: str = "matches"
    q: str = ""
    roles: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    exp: str = ""
    posted: str = ""
    sources: tuple[str, ...] = ()
    salary: bool = False
    sort: str = "best"
    page: int = 1
    per: int = PAGE

    @classmethod
    def from_query(cls, qs: dict[str, list[str]]) -> Filters:
        one = lambda k, default="": (qs.get(k) or [default])[0].strip()   # noqa: E731
        many = lambda k, allowed: tuple(v for v in dict.fromkeys(qs.get(k, [])) if v in allowed)   # noqa: E731
        def number(k: str, default: int) -> int:
            try:
                return int(one(k, str(default)))
            except ValueError:
                return default
        return cls(
            view=one("view", "matches") if one("view", "matches") in VIEWS else "matches",
            q=one("q")[:200],
            roles=many("role", ROLES),
            modes=many("mode", ("remote", "onsite", "unclear")),
            exp=one("exp") if one("exp") in EXPERIENCE else "",
            posted=one("posted") if one("posted") in POSTED_WINDOWS else "",
            sources=tuple(v for v in dict.fromkeys(qs.get("source", [])) if re.fullmatch(r"[a-z]+", v)),
            salary=one("salary") == "1",
            sort=one("sort", "best") if one("sort", "best") in SORTS else "best",
            page=max(1, min(100_000, number("page", 1))),
            per=number("per", PAGE) if number("per", PAGE) in PER_PAGE else PAGE,
        )

    def url(self, **change) -> str:
        f = replace(self, **change)
        pairs = [("view", f.view)] if f.view != "matches" else []
        pairs += [("q", f.q)] if f.q else []
        pairs += [("role", v) for v in f.roles] + [("mode", v) for v in f.modes]
        pairs += [("exp", f.exp)] if f.exp else []
        pairs += [("posted", f.posted)] if f.posted else []
        pairs += [("source", v) for v in f.sources]
        pairs += [("salary", "1")] if f.salary else []
        pairs += [("sort", f.sort)] if f.sort != "best" else []
        pairs += [("per", str(f.per))] if f.per != PAGE else []
        pairs += [("page", str(f.page))] if f.page != 1 else []
        return "/?" + urllib.parse.urlencode(pairs) if pairs else "/"

    def predicates(self, search_hits: set[str] | None) -> dict[str, Callable[[dict], bool]]:
        """One test per active filter, keyed so a facet's counts can leave out its own filter."""
        p: dict[str, Callable[[dict], bool]] = {}
        if search_hits is not None:
            p["q"] = lambda j: j["id"] in search_hits
        if self.roles:
            p["roles"] = lambda j: j["role"] in self.roles
        if self.modes:
            p["modes"] = lambda j: j["mode"] in self.modes
        if self.exp == "none":
            p["exp"] = lambda j: j["years"] is None
        elif self.exp:
            limit = float(self.exp)
            p["exp"] = lambda j: j["years"] is not None and j["years"] <= limit
        if self.posted:
            days = int(self.posted)
            p["posted"] = lambda j: j["age"] is not None and j["age"] <= days
        if self.sources:
            p["sources"] = lambda j: j["source"] in self.sources
        if self.salary:
            p["salary"] = lambda j: bool(j["salary"])
        return p

    def chips(self, cfg: dict) -> list[tuple[str, str]]:
        """(label, url without that filter) for every active filter."""
        modes = work_modes(cfg)
        out = [(f"“{self.q}”", self.url(q="", page=1))] if self.q else []
        out += [(ROLES[v][0], self.url(roles=tuple(x for x in self.roles if x != v), page=1)) for v in self.roles]
        out += [(modes[v], self.url(modes=tuple(x for x in self.modes if x != v), page=1)) for v in self.modes]
        if self.exp:
            out.append((f"Experience: {EXPERIENCE[self.exp].lower()}", self.url(exp="", page=1)))
        if self.posted:
            out.append((POSTED_WINDOWS[self.posted], self.url(posted="", page=1)))
        out += [(DISPLAY.get(v, v), self.url(sources=tuple(x for x in self.sources if x != v), page=1)) for v in self.sources]
        if self.salary:
            out.append(("Shows salary", self.url(salary=False, page=1)))
        return out


def _option(kind: str, name: str, value: str, label: str, count: int, checked: bool) -> str:
    zero = " zero" if not count and not checked else ""
    return (f'<label class="opt{zero}"><input type="{kind}" name="{name}" value="{e(value)}"{" checked" if checked else ""}>'
            f'<span>{e(label)}</span><span class="n">{count:,}</span></label>')


def page_numbers(page: int, pages: int) -> list[int | None]:
    """1 … 4 5 [6] 7 8 … 105: the first and last page, two either side of the current one, and
    None where pages are skipped."""
    keep = sorted({1, pages, *range(page - 2, page + 3)} & set(range(1, pages + 1)))
    out: list[int | None] = []
    for n in keep:
        if out and n - out[-1] > 1:
            out.append(None)
        out.append(n)
    return out


def pager(f: Filters, page: int, pages: int) -> str:
    if pages <= 1:
        return ""
    prev = (f'<a class="step" rel="prev" href="{e(f.url(page=page - 1))}">‹ Previous</a>' if page > 1
            else '<span class="step off">‹ Previous</span>')
    nxt = (f'<a class="step" rel="next" href="{e(f.url(page=page + 1))}">Next ›</a>' if page < pages
           else '<span class="step off">Next ›</span>')
    cells = []
    for n in page_numbers(page, pages):
        if n is None:
            cells.append('<span class="gap">…</span>')
        elif n == page:
            cells.append(f'<span class="cur" aria-current="page">{n:,}</span>')
        else:
            cells.append(f'<a href="{e(f.url(page=n))}" aria-label="Page {n}">{n:,}</a>')
    return f'<nav class="pager" aria-label="Pages">{prev}{"".join(cells)}{nxt}</nav>'


# Jobs the dashboard lists: what the rules kept, plus everything you applied to, so the Applied
# tab stays a full record after a posting closes, gets merged as a duplicate, or new rules reject it.
LISTED = f"(({SHOWABLE}) OR status = 'applied')"


def kept_jobs(db: DB, cfg: dict) -> list[dict]:
    """Every listed job, with just what filtering, counting and sorting need. One indexed
    read; the full rows are loaded only for the jobs on screen."""
    mode = _mode_fn(cfg)
    rows = db.q(
        f"SELECT id, title, location, remote, source, salary != '' AS salary, status, status_at,"
        f" {POSTED_AT} AS posted, {YEARS} AS years, julianday('now') - julianday({POSTED_AT}) AS age,"
        f" {score_sql(cfg)} AS score FROM jobs WHERE {LISTED}")
    return [{**dict(r), "role": role_of(r["title"]), "mode": mode(r["location"], r["remote"])} for r in rows]


def search_hits(db: DB, query: str) -> set[str] | None:
    terms = query.split()
    if not terms:
        return None
    one = "(title LIKE ? OR company LIKE ? OR location LIKE ? OR description LIKE ?)"
    params = [f"%{t}%" for t in terms for _ in range(4)]
    return {r[0] for r in db.q(f"SELECT id FROM jobs WHERE {LISTED} AND {' AND '.join([one] * len(terms))}", params)}


def dashboard_html(db: DB, cfg: dict, f: Filters | None = None, companies: int = 0, sign_out: bool = False) -> str:
    f = f or Filters()
    min_score = int(cfg["digest"].get("min_score", 0))
    preds = f.predicates(search_hits(db, f.q))

    def ok(j: dict, skip: str = "") -> bool:
        return all(test(j) for key, test in preds.items() if key != skip)

    def in_tab(j: dict, tab: str) -> bool:
        _, status, use_min = VIEWS[tab]
        return (status is None or j["status"] == status) and (not use_min or j["score"] >= min_score)

    jobs = kept_jobs(db, cfg)
    tab_n = {tab: sum(1 for j in jobs if in_tab(j, tab) and ok(j)) for tab in VIEWS}
    pool = [j for j in jobs if in_tab(j, f.view)]

    # Facet counts: how many jobs each option would leave, given every other filter.
    role_n = Counter(j["role"] for j in pool if ok(j, "roles"))
    mode_n = Counter(j["mode"] for j in pool if ok(j, "modes"))
    source_n = Counter(j["source"] for j in pool if ok(j, "sources"))
    years = [j["years"] for j in pool if ok(j, "exp")]
    exp_n = {k: (len(years) if not k else sum(y is None for y in years) if k == "none"
                 else sum(y is not None and y <= float(k) for y in years)) for k in EXPERIENCE}
    ages = [j["age"] for j in pool if ok(j, "posted")]
    posted_n = {k: (len(ages) if not k else sum(a is not None and a <= int(k) for a in ages)) for k in POSTED_WINDOWS}
    salary_n = sum(1 for j in pool if ok(j, "salary") and j["salary"])

    found = [j for j in pool if ok(j)]
    if f.sort == "newest":
        found.sort(key=lambda j: (j["posted"] or "", j["score"]), reverse=True)
    elif f.sort == "years":
        found.sort(key=lambda j: (j["years"] is None, j["years"] or 0, -j["score"]))
    elif f.view in ("applied", "hidden"):
        found.sort(key=lambda j: j["status_at"] or "", reverse=True)
    else:
        found.sort(key=lambda j: (j["score"], j["posted"] or ""), reverse=True)
    total = len(found)
    pages = max(1, -(-total // f.per))
    page = min(f.page, pages)
    first = (page - 1) * f.per
    ids = [j["id"] for j in found[first: first + f.per]]
    full = {r["id"]: r for r in db.q(f"SELECT *, {score_sql(cfg)} AS score FROM jobs WHERE id IN "
                                     f"({','.join('?' * len(ids))})", ids)} if ids else {}
    rows = [full[i] for i in ids if i in full]

    # -- top bar
    tabs = "".join(f'<a class="{"on" if k == f.view else ""}" href="{e(f.url(view=k, page=1))}">{e(lbl)}'
                   f'<span class="n">{tab_n[k]:,}</span></a>' for k, (lbl, _, _) in VIEWS.items())
    top = top_bar(
        "jobs",
        f'<div class="search">{SEARCH_ICON}<input id="q" name="q" form="filters" type="search" value="{e(f.q)}" '
        f'placeholder="Search roles, companies, skills or cities" aria-label="Search"><kbd>/</kbd></div>',
        f'<nav class="tabs" aria-label="Lists">{tabs}</nav>'
        f'<button class="btn filters-btn" type="button">Filters</button>',
    )

    # -- filter rail
    modes = work_modes(cfg)
    role_opts = "".join(_option("checkbox", "role", k, lbl, role_n.get(k, 0), k in f.roles) for k, (lbl, _) in ROLES.items())
    mode_opts = "".join(_option("checkbox", "mode", k, lbl, mode_n.get(k, 0), k in f.modes) for k, lbl in modes.items())
    exp_opts = "".join(_option("radio", "exp", k, lbl, exp_n[k], k == f.exp) for k, lbl in EXPERIENCE.items())
    posted_opts = "".join(_option("radio", "posted", k, lbl, posted_n[k], k == f.posted) for k, lbl in POSTED_WINDOWS.items())
    srcs = sorted(set(source_n) | set(f.sources), key=lambda s: (-source_n.get(s, 0), s))
    src_html = [_option("checkbox", "source", s, DISPLAY.get(s, s), source_n.get(s, 0), s in f.sources) for s in srcs]
    if len(src_html) > 6:
        src_html = src_html[:6] + [f'<details><summary>Show {len(src_html) - 6} more</summary>{"".join(src_html[6:])}</details>']
    last_run = db.q("SELECT finished_at FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")
    foot = ""
    if companies:
        foot += f'<p>Tracking <span class="num">{companies:,}</span> companies</p>'
    foot += f'<p>Last run {e(ago(last_run[0][0])) if last_run else "never"}</p>'
    if sign_out:
        foot += '<p><a href="/logout">Sign out</a></p>'
    foot += ('<p class="keys"><kbd>j</kbd> <kbd>k</kbd> move, <kbd>o</kbd> open, <kbd>f</kbd> auto-fill, <kbd>a</kbd> applied, '
             '<kbd>h</kbd> hide, <kbd>d</kbd> description, <kbd>←</kbd> <kbd>→</kbd> pages</p>')
    hidden = f'<input type="hidden" name="view" value="{e(f.view)}">' if f.view != "matches" else ""
    rail = (
        f'<form class="rail" id="filters" method="get" action="/" aria-label="Filters">{hidden}'
        f'<fieldset class="facet"><legend>Role</legend>{role_opts}</fieldset>'
        f'<fieldset class="facet"><legend>Work mode</legend>{mode_opts}</fieldset>'
        f'<fieldset class="facet"><legend>Experience required</legend>{exp_opts}</fieldset>'
        f'<fieldset class="facet"><legend>Posted</legend>{posted_opts}</fieldset>'
        f'<fieldset class="facet"><legend>Source</legend>{"".join(src_html)}</fieldset>'
        f'<fieldset class="facet"><legend>Salary</legend>'
        f'{_option("checkbox", "salary", "1", "Shows salary", salary_n, f.salary)}</fieldset>'
        f'<div class="rail-foot">{foot}</div></form>'
    )

    # -- results
    sort_sel = "".join(f'<option value="{k}"{" selected" if k == f.sort else ""}>{e(v)}</option>' for k, v in SORTS.items())
    noun = "role" if total == 1 else "roles"
    per_sel = "".join(f'<option value="{n}"{" selected" if n == f.per else ""}>{n} per page</option>' for n in PER_PAGE)
    shown_range = f"{first + 1:,}–{first + len(rows):,}" if rows else ""
    tools = ""
    if pages > 1:
        prev = (f'<a class="mini" href="{e(f.url(page=page - 1))}" aria-label="Previous page">‹</a>' if page > 1
                else '<span class="mini off" aria-hidden="true">‹</span>')
        nxt = (f'<a class="mini" href="{e(f.url(page=page + 1))}" aria-label="Next page">›</a>' if page < pages
               else '<span class="mini off" aria-hidden="true">›</span>')
        tools = f'<span class="range">Page {page:,} of {pages:,}</span>{prev}{nxt}'
    elif rows:
        tools = f'<span class="range">Showing {shown_range}</span>'
    head = (f'<div class="results-head"><span class="count">{total:,}</span><span class="what">{noun}</span>'
            f'<div class="head-tools">{tools}<select name="sort" form="filters" aria-label="Sort">{sort_sel}</select>'
            f'<select name="per" form="filters" aria-label="Jobs per page">{per_sel}</select></div></div>')
    chips = f.chips(cfg)
    chip_html = ""
    if chips:
        chip_html = '<div class="chips">' + "".join(
            f'<a class="chip" href="{e(u)}" title="Remove filter">{e(lbl)}<b aria-hidden="true">×</b></a>' for lbl, u in chips)
        chip_html += f'<a class="chip clear" href="{e(Filters(view=f.view, sort=f.sort).url())}">Clear all</a></div>'
    since = db.get_meta("previous_digest_at")
    if rows:
        body = '<div class="list">' + "".join(render_job(r, True, since) for r in rows) + "</div>"
        body += pager(f, page, pages) + f'<p class="showing">Showing {shown_range} of {total:,}</p>'
    elif chips:
        body = ('<div class="list empty"><strong>No roles match these filters.</strong>'
                f'Remove a filter above, or <a href="{e(Filters(view=f.view).url())}">clear them all</a>.</div>')
    else:
        body = ('<div class="list empty"><strong>Nothing here yet.</strong>'
                + {"applied": "Jobs you mark as applied are listed here.", "hidden": "Jobs you hide are listed here."}.get(
                    f.view, "Run <code>python -m justjob run</code> to fetch jobs.") + "</div>")

    toast = '<div class="toast" id="toast" role="status"><span></span><button type="button">Undo</button></div>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Just Job</title>'
        f"<style>{CSS}</style></head><body>{top}<div class=\"layout\">{rail}<main>{head}{chip_html}{body}</main></div>"
        f"{toast}<script>{JS}</script></body></html>"
    )


# --- sign-in ---------------------------------------------------------------------------

SESSION_COOKIE = "jj_session"
SESSION_DAYS = 30
MAX_FAILURES = 5            # wrong passwords per address...
FAILURE_WINDOW = 10 * 60    # ...within this many seconds, before sign-in is refused for a while


def session_key(password: str, secret: bytes) -> bytes:
    """Signs session cookies. Mixing in the password signs everyone out when it changes."""
    return hmac.new(secret, password.encode(), hashlib.sha256).digest()


def make_session(key: bytes, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{hmac.new(key, str(expires).encode(), hashlib.sha256).hexdigest()}"


def valid_session(key: bytes, value: str, now: float | None = None) -> bool:
    expires, _, sig = value.partition(".")
    if not expires.isdigit() or int(expires) < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(sig, hmac.new(key, expires.encode(), hashlib.sha256).hexdigest())


def load_secret(path: Path) -> bytes:
    """A random key kept next to the database, made on first use."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        if os.name != "nt":
            path.chmod(0o600)
    return path.read_bytes()


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def safe_next(url: str) -> str:
    """Only paths on this site, so the sign-in page can't be used to bounce people elsewhere."""
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url else "/"


def login_html(error: str = "", next_url: str = "/") -> str:
    msg = f'<p class="login-error" role="alert">{e(error)}</p>' if error else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in to Just Job</title>'
        f'<style>{CSS}</style></head><body class="login-page">'
        '<form class="login" method="post" action="/login"><span class="brand">Just Job</span>'
        '<label for="pw">Password</label>'
        '<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>'
        f'<input type="hidden" name="next" value="{e(safe_next(next_url))}">{msg}'
        '<button class="btn apply" type="submit">Sign in</button></form></body></html>'
    )


class Handler(BaseHTTPRequestHandler):
    db_path = ""
    cfg: dict = {}
    companies = 0
    password = ""          # DASHBOARD_PASSWORD; empty = no sign-in (fine on your own computer)
    key = b""
    failures: dict[str, list[float]] = {}
    failures_lock = threading.Lock()
    on_rules_changed: Callable[[], None] | None = None   # starts a re-filter after profile edits

    def log_message(self, fmt, *args):
        log.debug("dashboard: " + fmt, *args)

    def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8",
              headers: dict | None = None) -> None:
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self._send(303, "", "text/plain", {"Location": location, **({"Set-Cookie": cookie} if cookie else {})})

    def _client(self) -> str:
        # Behind a proxy on this machine (Caddy), the visitor's address is in X-Forwarded-For.
        peer = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For")
        return forwarded.split(",")[0].strip() if forwarded and is_loopback(peer) else peer

    def _signed_in(self) -> bool:
        if not self.password:
            return True
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return False
        morsel = cookie.get(SESSION_COOKIE)
        return bool(morsel) and valid_session(self.key, morsel.value)

    def _allowed(self) -> bool:
        """Refuses everything when the dashboard is reachable through a proxy without a password."""
        if not self.password and self.headers.get("X-Forwarded-For"):
            self._send(403, "Set DASHBOARD_PASSWORD in .env to use the dashboard from another computer.", "text/plain")
            return False
        return True

    def _cookie(self, value: str, max_age: int) -> str:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"{SESSION_COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"

    def _form(self, limit: int = 10_000, blank: bool = False) -> dict[str, list[str]]:
        length = min(int(self.headers.get("Content-Length") or 0), limit)
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=blank)

    def do_GET(self):
        if not self._allowed():
            return
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/login":
            if self._signed_in():
                return self._redirect(safe_next(qs.get("next", ["/"])[0]))
            return self._send(200, login_html(next_url=qs.get("next", ["/"])[0]))
        if url.path == "/logout":
            return self._redirect("/login", self._cookie("", 0))
        if url.path not in ("/", "/description", "/profile", "/profile/resume", "/apply", "/apply/status", "/apply/shot",
                            "/answers"):
            return self._send(404, "not found", "text/plain")
        if not self._signed_in():
            if url.path in ("/description", "/profile/resume", "/apply/status", "/apply/shot"):
                return self._send(401, "Sign in again.", "text/plain")
            return self._redirect("/login?" + urllib.parse.urlencode({"next": self.path}))
        db = DB(self.db_path)
        try:
            if url.path.startswith("/apply"):
                return self._get_apply(db, url.path, qs.get("job", [""])[0])
            if url.path == "/profile":
                prof = profile.load(db, self.cfg)
                notice = profile_page.notice_for(qs.get("done", [""])[0], qs.get("n", [""])[0])
                warn = profile_page.warning_for(qs.get("bad", [""])[0].split(","))
                return self._send(200, profile_page.render(prof, bool(ai.providers(self.cfg)), notice, warn))
            if url.path == "/answers":
                return self._send(200, answers_page.render(db.memory_all(), db.submitted_applications(),
                                                           memory.trust_after(self.cfg),
                                                           answers_page.notice_for(qs.get("done", [""])[0])))
            if url.path == "/profile/resume":
                path = profile.resume_path(Path(self.db_path).parent, profile.load(db, self.cfg))
                if not path:
                    return self._send(404, "No resume uploaded yet.", "text/plain")
                return self._send(200, path.read_bytes(), RESUME_TYPES[path.suffix],
                                  {"Content-Disposition": f'inline; filename="resume{path.suffix}"'})
            if url.path == "/description":
                row = db.q("SELECT description FROM jobs WHERE id = ?", (qs.get("id", [""])[0],))
                if not row:
                    return self._send(404, "unknown job", "text/plain")
                return self._send(200, row[0][0], "text/plain; charset=utf-8")
            self._send(200, dashboard_html(db, self.cfg, Filters.from_query(qs), self.companies, bool(self.password)))
        finally:
            db.close()

    def do_POST(self):
        if not self._allowed():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self._login()
        action = {"/status": self._post_status, "/profile": self._post_profile,
                  "/profile/resume": self._post_resume, "/profile/parse": self._post_parse,
                  "/apply/draft": self._post_draft, "/apply/fill": self._post_fill,
                  "/answers/save": self._post_answer_save, "/answers/delete": self._post_answer_delete}.get(path)
        if not action:
            return self._send(404, "not found", "text/plain")
        # Only accept requests from this page, not from other sites open in the browser.
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, "forbidden", "text/plain")
        if not self._signed_in():
            return self._send(401, "Sign in again.", "text/plain")
        action()

    def _post_status(self) -> None:
        form = self._form()
        job_id, status = form.get("id", [""])[0], form.get("status", [""])[0]
        if status not in STATUSES:
            return self._send(400, "bad status", "text/plain")
        db = DB(self.db_path)
        try:
            ok = db.set_status(job_id, status, now_iso())
        finally:
            db.close()
        self._send(200 if ok else 404, "ok" if ok else "unknown job", "text/plain")

    # -- apply agent --------------------------------------------------------------------------

    def _get_apply(self, db: DB, path: str, job_id: str) -> None:
        job = apply_agent.job_row(db, job_id)
        if not job:
            return self._send(404, "Unknown job.", "text/plain")
        if path == "/apply/status":
            return self._send(200, json.dumps(apply_agent.status(db, job_id)), "application/json")
        app = db.get_application(job_id)
        if path == "/apply/shot":
            name = (app or {}).get("screenshot") or ""
            shot = Path(self.db_path).parent / "applications" / name
            if not name or "/" in name or "\\" in name or not shot.exists():
                return self._send(404, "No confirmation screenshot.", "text/plain")
            return self._send(200, shot.read_bytes(), "image/png")
        prof = profile.load(db, self.cfg)
        questions, answers = apply_agent.parts(app) if app else ([], {})
        warns = apply_agent.warnings(db, self.cfg, job, questions, answers, prof) if app else []
        self._send(200, apply_page.render(job, app, questions, answers, warns, (prof.get("resume") or {}).get("name"),
                                          apply_browser.LIVE.get(job_id)))

    def _post_draft(self) -> None:
        job_id = self._form().get("job", [""])[0]
        if apply_browser.LIVE.get(job_id, {}).get("state") in ("filling", "waiting"):
            return self._send(409, "This form is open in the browser right now.", "text/plain; charset=utf-8")
        db = DB(self.db_path)
        try:
            apply_agent.draft(db, self.cfg, job_id)
        except FormError as err:
            return self._send(400, str(err), "text/plain; charset=utf-8")
        except Exception as err:   # network trouble, an ATS changing its API, ...
            log.exception("apply: drafting %s failed", job_id)
            return self._send(502, f"Couldn't read the form: {str(err)[:200]}", "text/plain; charset=utf-8")
        finally:
            db.close()
        self._send(200, "ok", "text/plain")

    def _post_fill(self) -> None:
        form = self._form(limit=200_000, blank=True)
        job_id = form.get("job", [""])[0]
        db = DB(self.db_path)
        try:
            if not db.get_application(job_id):
                return self._send(400, "Draft the answers first.", "text/plain; charset=utf-8")
            apply_agent.approve(db, job_id, form)
            err = apply_agent.fill(db, self.cfg, job_id, self.db_path, Path(self.db_path).parent)
        finally:
            db.close()
        self._send(409 if err else 200, err or "ok", "text/plain; charset=utf-8")

    # -- saved answers ----------------------------------------------------------------------

    def _post_answer_save(self) -> None:
        """Your edit on the Answers page. You chose it there, so it fills without asking from now on."""
        form = self._form(limit=50_000, blank=True)
        db = DB(self.db_path)
        try:
            row = db.memory_get(form.get("key", [""])[0])
            if not row:
                return self._send(404, "That answer isn't saved anymore.", "text/plain; charset=utf-8")
            q = answers_page.as_question(row)
            raw = [v.strip() for v in form.get("value", [])]
            value = fit(q, raw if q.kind == "multiselect" else (raw[0] if raw else ""))
            done = "invalid" if value is None else "saved"
            if value is not None:
                db.memory_edit(row["key"], value, max(row["confirmed"], memory.trust_after(self.cfg)), now_iso())
        finally:
            db.close()
        self._redirect(f"/answers?done={done}")

    def _post_answer_delete(self) -> None:
        db = DB(self.db_path)
        try:
            db.memory_delete(self._form().get("key", [""])[0])
        finally:
            db.close()
        self._redirect("/answers?done=deleted")

    # -- profile ----------------------------------------------------------------------------

    def _profile_saved(self, db: DB, old: dict, new: dict) -> str:
        """Make the running server use the new profile; re-filter stored jobs if the rules care."""
        profile.apply_to_cfg(self.cfg, new)
        if not profile.rules_changed(old, new):
            return "saved"
        db.set_meta(REFILTER_PENDING, "1")
        if self.on_rules_changed:
            self.on_rules_changed()
        return "saved-refilter"

    def _post_profile(self) -> None:
        form = self._form(limit=100_000, blank=True)
        db = DB(self.db_path)
        try:
            old = profile.load(db, self.cfg)
            new, bad = profile.from_form(form, old)
            profile.save(db, new)
            query = {"done": self._profile_saved(db, old, new), **({"bad": ",".join(bad)} if bad else {})}
        finally:
            db.close()
        self._redirect("/profile?" + urllib.parse.urlencode(query))

    def _post_resume(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > profile.MAX_RESUME_BYTES:
            return self._send(413, "That file is over 8 MB.", "text/plain; charset=utf-8")
        data = self.rfile.read(length)
        name = urllib.parse.unquote(self.headers.get("X-Filename") or "")
        db = DB(self.db_path)
        try:
            old = profile.load(db, self.cfg)
            new = profile.store_resume(db, self.cfg, Path(self.db_path).parent, name, data)
            self._profile_saved(db, old, new)
        except profile.ResumeError as err:
            return self._send(400, str(err), "text/plain; charset=utf-8")
        finally:
            db.close()
        self._send(200, "ok", "text/plain")

    def _post_parse(self) -> None:
        db = DB(self.db_path)
        try:
            prof = profile.load(db, self.cfg)
            if not prof.get("resume_text"):
                return self._send(400, "Upload a resume first.", "text/plain; charset=utf-8")
            try:
                facts, model = profile.parse_resume(self.cfg, prof["resume_text"])
            except profile.ResumeError as err:
                return self._send(502, str(err), "text/plain; charset=utf-8")
            old = dict(prof)
            filled = profile.fill_from_facts(prof, facts, model)
            profile.save(db, prof)
            self._profile_saved(db, old, prof)
        finally:
            db.close()
        self._send(200, str(len(filled)), "text/plain")

    def _login(self) -> None:
        form = self._form()
        next_url = safe_next(form.get("next", ["/"])[0])
        if not self.password:
            return self._redirect(next_url)
        who, now = self._client(), time.time()
        with self.failures_lock:
            recent = [t for t in self.failures.get(who, []) if now - t < FAILURE_WINDOW]
            self.failures[who] = recent
        if len(recent) >= MAX_FAILURES:
            return self._send(429, login_html("Too many wrong passwords. Try again in 10 minutes.", next_url))
        if hmac.compare_digest(form.get("password", [""])[0].encode(), self.password.encode()):
            with self.failures_lock:
                self.failures.pop(who, None)
            return self._redirect(next_url, self._cookie(make_session(self.key), SESSION_DAYS * 86400))
        with self.failures_lock:
            self.failures.setdefault(who, []).append(now)
        time.sleep(1)   # makes guessing slow
        self._send(401, login_html("Wrong password. Try again.", next_url))


RESUME_TYPES = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def serve(cfg: dict, db_path: str, port: int, open_browser: bool = True, host: str = "127.0.0.1",
          on_rules_changed: Callable[[], None] | None = None) -> None:
    Handler.db_path = db_path
    Handler.cfg = cfg
    Handler.on_rules_changed = staticmethod(on_rules_changed) if on_rules_changed else None
    Handler.companies = sum(len(registry.load(ats)) for ats in ATS)
    Handler.password = os.environ.get("DASHBOARD_PASSWORD", "")
    if Handler.password:
        Handler.key = session_key(Handler.password, load_secret(Path(db_path).parent / "session.secret"))
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"Dashboard at {url}{' (sign-in required)' if Handler.password else ''}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
