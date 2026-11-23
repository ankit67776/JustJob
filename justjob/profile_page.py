"""The dashboard's Profile page: your details, your resume and what the AI read from it."""
from __future__ import annotations

import json
from datetime import datetime

from . import profile as P
from .ui import CSS, e, top_bar

JS = """
const form = document.getElementById('profile-form');
const state = document.getElementById('save-state');
const status = document.getElementById('resume-status');
let dirty = false;
form.addEventListener('input', () => {
  if (dirty) return;
  dirty = true;
  state.textContent = 'Unsaved changes';
  state.classList.add('dirty');
});
form.addEventListener('submit', () => { dirty = false; });
addEventListener('beforeunload', (ev) => { if (dirty) { ev.preventDefault(); ev.returnValue = ''; } });

function say(text, err) {
  status.textContent = text;
  status.classList.toggle('err', !!err);
}
function busy(on) { document.querySelectorAll('[data-parse], [data-pick]').forEach(b => b.toggleAttribute('disabled', on)); }

async function readWithAI() {
  if (dirty) return say('Save your changes first, then read the resume.', true);
  busy(true);
  say('Reading your resume with AI. This can take up to a minute.');
  const r = await fetch('/profile/parse', {method: 'POST'});
  const text = await r.text();
  if (!r.ok) { busy(false); return say(text, true); }
  location.href = '/profile?done=parsed&n=' + encodeURIComponent(text) + '#resume';
}
document.querySelectorAll('[data-parse]').forEach(b => b.addEventListener('click', readWithAI));
document.querySelectorAll('[data-pick]').forEach(b => b.addEventListener('click', () => b.nextElementSibling.click()));

document.querySelectorAll('input[data-resume]').forEach(input => input.addEventListener('change', async () => {
  const file = input.files[0];
  input.value = '';
  if (!file) return;
  if (dirty) return say('Save your changes first, then upload.', true);
  if (file.size > 8 * 1024 * 1024) return say('That file is over 8 MB.', true);
  busy(true);
  say('Uploading ' + file.name + '…');
  const r = await fetch('/profile/resume', {method: 'POST', body: file,
                                            headers: {'X-Filename': encodeURIComponent(file.name)}});
  if (!r.ok) { busy(false); return say(await r.text(), true); }
  if (document.body.dataset.ai === '1') return readWithAI();
  location.href = '/profile?done=uploaded#resume';
}));

document.querySelector('[data-add-skills]')?.addEventListener('click', (ev) => {
  const box = document.getElementById('f-skills');
  const add = JSON.parse(ev.currentTarget.dataset.addSkills);
  box.value = [box.value.trim(), add.join(', ')].filter(Boolean).join(', ');
  box.dispatchEvent(new Event('input', {bubbles: true}));
  ev.currentTarget.remove();
  box.focus();
  say('Added ' + add.length + ' skills. Save to keep them.');
});
"""


