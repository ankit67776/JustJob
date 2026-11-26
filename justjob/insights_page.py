"""The dashboard's Insights page: how far applications get, what gets replies, and how many go out each week.

Charts are plain HTML in the app's one accent color: one series each, so no legend; every value is
printed or in the table under each chart, and hovering or focusing a mark shows it too."""
from __future__ import annotations

from .ui import CSS, e, top_bar

RANGES = {"30": ("Last 30 days", 30), "90": ("Last 90 days", 90), "all": ("All time", None)}

JS = """
const tip = document.getElementById('tip');
function show(el) {
  tip.textContent = el.dataset.tip;
  tip.hidden = false;
  const r = el.getBoundingClientRect();
  tip.style.left = Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, r.left + r.width / 2 - tip.offsetWidth / 2)) + 'px';
  tip.style.top = (window.scrollY + r.top - tip.offsetHeight - 8) + 'px';
}
document.querySelectorAll('[data-tip]').forEach((el) => {
  el.addEventListener('pointerenter', () => show(el));
  el.addEventListener('focus', () => show(el));
  el.addEventListener('pointerleave', () => { tip.hidden = true; });
  el.addEventListener('blur', () => { tip.hidden = true; });
});
"""


def pct(x: float | None) -> str:
    return "" if x is None else f"{round(100 * x)}%"


def tile(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="tile"><div class="tl">{e(label)}</div><div class="tv">{e(value)}</div>'
            + (f'<div class="ts">{e(sub)}</div>' if sub else "") + "</div>")


def tiles(ins: dict) -> str:
    n = ins["total"]
    days = ins["median_days"]
    wait = "None yet" if days is None else ("Under a day" if days < 1 else f"{days:.0f} day{'s' if round(days) != 1 else ''}")
    return ('<div class="tiles">'
            + tile("Applications", f"{n:,}", f"{ins['open']} still open")
            + tile("Got a reply", pct(ins["replied"] / n) if n else "–", f"{ins['replied']} of {n}")
            + tile("Reached an interview", f"{ins['interviews']}", pct(ins["interviews"] / n) + " of applications" if n else "")
            + tile("Offers", f"{ins['offers']}")
            + tile("Typical wait for a reply", wait, "median, applied to first answer")
            + "</div>")


def table(head: list[str], rows: list[list[str]], caption: str) -> str:
    th = "".join(f'<th{" class=num" if i else ""}>{e(h)}</th>' for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td{" class=num" if i else ""}>{e(c)}</td>' for i, c in enumerate(r)) + "</tr>"
                   for r in rows)
    return (f'<details class="as-table"><summary>Show as a table</summary><table class="history">'
            f'<caption class="sr">{e(caption)}</caption><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></details>')


def funnel(ins: dict) -> str:
    rows, data = [], []
    for i, f in enumerate(ins["funnel"]):
        share = pct(f["of_total"]) if i else "100%"
        # Screening's share of Applied is its share of everything; say it once.
        step = (f" · {pct(f['of_previous'])} of {ins['funnel'][i - 1]['label']}"
                if i > 1 and f["of_previous"] is not None else "")
        tipt = f"{f['label']}: {f['count']} of {ins['total']} applications ({share}){step}"
        rows.append(f'<div class="frow"><div class="fname">{e(f["label"])}</div><div class="ftrack">'
                    f'<div class="fbar" style="--w:{f["of_total"] if ins["total"] else 0:.4f}" tabindex="0" '
                    f'data-tip="{e(tipt)}"></div>'
                    f'<span class="fval">{f["count"]} <span class="muted">· {share}{e(step)}</span></span></div></div>')
        data.append([f["label"], str(f["count"]), share, pct(f["of_previous"]) if i else ""])
    ends = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in ins["outcomes"].items() if n)
    foot = f'<p class="blurb foot">Closed so far: {e(ends)}.</p>' if ends else ""
    return (f'<section class="card"><h2>How far applications got</h2><p class="blurb">Each bar counts the applications '
            'that reached that stage, including ones that went further or were turned down later.</p>'
            f'<div class="funnel">{"".join(rows)}</div>{foot}'
            + table(["Stage", "Reached", "Of all applications", "Of the stage before"], data, "How far applications got")
            + "</section>")


