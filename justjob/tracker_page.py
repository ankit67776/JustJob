"""The dashboard's Applications board and the page for one application: its stage, timeline and notes."""
from __future__ import annotations

import urllib.parse
from datetime import date, datetime

from . import tracker as T
from .ui import CSS, e, monogram, top_bar

JS = """
const post = (path, data) => fetch(path, {method: 'POST', body: new URLSearchParams(data)});
document.querySelectorAll('select.move').forEach((s) => s.addEventListener('change', async () => {
  s.disabled = true;
  await post('/tracker/stage', {id: s.dataset.id, stage: s.value});
  location.reload();
}));
// Drag a card onto a column, or onto one of the Closed column's outcomes.
let dragged = null;
document.querySelectorAll('.tcard').forEach((c) => {
  c.addEventListener('dragstart', (ev) => { dragged = c; c.classList.add('dragging'); ev.dataTransfer.effectAllowed = 'move'; });
  c.addEventListener('dragend', () => { c.classList.remove('dragging'); dragged = null;
    document.querySelectorAll('.over').forEach((z) => z.classList.remove('over')); });
});
document.querySelectorAll('[data-drop]').forEach((z) => {
  z.addEventListener('dragover', (ev) => { if (dragged) { ev.preventDefault(); z.classList.add('over'); } });
  z.addEventListener('dragleave', () => z.classList.remove('over'));
  z.addEventListener('drop', async (ev) => {
    ev.preventDefault();
    z.classList.remove('over');
    if (!dragged || dragged.dataset.stage === z.dataset.drop) return;
    dragged.style.opacity = '.4';
    await post('/tracker/stage', {id: dragged.dataset.id, stage: z.dataset.drop});
    location.reload();
  });
});
"""


