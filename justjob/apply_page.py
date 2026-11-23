"""The apply review page: every question on a job's form, the drafted answer, where it came from,
and what needs you. From here the agent opens the form in a browser and fills it."""
from __future__ import annotations

from datetime import datetime

from .apply.answers import Answer
from .apply.forms import Question
from .ui import CSS, e, top_bar

SOURCES = {"profile": "From your profile", "rule": "Rule", "memory": "Your earlier answer", "ai": "AI",
           "you": "Edited by you", "none": ""}
GROUPS = (("standard", "Basics"), ("custom", "Questions from {company}"), ("eeo", "Voluntary disclosures"))
ATS_NAMES = {"greenhouse": "Greenhouse", "ashby": "Ashby"}

JS = """
const job = document.body.dataset.job;
const form = document.getElementById('answers');
const fillBtn = document.getElementById('fill');
const redraft = document.getElementById('redraft');
const line = document.getElementById('apply-status');
const live = document.getElementById('live');
let timer = null;

function say(text, err) { if (line) { line.textContent = text; line.classList.toggle('err', !!err); } }
async function post(path, body) {
  const r = await fetch(path, {method: 'POST', body});
  return [r.ok, await r.text()];
}
function showLive(s) {
  live.hidden = false;
  live.dataset.state = s.state;
  live.querySelector('.msg').textContent = s.message || '';
  const ul = live.querySelector('ul');
  ul.replaceChildren(...(s.state === 'waiting' ? s.todo : []).map(t => {
    const li = document.createElement('li');
    li.textContent = t.label + ': ' + t.why;
    return li;
  }));
}
function poll() {
  clearInterval(timer);
  timer = setInterval(async () => {
    const r = await fetch('/apply/status?job=' + encodeURIComponent(job));
    if (!r.ok) return;
    const s = await r.json();
    showLive(s);
    if (['submitted', 'closed', 'failed'].includes(s.state)) {
      clearInterval(timer);
      if (fillBtn) fillBtn.disabled = false;
      if (s.state === 'submitted') setTimeout(() => location.reload(), 800);
    }
  }, 2000);
}
fillBtn?.addEventListener('click', async () => {
  const data = new URLSearchParams(new FormData(form));
  data.set('job', job);
  fillBtn.disabled = true;
  say('');
  showLive({state: 'filling', message: 'Opening the browser…', todo: []});
  const [ok, text] = await post('/apply/fill', data);
  if (!ok) { fillBtn.disabled = false; live.hidden = true; return say(text, true); }
  poll();
});
redraft?.addEventListener('click', async () => {
  redraft.disabled = true;
  say('Reading the form again and redrafting. Your edits are kept.');
  const [ok, text] = await post('/apply/draft', new URLSearchParams({job}));
  if (!ok) { redraft.disabled = false; return say(text, true); }
  location.reload();
});
(async () => {
  if (document.body.dataset.draft === '1') {
    const [ok, text] = await post('/apply/draft', new URLSearchParams({job}));
    if (ok) return location.reload();
    document.getElementById('drafting').hidden = true;
    const err = document.getElementById('draft-error');
    err.hidden = false;
    err.querySelector('.msg').textContent = text;
  }
  if (['filling', 'waiting'].includes(document.body.dataset.state)) poll();
})();
form?.addEventListener('input', () => say('Your edits are sent along when you press Fill in browser.'));
"""