def meters(title: str, blurb: str, groups: list[dict]) -> str:
    if not groups:
        return ""
    rows, data = [], []
    for g in groups:
        tipt = f"{g['name']}: {g['replied']} of {g['applied']} got a reply ({pct(g['rate'])})"
        rows.append(f'<div class="mrow"><div class="mname">{e(g["name"])}</div>'
                    f'<div class="mtrack" tabindex="0" data-tip="{e(tipt)}"><div class="mfill" style="width:{100 * g["rate"]:.2f}%">'
                    f'</div></div><div class="mval">{pct(g["rate"])} <span class="muted">{g["replied"]} of {g["applied"]}</span>'
                    "</div></div>")
        data.append([g["name"], str(g["applied"]), str(g["replied"]), pct(g["rate"])])
    return (f'<section class="card"><h2>{e(title)}</h2><p class="blurb">{e(blurb)}</p><div class="meters">{"".join(rows)}</div>'
            + table(["Group", "Applications", "Replies", "Reply rate"], data, title) + "</section>")


def weekly(ins: dict) -> str:
    weeks = ins["weeks"]
    top = max((w["count"] for w in weeks), default=0)
    scale = max(top, 1)
    peak = next((i for i, w in enumerate(weeks) if w["count"] == top and top), None)
    cols, data = [], []
    for i, w in enumerate(weeks):
        day = w["start"].strftime("%d %b").lstrip("0")
        label = f'<span class="cv">{w["count"]}</span>' if w["count"] and (i == peak or i == len(weeks) - 1) else ""
        tipt = f"Week of {day}: {w['count']} application{'s' if w['count'] != 1 else ''}"
        cols.append(f'<div class="wcol" tabindex="0" data-tip="{e(tipt)}"><div class="wplot">{label}'
                    f'<div class="wbar" style="height:{100 * w["count"] / scale:.2f}%"></div></div>'
                    f'<div class="wx">{e(day) if (len(weeks) - 1 - i) % 2 == 0 else ""}</div></div>')
        data.append([f"Week of {day}", str(w["count"])])
    return (f'<section class="card"><h2>Applications per week</h2><p class="blurb">The last {len(weeks)} weeks, by the '
            'week you applied.</p><div class="weekly"><div class="wy"><span>' + (str(top) if top else "") + '</span><span>0</span></div>'
            f'<div class="wcols">{"".join(cols)}</div></div>'
            + table(["Week", "Applications"], data, "Applications per week") + "</section>")


def render(ins: dict, range_key: str) -> str:
    chips = "".join(f'<a class="{"on" if k == range_key else ""}" href="/insights?range={k}"'
                    f'{" aria-current=page" if k == range_key else ""}>{e(label)}</a>' for k, (label, _) in RANGES.items())
    if ins["total"]:
        content = (tiles(ins) + funnel(ins) + '<div class="two">'
                   + meters("Replies by where you found the job", "Which boards and sites answer.", ins["by_source"])
                   + meters("Replies by role type", "Grouped by job title, the same way as on the Jobs page.", ins["by_role"])
                   + meters("Replies by match score", "Whether the jobs Just Job ranked highest answer more often.",
                            ins["by_score"])
                   + meters("Replies by how you applied", "Forms the agent filled against ones you sent yourself.",
                            ins["by_route"])
                   + "</div>" + weekly(ins))
    else:
        content = ('<section class="card"><p class="blurb">No applications in this range yet. Mark jobs as applied on the '
                   'Jobs page, or add ones you made elsewhere on the Applications page, and move them along as you hear '
                   "back.</p></section>")
    body = (f'<div class="page-head row-head"><div><h1>Insights</h1><p>What your applications say about the search. A reply '
            'is any answer from the employer: a screening call, an interview, an offer or a rejection.</p></div>'
            f'<a class="btn" href="/tracker">Applications</a></div><nav class="range" aria-label="Time range">{chips}</nav>'
            f'{content}<div id="tip" class="tip" role="tooltip" hidden></div>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Insights · Just Job</title>'
            f'<style>{CSS}</style></head><body>{top_bar("insights")}<main class="apply-wrap wide">{body}</main>'
            f'<script>{JS}</script></body></html>')
