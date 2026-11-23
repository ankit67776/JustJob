"""The dashboard's Answers page: what the apply agent learned from your submitted forms, and how
often its answers needed no edit from you. Saved answers can be edited or deleted here."""
from __future__ import annotations

from datetime import datetime

from .apply.forms import Question
from .ui import CSS, e, top_bar


def _day(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y").lstrip("0")
    except (TypeError, ValueError):
        return ""


def as_question(row: dict) -> Question:
    """A saved answer's question, rebuilt enough to check a value against its options."""
    return Question("saved", row["label"], row["kind"], False, row["options"] or [])


def control(row: dict) -> str:
    q, value = as_question(row), row["value"]
    if q.kind == "multiselect":
        chosen = set(value if isinstance(value, list) else [value])
        return '<div class="checks">' + "".join(
            f'<label><input type="checkbox" name="value" value="{e(o)}"{" checked" if o in chosen else ""}>{e(o)}</label>'
            for o in q.options) + "</div>"
    if q.kind in ("select", "boolean"):
        return ('<select name="value">' + "".join(f'<option{" selected" if o == value else ""}>{e(o)}</option>'
                                                  for o in q.options or ["Yes", "No"]) + "</select>")
    if q.kind == "textarea":
        return f'<textarea name="value" rows="5">{e(str(value))}</textarea>'
    return f'<input name="value" value="{e(str(value))}">'


def status(row: dict, trust: int) -> str:
    left = trust - row["confirmed"]
    if row["kind"] == "textarea":
        return '<span class="origin">Always a draft for you to read</span>'
    if left <= 0:
        return '<span class="origin memory">Fills without asking</span>'
    return f'<span class="needs">Asks you to check it {"once more" if left == 1 else f"{left} more times"}</span>'


def saved_html(row: dict, trust: int) -> str:
    where = f"From the {e(row['company'])} form" if row["company"] else "From an earlier form"
    meta = (f"<span>{where}, {e(_day(row['updated_at']))}</span>"
            + ("<span>Wins over your profile</span>" if row["overrides"] else ""))
    return (f'<form class="qa saved" method="post" action="/answers/save"><input type="hidden" name="key" value="{e(row["key"])}">'
            f'<div><div class="q">{e(row["label"])}</div></div>'
            f'<div class="control">{control(row)}<div class="why">{status(row, trust)}{meta}</div>'
            '<div class="actions"><button class="btn" type="submit">Save</button>'
            '<button class="btn danger" type="submit" formaction="/answers/delete">Delete</button></div></div></form>')


def history_html(apps: list[dict]) -> str:
    rows = []
    for a in apps:
        out = (a["report"] or {}).get("outcome")
        if not out or not out.get("fields"):
            continue
        share = round(100 * out["kept"] / out["fields"])
        rows.append(f"<tr><td>{e(a['company'] or '')} · {e(a['title'] or a['job_id'])}</td><td>{e(_day(a['submitted_at']))}</td>"
                    f'<td class="num">{out["kept"]} of {out["fields"]} ({share}%)</td>'
                    f'<td class="num">{len(out.get("saved") or [])}</td></tr>')
    if not rows:
        return ('<p class="blurb">No forms sent through the agent yet. After each one, this shows how many of its '
                "answers went in without an edit from you.</p>")
    return ('<table class="history"><thead><tr><th>Application</th><th>Sent</th><th class="num">Needed no edit</th>'
            f'<th class="num">Answers saved</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


def render(saved: list[dict], apps: list[dict], trust: int, notice: str = "") -> str:
    trusted = sum(1 for r in saved if r["confirmed"] >= trust and r["kind"] != "textarea")
    tally = (f'<div class="tally"><span><b>{len(saved)}</b> saved answers</span><span><b>{trusted}</b> fill without asking'
             f'</span><span><b>{len(saved) - trusted}</b> still checked by you</span></div>')
    items = "".join(saved_html(r, trust) for r in saved) or (
        '<p class="blurb">Nothing saved yet. Answer a question the agent couldn\'t, or change one of its answers, '
        "then press Submit: the answer is kept for the next form that asks.</p>")
    body = (f'<div class="page-head"><h1>Saved answers</h1><p>What the apply agent learned from the forms you submitted. '
            f"A saved answer is outlined for you to check until you've kept it on {trust} forms; after that it fills "
            "without asking. Written answers always stay drafts.</p></div>"
            + (f'<p class="notice" role="status">{e(notice)}</p>' if notice else "")
            + f'<section class="card"><h2>How often the agent got it right</h2>{history_html(apps)}</section>'
            f'<section class="card"><h2>Answers</h2>{tally}{items}</section>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Saved answers · Just Job</title>'
            f'<style>{CSS}</style></head><body>{top_bar("answers")}<main class="apply-wrap">{body}</main></body></html>')


def notice_for(done: str) -> str:
    """The banner after a redirect. Only fixed texts, so nothing from the URL is shown as is."""
    return {"saved": "Answer saved. The agent uses it from the next form on.", "deleted": "Answer deleted.",
            "invalid": "That answer isn't one the form accepts, so nothing changed."}.get(done, "")
