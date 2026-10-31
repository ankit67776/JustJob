"""HTML rendering (shared by the digest file and the dashboard) and the local dashboard server."""
from __future__ import annotations

import hashlib
import hmac
import html
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
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import registry
from .db import DB, score_sql, SHOWABLE, STATUSES
from .sources import ATS, DISPLAY, source_label
from .text import now_iso

log = logging.getLogger(__name__)

CSS = """
:root {
  --paper:#F3F5F4; --surface:#FFFFFF; --ink:#17201D; --muted:#5E6A66; --faint:#8B9692;
  --line:#DCE2DF; --line-soft:#E9EDEB; --hover:#F8FAF9;
  --jade:#0E7A61; --jade-ink:#0A5C49; --jade-wash:#E2F0EB; --amber:#A86600; --amber-wash:#FBF0DD;
  --num:"Bahnschrift","DIN Alternate","DIN 2014","Barlow",system-ui,sans-serif;
  --text:"Segoe UI Variable Text","Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
}
* { box-sizing:border-box; }
html { scroll-padding-top:90px; }
body { margin:0; background:var(--paper); color:var(--ink); font:14px/1.5 var(--text); -webkit-font-smoothing:antialiased; }
a { color:inherit; }
:focus-visible { outline:2px solid var(--jade); outline-offset:2px; border-radius:4px; }
.num, .n, .count { font-family:var(--num); font-variant-numeric:tabular-nums; }

/* top bar */
.top { position:sticky; top:0; z-index:10; background:var(--surface); border-bottom:1px solid var(--line);
       display:grid; grid-template-columns:240px minmax(0,1fr) auto; gap:28px; align-items:center; padding:10px 28px; }
.brand { font-family:var(--num); font-size:19px; font-weight:600; text-decoration:none; display:flex; align-items:center; gap:10px; }
.brand::before { content:""; width:12px; height:12px; border-radius:3px; background:var(--jade); }
.search { position:relative; max-width:560px; }
.search svg { position:absolute; left:12px; top:50%; translate:0 -50%; color:var(--faint); pointer-events:none; }
.search input { width:100%; padding:9px 40px 9px 36px; border:1px solid var(--line); border-radius:9px; background:var(--paper);
                font:inherit; color:var(--ink); }
.search input:focus { background:var(--surface); border-color:var(--jade); outline:none; box-shadow:0 0 0 3px var(--jade-wash); }
.search kbd { position:absolute; right:10px; top:50%; translate:0 -50%; font:12px var(--num); color:var(--faint);
              border:1px solid var(--line); border-radius:5px; padding:0 6px; background:var(--surface); }
.tabs { display:flex; gap:2px; padding:3px; background:var(--paper); border-radius:9px; }
.tabs a { padding:6px 12px; border-radius:7px; color:var(--muted); text-decoration:none; white-space:nowrap; }
.tabs a .n { margin-left:6px; color:var(--faint); }
.tabs a.on { background:var(--surface); color:var(--ink); font-weight:600; box-shadow:0 0 0 1px var(--line); }
.tabs a.on .n { color:var(--jade); }
.top .filters-btn { display:none; }
.top > *, .layout > *, .row > * { min-width:0; }   /* let grid columns shrink on small screens */

/* layout */
.layout { display:grid; grid-template-columns:240px minmax(0,1fr); gap:28px; max-width:1280px; margin:0 auto; padding:22px 28px 80px; }
.rail { position:sticky; top:78px; align-self:start; max-height:calc(100vh - 96px); overflow:auto; padding-right:6px; }
.facet { border:0; margin:0; padding:0 0 14px; }
.facet + .facet { border-top:1px solid var(--line); padding-top:14px; }
.facet legend { float:left; width:100%; padding:0; margin-bottom:6px; font-weight:600; font-size:13.5px; }
.facet legend + * { clear:both; }
.opt { display:flex; align-items:center; gap:10px; padding:5px 8px; margin:0 -8px; border-radius:7px; cursor:pointer; }
.opt:hover { background:var(--surface); }
.opt input { accent-color:var(--jade); width:15px; height:15px; margin:0; flex:none; }
.opt .n { margin-left:auto; font-size:12.5px; color:var(--faint); }
.opt.zero { color:var(--faint); }
.opt:has(input:checked) { color:var(--jade-ink); font-weight:600; }
.opt:has(input:checked) .n { color:var(--jade); }
.facet details summary { cursor:pointer; color:var(--muted); font-size:13px; padding:4px 0; list-style:none; }
.facet details summary::-webkit-details-marker { display:none; }
.facet details[open] summary { display:none; }
.rail-foot { margin-top:18px; padding-top:14px; border-top:1px solid var(--line); color:var(--muted); font-size:12.5px; }
.rail-foot p { margin:0 0 6px; }
.rail-foot .num { color:var(--ink); font-size:14px; }
.keys { color:var(--faint); }
.keys kbd { font:11.5px var(--num); border:1px solid var(--line); border-radius:4px; padding:0 4px; background:var(--surface); color:var(--muted); }

/* results */
.results-head { display:flex; align-items:flex-end; gap:16px; margin:2px 0 12px; }
.results-head .count { font-size:32px; font-weight:600; line-height:1; }
.results-head .what { color:var(--muted); padding-bottom:3px; }
.head-tools { margin-left:auto; display:flex; align-items:center; gap:8px; }
.head-tools .range { color:var(--muted); font:13px var(--num); font-variant-numeric:tabular-nums; margin-right:4px; white-space:nowrap; }
.head-tools .mini { display:inline-grid; place-items:center; width:30px; height:32px; border-radius:7px; text-decoration:none;
                    color:var(--ink); border:1px solid var(--line); background:var(--surface); }
.head-tools .mini.off { color:var(--line); }
.results-head select { padding:7px 10px; border:1px solid var(--line); border-radius:8px; background:var(--surface);
                       font:inherit; color:var(--ink); }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 14px; }
.chip { display:inline-flex; align-items:center; gap:6px; padding:3px 6px 3px 11px; border-radius:999px; background:var(--jade-wash);
        color:var(--jade-ink); font-size:12.5px; font-weight:500; text-decoration:none; }
.chip b { display:grid; place-items:center; width:17px; height:17px; border-radius:50%; font-weight:400; }
.chip:hover b { background:var(--jade); color:#fff; }
.chip.clear { background:none; color:var(--muted); text-decoration:underline; text-underline-offset:3px; padding-left:4px; }

.list { background:var(--surface); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
.row { position:relative; display:grid; grid-template-columns:42px minmax(0,1fr) 86px 112px; gap:18px; padding:16px 20px;
       border-top:1px solid var(--line-soft); transition:opacity .2s, max-height .25s; max-height:800px; }
.row:first-child { border-top:0; }
.row:hover { background:var(--hover); }
.row.cur { background:var(--hover); }
.row.cur::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--jade); }
.row.leaving { opacity:0; max-height:0; padding-top:0; padding-bottom:0; overflow:hidden; }
.mono { width:42px; height:42px; border-radius:10px; display:grid; place-items:center; font:600 15px var(--num);
        background:hsl(var(--h) 38% 92%); color:hsl(var(--h) 45% 28%); }
.title { font-size:15.5px; font-weight:600; text-decoration:none; }
.title:visited { color:var(--muted); }   /* postings you've already opened */
.title:hover { color:var(--jade-ink); text-decoration:underline; text-underline-offset:3px; }
.new { display:inline-block; margin-left:8px; padding:0 7px; border-radius:999px; background:var(--jade); color:#fff;
       font-size:11.5px; font-weight:600; vertical-align:2px; }
.meta { display:flex; flex-wrap:wrap; gap:2px 14px; margin-top:3px; color:var(--muted); font-size:13px; }
.meta .company { color:var(--ink); font-weight:500; }
.meta .remote { color:var(--jade-ink); font-weight:600; }
.meta .src { color:var(--faint); }
.why { margin:8px 0 0; color:var(--muted); font-size:13px; max-width:72ch; }
.skills { display:flex; flex-wrap:wrap; align-items:center; gap:5px; margin-top:8px; font-size:12px; }
.skills .lead { color:var(--faint); margin-right:2px; }
.skill { padding:1px 8px; border-radius:999px; border:1px solid var(--line); color:var(--ink); }
.skills .rest { color:var(--faint); }
.flag { padding:1px 8px; border-radius:999px; background:var(--amber-wash); color:var(--amber); }
.desc { margin-top:8px; }
.desc summary { cursor:pointer; color:var(--muted); font-size:13px; width:max-content; }
.desc summary:hover { color:var(--ink); }
.desc pre { white-space:pre-wrap; font:13px/1.6 var(--text); background:var(--paper); border-radius:8px; padding:12px 14px;
            max-height:440px; overflow:auto; margin:8px 0 0; max-width:80ch; }
.score { text-align:right; }
.score .num { display:block; font-size:30px; font-weight:600; line-height:1; }
.score .bar { display:block; height:3px; margin:9px 0 5px auto; width:56px; border-radius:2px; background:var(--line-soft); overflow:hidden; }
.score .bar i { display:block; height:100%; background:currentColor; }
.score .lbl { display:block; font-size:11.5px; color:var(--faint); }
.hi { color:var(--jade); } .mid { color:var(--amber); } .lo { color:var(--faint); }
.side { display:flex; flex-direction:column; gap:6px; }
.posted { font:13px var(--num); color:var(--muted); text-align:right; margin-bottom:2px; }
.posted.fresh { color:var(--jade); font-weight:600; }
.btn { display:block; text-align:center; padding:6px 10px; border-radius:8px; border:1px solid var(--line); background:var(--surface);
       color:var(--ink); text-decoration:none; cursor:pointer; font:inherit; font-size:13px; }
.btn:hover { border-color:var(--faint); }
.btn.apply { background:var(--jade); border-color:var(--jade); color:#fff; font-weight:600; }
.btn.apply:hover { background:var(--jade-ink); }
.minor { display:flex; gap:6px; }
.minor .btn { flex:1; padding:4px 0; color:var(--muted); }
.pager { display:flex; flex-wrap:wrap; justify-content:center; align-items:center; gap:4px; margin:20px 0 6px; }
.pager a, .pager span { min-width:36px; height:36px; padding:0 10px; display:inline-grid; place-items:center; border-radius:8px;
                        font:14px var(--num); font-variant-numeric:tabular-nums; text-decoration:none; color:var(--ink); }
.pager a:hover { background:var(--surface); box-shadow:0 0 0 1px var(--line); }
.pager .cur { background:var(--ink); color:#fff; font-weight:600; }
.pager .gap { min-width:18px; padding:0; color:var(--faint); }
.pager .step { font:14px var(--text); color:var(--muted); }
.pager .step.off { color:var(--line); }
.showing { text-align:center; color:var(--faint); font-size:12.5px; }
.empty { padding:64px 24px; text-align:center; color:var(--muted); }
.empty strong { display:block; color:var(--ink); font-size:16px; margin-bottom:4px; }
.toast { position:fixed; left:50%; bottom:24px; translate:-50% 0; display:flex; gap:14px; align-items:center; padding:10px 12px 10px 16px;
         border-radius:10px; background:var(--ink); color:#fff; font-size:13.5px; opacity:0; pointer-events:none; transition:opacity .2s; }
.toast.on { opacity:1; pointer-events:auto; }
.toast button { background:none; border:0; color:#9FE3CF; font:inherit; font-weight:600; cursor:pointer; padding:2px 4px; }

/* sign-in page */
.login-page { display:grid; place-items:center; min-height:100vh; padding:16px; }
.login { width:min(360px, 100%); background:var(--surface); border:1px solid var(--line); border-radius:14px;
         padding:28px; display:grid; gap:10px; }
.login .brand { margin-bottom:12px; }
.login label { font-weight:600; font-size:13.5px; }
.login input { padding:10px 12px; border:1px solid var(--line); border-radius:9px; font:inherit; color:var(--ink);
               background:var(--paper); }
.login input:focus { outline:none; border-color:var(--jade); box-shadow:0 0 0 3px var(--jade-wash); background:var(--surface); }
.login .btn { padding:10px; font-size:14px; margin-top:6px; }
.login-error { margin:0; color:#B42318; font-size:13px; }

/* digest file */
.digest main { max-width:900px; margin:0 auto; padding:22px 28px 60px; }
.digest h2 { font:600 18px var(--num); margin:26px 0 10px; }
.digest .top { grid-template-columns:1fr; }
.meta-line { color:var(--muted); }

@media (max-width:960px) {
  .top { grid-template-columns:1fr auto; gap:12px; padding:10px 16px; }
  .search { grid-column:1 / -1; grid-row:2; max-width:none; }
  .tabs { grid-column:1 / -1; grid-row:3; overflow:auto; }
  .top .filters-btn { display:inline-block; grid-column:2; grid-row:1; }
  .layout { grid-template-columns:1fr; padding:16px; }
  .rail { display:none; position:static; max-height:none; background:var(--surface); border:1px solid var(--line);
          border-radius:12px; padding:14px 16px; }
  body.show-filters .rail { display:block; }
  .row { grid-template-columns:38px minmax(0,1fr) 64px; gap:12px; padding:14px; }
  .side { grid-column:2 / -1; flex-direction:row; align-items:center; flex-wrap:wrap; }
  .side .apply { flex:1; }
  .minor { flex:none; }
  .minor .btn { padding:4px 8px; }
  .results-head .count { font-size:26px; }
  .results-head { flex-wrap:wrap; }
  .head-tools { margin-left:0; width:100%; }
  .head-tools .range { margin-right:auto; }
  .posted { text-align:left; margin:0 auto 0 0; }
  .score .num { font-size:24px; }
}
@media (prefers-reduced-motion: reduce) { * { transition:none !important; } }
"""

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
  if (e.key === 'a') row.querySelector('[data-status=applied]')?.click();
  if (e.key === 'h') row.querySelector('[data-status=hidden]')?.click();
  if (e.key === 'd') { const d = row.querySelector('details'); if (d) d.open = !d.open; }
});
"""

SEARCH_ICON = ('<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5" fill="none" '
               'stroke="currentColor" stroke-width="1.6"/><path d="M11 11l3.5 3.5" stroke="currentColor" stroke-width="1.6" '
               'stroke-linecap="round"/></svg>')

e = html.escape


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
    side += f'<a class="btn apply" href="{e(r["url"])}" target="_blank" rel="noopener">Apply</a>'
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
    def from_query(cls, qs: dict[str, list[str]]) -> "Filters":
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


def kept_jobs(db: DB, cfg: dict) -> list[dict]:
    """Every job the rules kept, with just what filtering, counting and sorting need. One indexed
    read; the full rows are loaded only for the jobs on screen."""
    mode = _mode_fn(cfg)
    rows = db.q(
        f"SELECT id, title, location, remote, source, salary != '' AS salary, status, status_at,"
        f" {POSTED_AT} AS posted, {YEARS} AS years, julianday('now') - julianday({POSTED_AT}) AS age,"
        f" {score_sql(cfg)} AS score FROM jobs WHERE {SHOWABLE}")
    return [{**dict(r), "role": role_of(r["title"]), "mode": mode(r["location"], r["remote"])} for r in rows]


def search_hits(db: DB, query: str) -> set[str] | None:
    terms = query.split()
    if not terms:
        return None
    one = "(title LIKE ? OR company LIKE ? OR location LIKE ? OR description LIKE ?)"
    params = [f"%{t}%" for t in terms for _ in range(4)]
    return {r[0] for r in db.q(f"SELECT id FROM jobs WHERE {SHOWABLE} AND {' AND '.join([one] * len(terms))}", params)}


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
    top = (
        f'<header class="top"><a class="brand" href="/">Just Job</a>'
        f'<div class="search">{SEARCH_ICON}<input id="q" name="q" form="filters" type="search" value="{e(f.q)}" '
        f'placeholder="Search roles, companies, skills or cities" aria-label="Search"><kbd>/</kbd></div>'
        f'<nav class="tabs" aria-label="Lists">{tabs}</nav>'
        f'<button class="btn filters-btn" type="button">Filters</button></header>'
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
    foot += ('<p class="keys"><kbd>j</kbd> <kbd>k</kbd> move, <kbd>o</kbd> open, <kbd>a</kbd> applied, '
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

    def log_message(self, fmt, *args):
        log.debug("dashboard: " + fmt, *args)

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", headers: dict | None = None) -> None:
        data = body.encode("utf-8")
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

    def _form(self) -> dict[str, list[str]]:
        length = min(int(self.headers.get("Content-Length") or 0), 10_000)
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

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
        if url.path not in ("/", "/description"):
            return self._send(404, "not found", "text/plain")
        if not self._signed_in():
            if url.path == "/description":
                return self._send(401, "Sign in again.", "text/plain")
            return self._redirect("/login?" + urllib.parse.urlencode({"next": self.path}))
        db = DB(self.db_path)
        try:
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
        if path != "/status":
            return self._send(404, "not found", "text/plain")
        # Only accept requests from this page, not from other sites open in the browser.
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, "forbidden", "text/plain")
        if not self._signed_in():
            return self._send(401, "Sign in again.", "text/plain")
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


def serve(cfg: dict, db_path: str, port: int, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    Handler.db_path = db_path
    Handler.cfg = cfg
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