def _day(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y").lstrip("0")
    except (TypeError, ValueError):
        return ""


def _ago(iso: str | None, now: str) -> str:
    days = T.days_between(iso, now)
    if days is None:
        return ""
    n = int(days)
    return "today" if n <= 0 else "yesterday" if n == 1 else f"{n}d ago"


def job_link(job_id: str) -> str:
    return "/tracker/job?" + urllib.parse.urlencode({"id": job_id})


def stage_select(row: dict) -> str:
    current = row["stage"] or "applied"
    opts = "".join(f'<option value="{k}"{" selected" if k == current else ""}>{e(v)}</option>' for k, v in T.STAGES.items())
    ends = "".join(f'<option value="{k}"{" selected" if k == current else ""}>{e(v)}</option>' for k, v in T.OUTCOMES.items())
    return (f'<select class="move" data-id="{e(row["id"])}" aria-label="Stage of {e(row["title"])} at {e(row["company"])}">'
            f'<optgroup label="Stage">{opts}</optgroup><optgroup label="Closed">{ends}</optgroup></select>')


def card(row: dict, now: str) -> str:
    letters, hue = monogram(row["company"] or "?")
    stage = row["stage"] or "applied"
    chips = []
    if stage in T.OUTCOMES:
        chips.append(f'<span class="chip end {e(stage)}">{e(T.OUTCOMES[stage])}</span>')
    nudge = T.nudge(row, now)
    quiet = T.days_between(row.get("active_at") or row.get("stage_at"), now)
    if nudge == "no_reply":
        chips.append(f'<span class="chip warn" title="Nothing back in {int(quiet)} days">No reply in 3 weeks</span>')
    elif nudge == "follow_up":
        chips.append(f'<span class="chip warn" title="Quiet for {int(quiet)} days">Follow up</span>')
    if row.get("agent_at"):
        chips.append('<span class="chip">Sent by the agent</span>')
    if row.get("closed") and T.is_open(stage):
        chips.append('<span class="chip">Posting closed</span>')
    since = _ago(row.get("stage_at"), now)
    moved = f"{T.LABELS.get(stage, stage)} {since}" if stage != "applied" else f"Applied {since}"
    close = ""
    if nudge == "no_reply":
        close = (f'<form method="post" action="/tracker/stage"><input type="hidden" name="id" value="{e(row["id"])}">'
                 '<input type="hidden" name="stage" value="no_response">'
                 '<button class="linkish" type="submit">Close as no response</button></form>')
    return (f'<article class="tcard" draggable="true" data-id="{e(row["id"])}" data-stage="{e(stage)}">'
            f'<div class="mono" style="--h:{hue}" aria-hidden="true">{e(letters)}</div>'
            f'<div class="tbody"><a class="t" href="{e(job_link(row["id"]))}">{e(row["title"])}</a>'
            f'<div class="co">{e(row["company"])}</div>'
            f'<div class="tmeta"><span>{e(T.where_from(row))}</span><span>{e(moved)}</span></div>'
            + (f'<div class="chips">{"".join(chips)}</div>' if chips else "")
            + f'{stage_select(row)}{close}</div></article>')


ADD_FORM = """<details class="card add-app"{open}><summary>Add an application you made elsewhere</summary>
<form method="post" action="/tracker/add" class="fields">
<div class="field"><label for="a-company">Company<span class="req">*</span></label><input id="a-company" name="company" required></div>
<div class="field"><label for="a-title">Job title<span class="req">*</span></label><input id="a-title" name="title" required></div>
<div class="field"><label for="a-url">Link to the posting</label><input id="a-url" name="url" type="url" placeholder="https://…"></div>
<div class="field"><label for="a-location">Location</label><input id="a-location" name="location" placeholder="e.g. Bengaluru, or Remote"></div>
<div class="field"><label for="a-via">Where you found it</label><input id="a-via" name="via" placeholder="e.g. LinkedIn, Referral" list="via-list">
<datalist id="via-list"><option>LinkedIn</option><option>Referral</option><option>Naukri</option><option>Company website</option>
<option>Wellfound</option><option>Recruiter</option></datalist></div>
<div class="field"><label for="a-date">Date applied</label><input id="a-date" name="applied" type="date" value="{today}"></div>
<div class="add-actions"><button class="btn apply" type="submit">Add to Applied</button></div>
</form></details>"""


def render_board(rows: list[dict], now: str, notice: str = "", show_add: bool = False) -> str:
    cols = {k: [] for k in T.STAGES}
    closed = []
    for r in rows:
        (cols[r["stage"] or "applied"] if T.is_open(r["stage"]) else closed).append(r)
    waiting = sum(1 for r in rows if T.nudge(r, now))
    open_n = len(rows) - len(closed)
    sections = []
    for key, label in T.STAGES.items():
        cards = "".join(card(r, now) for r in cols[key]) or '<p class="empty">Nothing here.</p>'
        sections.append(f'<section class="col" data-drop="{key}" aria-label="{e(label)}"><header><h2>{e(label)}</h2>'
                        f'<span class="n">{len(cols[key])}</span></header><div class="cards">{cards}</div></section>')
    zones = "".join(f'<span class="zone" data-drop="{k}">{e(v)}</span>' for k, v in T.OUTCOMES.items())
    closed_cards = "".join(card(r, now) for r in closed) or '<p class="empty">Nothing closed yet.</p>'
    sections.append(f'<section class="col closed" aria-label="Closed"><header><h2>Closed</h2><span class="n">{len(closed)}</span>'
                    f'</header><div class="zones" aria-hidden="true">{zones}</div><div class="cards">{closed_cards}</div></section>')
    summary = (f"{open_n} open" + (f", {waiting} waiting on a follow-up" if waiting else "")
               + f", {len(closed)} closed." if rows else "Nothing yet. Jobs you mark as applied show up here.")
    body = (f'<div class="page-head row-head"><div><h1>Applications</h1><p>{e(summary)} Drag a card to another column, '
            'or pick its stage. Open one for its timeline and notes.</p></div>'
            '<a class="btn" href="/insights">See insights</a></div>'
            + (f'<p class="notice" role="status">{e(notice)}</p>' if notice else "")
            + ADD_FORM.format(open=" open" if show_add else "", today=date.today().isoformat())
            + f'<div class="board">{"".join(sections)}</div>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Applications · Just Job</title>'
            f'<style>{CSS}</style></head><body>{top_bar("tracker")}<main class="track-wrap">{body}</main>'
            f'<script>{JS}</script></body></html>')


def stepper(row: dict, events: list[dict]) -> str:
    reached = T.furthest(events)
    when = {}
    for ev in events:
        if ev["kind"] == "stage" and ev["stage"] in T.STAGES:
            when.setdefault(ev["stage"], ev["at"])
    stage = row["stage"] or "applied"
    steps = []
    for i, (key, label) in enumerate(T.STAGES.items()):
        state = "now" if key == stage else "done" if i <= reached else "todo"
        day = f'<span class="d">{e(_day(when.get(key)))}</span>' if key in when else ""
        steps.append(f'<li class="{state}"><span class="dot" aria-hidden="true"></span><span class="l">{e(label)}</span>{day}</li>')
    return f'<ol class="stepper" aria-label="Stages">{"".join(steps)}</ol>'


def stage_buttons(row: dict) -> str:
    stage = row["stage"] or "applied"
    hidden = f'<input type="hidden" name="id" value="{e(row["id"])}"><input type="hidden" name="next" value="job">'
    moves = "".join(f'<button class="btn{" on" if k == stage else ""}" name="stage" value="{k}" type="submit"'
                    f'{" disabled" if k == stage else ""}>{e(v)}</button>' for k, v in T.STAGES.items())
    ends = "".join(f'<button class="btn{" on" if k == stage else ""}" name="stage" value="{k}" type="submit"'
                   f'{" disabled" if k == stage else ""}>{e(v)}</button>' for k, v in T.OUTCOMES.items())
    return (f'<form method="post" action="/tracker/stage" class="moves">{hidden}'
            f'<div class="group"><span class="glabel">Move to</span>{moves}</div>'
            f'<div class="group"><span class="glabel">Close as</span>{ends}</div>'
            '<input class="note-in" name="note" placeholder="Note for this move (optional), e.g. Recruiter call on Monday">'
            '</form>')


def timeline(events: list[dict]) -> str:
    items = []
    for ev in reversed(events):
        if ev["kind"] == "stage":
            what = "Applied" if ev["stage"] == "applied" else f"Moved to {T.LABELS.get(ev['stage'], ev['stage'])}"
            items.append(f'<li class="ev stage"><div class="when">{e(_day(ev["at"]))}</div><div><b>{e(what)}</b>'
                         + (f'<p>{e(ev["note"])}</p>' if ev["note"] else "") + "</div></li>")
        else:
            items.append(f'<li class="ev note"><div class="when">{e(_day(ev["at"]))}</div><div><p>{e(ev["note"])}</p></div></li>')
    return f'<ol class="timeline">{"".join(items)}</ol>' if items else '<p class="empty">No history yet.</p>'


def render_job(row: dict, events: list[dict], now: str, has_application: bool) -> str:
    stage = row["stage"] or "applied"
    links = (f'<a class="btn" href="{e(row["url"])}" target="_blank" rel="noopener">Open posting</a>' if row["url"] else "")
    if has_application:
        links += f'<a class="btn" href="/apply?{e(urllib.parse.urlencode({"job": row["id"]}))}">Form answers</a>'
    meta = "".join(f"<span>{e(x)}</span>" for x in (row["company"], row["location"], T.where_from(row)) if x)
    status = T.LABELS.get(stage, stage) + (" (closed)" if stage in T.OUTCOMES else "")
    body = (f'<a class="back" href="/tracker">← Applications</a>'
            f'<div class="job-head"><div><h1>{e(row["title"])}</h1><div class="meta">{meta}</div></div>'
            f'<div class="links">{links}</div></div>'
            f'<section class="card"><h2>{e(status)}</h2><p class="blurb">Applied {e(_day(T.applied_on(row, events)))}'
            f' · last update {e(_ago(row.get("active_at") or row.get("stage_at"), now))}</p>{stepper(row, events)}'
            f'{stage_buttons(row)}</section>'
            f'<section class="card"><h2>Timeline</h2>'
            f'<form method="post" action="/tracker/note" class="add-note"><input type="hidden" name="id" value="{e(row["id"])}">'
            '<textarea name="note" rows="2" placeholder="Add a note: who you spoke to, what\'s next, questions they asked" '
            'required></textarea><button class="btn apply" type="submit">Add note</button></form>'
            f'{timeline(events)}</section>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(row["title"])} · Applications · Just Job</title>'
            f'<style>{CSS}</style></head><body>{top_bar("tracker")}<main class="apply-wrap">{body}</main></body></html>')


def notice_for(done: str) -> str:
    """The banner after a redirect. Only fixed texts, so nothing from the URL is shown as is."""
    return {"added": "Added to Applied.", "moved": "Stage updated.", "noted": "Note added.",
            "missing": "Add a company and a job title."}.get(done, "")