def _when(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y").lstrip("0")
    except (TypeError, ValueError):
        return ""


def _value(f: P.Field, v) -> str:
    if f.kind == "list":
        return ", ".join(v or [])
    if f.kind == "number":
        return "" if v is None else f"{v:g}"
    return v or ""


def field_html(f: P.Field, value, missing: bool) -> str:
    fid = f"f-{f.key}"
    req = '<span class="req" title="Needed to apply">*</span>' if f.required else ""
    common = f'id="{fid}" name="{f.key}"' + (f' placeholder="{e(f.placeholder)}"' if f.placeholder else "")
    common += f' autocomplete="{f.autocomplete}"' if f.autocomplete else ""
    if f.help:
        common += f' aria-describedby="{fid}-help"'
    text = _value(f, value)
    if f.kind in ("textarea", "list"):
        control = f'<textarea {common} rows="{3 if f.kind == "textarea" else 2}">{e(text)}</textarea>'
    elif f.kind == "select":
        blank = "" if P.DECLINE in f.options else '<option value="">Choose…</option>'
        opts = "".join(f'<option{" selected" if o == text else ""}>{e(o)}</option>' for o in f.options)
        control = f"<select {common}>{blank}{opts}</select>"
    elif f.kind == "number":
        control = f'<input {common} type="number" min="0" max="1000" step="any" inputmode="decimal" value="{e(text)}">'
    else:
        control = f'<input {common} type="{f.kind}" value="{e(text)}">'
    help_html = f'<span class="help" id="{fid}-help">{e(f.help)}</span>' if f.help else ""
    cls = "field" + (" wide" if f.wide else "") + (" missing" if missing else "")
    return f'<div class="{cls}"><label for="{fid}">{e(f.label)}{req}</label>{control}{help_html}</div>'


def _span(job: dict) -> str:
    def fmt(ym):
        if not ym or ym == "Present":
            return ym or ""
        try:
            return datetime.strptime(ym, "%Y-%m").strftime("%b %Y")
        except ValueError:
            return ym
    start, end = fmt(job.get("start")), fmt(job.get("end"))
    return f"{start} – {end}" if start and end else start or end


def facts_html(prof: dict) -> str:
    facts = prof.get("facts")
    if not facts:
        return ""
    parts = []
    jobs = "".join(
        f'<div class="fact"><div class="what">{e(j["title"])}{" (internship)" if j["internship"] else ""}</div>'
        f'<div class="where">{e(j["company"])}{" · " + e(j["location"]) if j["location"] else ""}</div>'
        f'<div class="when">{e(_span(j))}</div>'
        + (f'<ul>{"".join(f"<li>{e(h)}</li>" for h in j["highlights"][:4])}</ul>' if j["highlights"] else "")
        + "</div>" for j in facts["experience"])
    if jobs:
        years = P.years_from(facts["experience"])
        note = f' <span class="when">{years:g} years full-time</span>' if years else ""
        parts.append(f'<section class="wide"><h3>Experience{note}</h3>{jobs}</section>')
    school = "".join(
        f'<div class="fact"><div class="what">{e(s["school"])}</div>'
        f'<div class="where">{e(", ".join(x for x in (s["degree"], s["field"]) if x))}</div>'
        f'<div class="when">{e(" – ".join(x for x in (s["start"], s["end"]) if x))}'
        f'{" · " + e(s["grade"]) if s["grade"] else ""}</div></div>' for s in facts["education"])
    if school:
        parts.append(f"<section><h3>Education</h3>{school}</section>")
    projects = "".join(
        f'<div class="fact"><div class="what">{e(p["name"])}</div>'
        + (f'<div class="where">{e(p["summary"])}</div>' if p["summary"] else "")
        + (f'<div class="when">{e(", ".join(p["tech"]))}</div>' if p["tech"] else "") + "</div>"
        for p in facts["projects"])
    if projects:
        parts.append(f"<section><h3>Projects</h3>{projects}</section>")
    if facts["skills"]:
        chips = "".join(f'<span class="skill">{e(s)}</span>' for s in facts["skills"])
        add = P.new_skills(prof)
        button = (f'<button type="button" class="btn" data-add-skills="{e(json.dumps(add, ensure_ascii=False))}">'
                  f'Add {len(add)} missing to my skills</button>' if add else "")
        parts.append(f'<section class="wide"><h3>Skills on your resume</h3><div class="skills">{chips}</div>'
                     f'<p class="status-line">{button}</p></section>')
    if facts["certifications"]:
        parts.append('<section class="wide"><h3>Certifications</h3><div class="skills">'
                     + "".join(f'<span class="skill">{e(c)}</span>' for c in facts["certifications"]) + "</div></section>")
    return f'<div class="facts">{"".join(parts)}</div>'


def resume_card(prof: dict, has_ai: bool) -> str:
    r = prof.get("resume")
    upload = ('<button type="button" class="btn{cls}" data-pick>{label}</button>'
              '<input type="file" data-resume hidden accept=".pdf,.docx,.txt,.md">')
    if not r:
        body = (f'<div class="file"><div class="doc">CV</div><div><div class="name">No resume yet</div>'
                f'<div class="sub">PDF, DOCX or TXT, up to 8 MB.</div></div>'
                f'<div class="actions">{upload.format(cls=" apply", label="Upload resume")}</div></div>')
    else:
        kind = P.RESUME_KINDS.get("." + r["file"].rsplit(".", 1)[-1], "FILE")
        words = len((prof.get("resume_text") or "").split())
        read = (f'<button type="button" class="btn" data-parse>{"Read again" if prof.get("facts") else "Read with AI"}</button>'
                if has_ai else "")
        body = (f'<div class="file"><div class="doc">{kind}</div><div><div class="name">{e(r["name"])}</div>'
                f'<div class="sub">Uploaded {_when(r["uploaded_at"])} · {max(1, r["size"] // 1024):,} KB · '
                f'{words:,} words</div></div><div class="actions">{read}'
                f'<a class="btn" href="/profile/resume" target="_blank" rel="noopener">Open</a>'
                f'{upload.format(cls="", label="Replace")}</div></div>')
    if not has_ai:
        note = "Add an AI key (GEMINI_API_KEY in .env) and the AI will fill this page from your resume."
    elif prof.get("facts"):
        note = f'Read by AI on {_when(prof["facts_at"])}. It only fills fields that are empty, so your edits stay.'
    else:
        note = "After you upload, the AI reads it and fills the empty fields below. Your edits always win."
    return (f'<section class="card" id="resume"><h2>Resume</h2><p class="blurb">The AI scores jobs against it, '
            f'and the apply agent attaches it to applications.</p>{body}'
            f'<p class="status-line" id="resume-status" role="status">{e(note)}</p>{facts_html(prof)}</section>')


def readiness(prof: dict) -> str:
    todo = P.missing(prof)
    total = P.required_count()
    done = total - len(todo)
    items = "".join(f'<li><a href="#{a}">{e(label)}</a></li>' for a, label in todo)
    detail = f"<ul>{items}</ul>" if todo else "<p>Everything the apply agent needs is filled in.</p>"
    return (f'<div class="ready"><div class="label">Ready to apply</div>'
            f'<div class="of num">{done} <small>of {total}</small></div>'
            f'<div class="meter"><i style="width:{round(100 * done / total)}%"></i></div>{detail}</div>')


def ready_line(prof: dict) -> str:
    """The readiness meter in one line, for phones, where the side rail is hidden."""
    todo = P.missing(prof)
    total = P.required_count()
    done = total - len(todo)
    rest = f' Missing: {e("; ".join(label for _, label in todo))}' if todo else ""
    return (f'<div class="ready-line"><div class="meter"><i style="width:{round(100 * done / total)}%"></i></div>'
            f'<p><b class="num">{done} of {total}</b> needed to apply.{rest}</p></div>')


def render(prof: dict, has_ai: bool, notice: str = "", warn: str = "") -> str:
    todo = {a for a, _ in P.missing(prof)}
    sections = "".join(
        f'<section class="card" id="s-{s.key}"><h2>{e(s.title)}</h2>'
        + (f'<p class="blurb">{e(s.blurb)}</p>' if s.blurb else '<p class="blurb"></p>')
        + '<div class="fields">'
        + "".join(field_html(f, prof.get(f.key), f"f-{f.key}" in todo) for f in s.fields)
        + "</div></section>" for s in P.SECTIONS)
    toc = '<a href="#resume">Resume</a>' + "".join(f'<a href="#s-{s.key}">{e(s.title)}</a>' for s in P.SECTIONS)
    rail = f'<aside class="rail">{readiness(prof)}<nav class="toc" aria-label="Sections">{toc}</nav></aside>'
    banners = (f'<p class="notice" role="status">{e(notice)}</p>' if notice else "") + (
        f'<p class="notice warn" role="alert">{e(warn)}</p>' if warn else "")
    main = (
        '<main><div class="page-head"><h1>Profile</h1>'
        '<p>What the filters, the AI and the apply agent know about you.</p></div>'
        f'{ready_line(prof)}{banners}{resume_card(prof, has_ai)}'
        f'<form id="profile-form" method="post" action="/profile">{sections}'
        '<div class="savebar"><button class="btn apply" type="submit">Save profile</button>'
        '<span id="save-state">Fields marked <b class="req">*</b> are needed before the agent can apply for you.</span>'
        '</div></form></main>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Profile · Just Job</title>'
        f'<style>{CSS}</style></head><body data-ai="{1 if has_ai else 0}">{top_bar("profile")}'
        f'<div class="layout profile">{rail}{main}</div><script>{JS}</script></body></html>'
    )


def notice_for(done: str, n: str) -> str:
    """The banner after a redirect. Only fixed texts, so nothing from the URL is shown as is."""
    if done == "saved":
        return "Profile saved."
    if done == "saved-refilter":
        return "Profile saved. Stored jobs are being checked again against your skills and experience limit."
    if done == "uploaded":
        return "Resume uploaded."
    if done == "parsed":
        count = int(n) if n.isdigit() else 0
        return (f"Read your resume and filled {count} empty field{'s' if count != 1 else ''}. Check them below."
                if count else "Read your resume. Your fields were already filled, so nothing changed.")
    return ""


def warning_for(bad: list[str]) -> str:
    labels = [P.FIELDS[k].label for k in bad if k in P.FIELDS]
    return f"These weren't valid, so they kept their old value: {', '.join(labels)}." if labels else ""