def _when(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %I:%M %p").lstrip("0").replace(" 0", " ")
    except (TypeError, ValueError):
        return ""


def outcome_line(out: dict | None) -> str:
    """' 18 of 21 answers went in without an edit from you. 3 answers saved for next time.'"""
    if not out or not out.get("fields"):
        return ""
    n = len(out.get("saved") or [])
    return (f" {out['kept']} of {out['fields']} answers went in without an edit from you."
            + (f" {n} answer{'s' if n != 1 else ''} saved for next time (see Answers)." if n else ""))


def control(q: Question, a: Answer | None, resume_name: str | None) -> str:
    value = a.value if a else None
    name = f'name="{e(q.id)}" id="q-{e(q.id)}"'
    if q.kind == "file":
        if value == "resume" and resume_name:
            return f'<div class="file-note">Your resume, <b>{e(resume_name)}</b>, is attached automatically.</div>'
        return '<div class="file-note">Attach this in the browser window.</div>'
    if q.kind == "multiselect":
        chosen = set(value or [])
        return '<div class="checks">' + "".join(
            f'<label><input type="checkbox" name="{e(q.id)}" value="{e(o)}"{" checked" if o in chosen else ""}>{e(o)}</label>'
            for o in q.options) + "</div>"
    if q.kind in ("select", "boolean"):
        opts = q.options or ["Yes", "No"]
        return (f"<select {name}><option value=\"\">Choose…</option>"
                + "".join(f'<option{" selected" if o == value else ""}>{e(o)}</option>' for o in opts) + "</select>")
    if q.kind == "textarea":
        return f"<textarea {name} rows=\"5\">{e(value or '')}</textarea>"
    kind = {"email": "email", "phone": "tel", "url": "url", "number": "text"}.get(q.kind, "text")
    return f'<input {name} type="{kind}" value="{e(value or "")}">'


def question_html(q: Question, a: Answer | None, resume_name: str | None) -> str:
    need = bool(a and a.needs_you)
    src = SOURCES.get(a.source, "") if a else ""
    why = []
    if src:
        why.append(f'<span class="origin {e(a.source)}">{e(src)}</span>')
    if need:
        why.append('<span class="needs">Needs you</span>')
    if a and a.note:
        why.append(f"<span>{e(a.note)}</span>")
    desc = f'<div class="desc">{e(q.description[:300])}</div>' if q.description else ""
    req = '<span class="req" title="Required">*</span>' if q.required else ""
    label_for = "" if q.kind in ("multiselect", "file") else f' for="q-{e(q.id)}"'
    return (f'<div class="qa{" need" if need else ""}"><div><label class="q"{label_for}>{e(q.label)}{req}</label>{desc}</div>'
            f'<div class="control">{control(q, a, resume_name)}'
            + (f'<div class="why">{"".join(why)}</div>' if why else "") + "</div></div>")


def tally(questions: list[Question], answers: dict[str, Answer]) -> str:
    need = sum(1 for q in questions if (a := answers.get(q.id)) and a.needs_you)
    ready = sum(1 for q in questions if (a := answers.get(q.id)) and not a.empty and not a.needs_you)
    empty = len(questions) - need - ready
    ai = sum(1 for a in answers.values() if a.source == "ai" and not a.empty)
    parts = [f'<span><b>{len(questions)}</b> questions</span>', f'<span><b>{ready}</b> ready</span>']
    if need:
        parts.append(f'<span class="need"><b>{need}</b> need you</span>')
    if empty:
        parts.append(f'<span><b>{empty}</b> left empty</span>')
    if ai:
        parts.append(f'<span><b>{ai}</b> drafted by AI</span>')
    return f'<div class="tally">{"".join(parts)}</div>'


def render(job: dict, app: dict | None, questions: list[Question], answers: dict[str, Answer], warnings: list[str],
           resume_name: str | None, live: dict | None = None) -> str:
    company = job["company"] or "the company"
    ats = ATS_NAMES.get((app or {}).get("ats", ""), "")
    head = (f'<a class="back" href="/">← Back to jobs</a>'
            f'<div class="job-head"><div><h1>{e(job["title"])}</h1><div class="meta"><span class="company">{e(company)}</span>'
            + (f"<span>{e(job['location'][:90])}</span>" if job.get("location") else "")
            + (f'<span class="src">{e(ats)} form</span>' if ats else "")
            + f'</div></div><div class="links"><a class="btn" href="{e(job["url"])}" target="_blank" rel="noopener">'
              f"Open posting</a></div></div>")
    if app is None:
        body = ('<div class="card drafting" id="drafting"><div class="spinner" aria-hidden="true"></div>'
                "<div><b>Reading the form and drafting your answers…</b><p>This takes a few seconds; open questions "
                "go to the AI.</p></div></div>"
                '<div class="card" id="draft-error" hidden><b>Couldn\'t draft this one.</b><p class="msg"></p>'
                f'<a class="btn apply" href="{e(job["url"])}" target="_blank" rel="noopener">Apply yourself</a></div>')
        state = "none"
    else:
        state = (live or {}).get("state") or app["state"]
        notes = "".join(f'<p class="notice warn" role="alert">{e(w)}</p>' for w in warnings)
        if app.get("submitted_at"):
            shot = (' <a href="/apply/shot?job=' + e(job["id"]) + '" target="_blank" rel="noopener">See the confirmation page</a>'
                    if app.get("screenshot") else "")
            notes = (f'<p class="notice" role="status">Submitted {e(_when(app["submitted_at"]))}. Marked as applied.'
                     f'{e(outcome_line((app.get("report") or {}).get("outcome")))}{shot}</p>' + notes)
        groups = []
        for key, title in GROUPS:
            qs = [q for q in questions if q.group == key]
            if qs:
                groups.append(f'<section class="card"><h2>{e(title.format(company=company))}</h2>'
                              + "".join(question_html(q, answers.get(q.id), resume_name) for q in qs) + "</section>")
        live_box = ('<div class="live card" id="live" role="status" data-state="{s}"{hidden}><p class="msg">{m}</p><ul></ul></div>'
                    .format(s=e(state), hidden="" if state in ("filling", "waiting") else " hidden",
                            m=e((live or {}).get("message", ""))))
        body = (f"{notes}{tally(questions, answers)}{live_box}<form id=\"answers\" onsubmit=\"return false\">{''.join(groups)}</form>"
                '<div class="savebar"><button class="btn apply" type="button" id="fill">Fill in browser</button>'
                '<button class="btn" type="button" id="redraft">Draft again</button>'
                '<span id="apply-status" class="status-line">The browser fills the form and stops. You check it and '
                "press Submit yourself.</span></div>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Apply · {e(job["title"])} · Just Job</title>'
        f'<style>{CSS}</style></head><body data-job="{e(job["id"])}" data-state="{e(state)}" data-draft="{1 if app is None else 0}">'
        f'{top_bar("jobs")}<main class="apply-wrap">{head}{body}</main><script>{JS}</script></body></html>'
    )
