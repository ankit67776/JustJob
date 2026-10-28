"""Daily digest: best open matches first, as Markdown and HTML files under data/digests/."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import DATA_DIR
from .db import DB, score_sql, SHOWABLE
from .sources import source_label
from .text import now_iso
from .web import experience_label, flags, page, render_job, why_label


@dataclass
class Digest:
    generated_at: str
    since: str | None          # previous digest time; jobs first seen after it are "new"
    new: list                  # new matches, best first
    top: list                  # all open matches you haven't acted on, best first
    total_open: int


def build(cfg: dict, db: DB) -> Digest:
    score_expr = score_sql(cfg)
    min_score = int(cfg["digest"].get("min_score", 30))
    limit = int(cfg["digest"].get("max_items", 60))
    since = db.get_meta("last_digest_at")
    base = f"SELECT *, {score_expr} AS score FROM jobs WHERE {SHOWABLE} AND status = 'new' AND {score_expr} >= ?"
    order = " ORDER BY score DESC, COALESCE(posted_at, first_seen) DESC LIMIT ?"
    top = db.q(base + order, (min_score, limit))
    new = db.q(base + " AND first_seen > ?" + order, (min_score, since or "", limit))
    total = db.q(f"SELECT COUNT(*) FROM jobs WHERE {SHOWABLE} AND status = 'new' AND {score_expr} >= ?",
                 (min_score,))[0][0]
    return Digest(now_iso(), since, new, top, total)


def _md_cell(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").strip()


def to_markdown(d: Digest) -> str:
    day = d.generated_at[:10]
    lines = [f"# Job digest {day}", "", f"{len(d.new)} new matches · {d.total_open} open matches you haven't acted on", ""]
    for heading, rows in (("New since last digest", d.new), ("Best open matches", d.top)):
        lines += [f"## {heading}", ""]
        if not rows:
            lines += ["_None._", ""]
            continue
        lines += ["| Score | Role | Company | Location | Exp | Why | Source |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            why = why_label(r) + (f" [{', '.join(flags(r))}]" if flags(r) else "")
            lines.append(
                f"| {r['score']} | [{_md_cell(r['title'])}]({r['url']}) | {_md_cell(r['company'])} | "
                f"{_md_cell(r['location'])[:50]} | {experience_label(r)} | {_md_cell(why)} | "
                f"{source_label(r['source'], r['board'])} |"
            )
        lines.append("")
    return "\n".join(lines)


def to_html(d: Digest) -> str:
    parts = [f'<p class="meta-line">{len(d.new)} new, {d.total_open} open matches you haven\'t acted on</p>']
    for heading, rows in (("New since last digest", d.new), ("Best open matches", d.top)):
        parts.append(f"<h2>{heading}</h2>")
        cards = "".join(render_job(r, False) for r in rows)
        parts.append(f'<div class="list">{cards}</div>' if cards else '<div class="list empty">None.</div>')
    return page(f"Job digest {d.generated_at[:10]}", "".join(parts))


def write(cfg: dict, d: Digest) -> dict:
    out_dir = DATA_DIR / "digests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(d.generated_at).astimezone().strftime("%Y-%m-%d")
    md, htm = out_dir / f"{stamp}.md", out_dir / f"{stamp}.html"
    md.write_text(to_markdown(d), encoding="utf-8")
    htm.write_text(to_html(d), encoding="utf-8")
    return {"new": len(d.new), "open": d.total_open, "markdown": str(md), "html": str(htm)}
