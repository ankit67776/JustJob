"""AI step: send jobs in batches to any OpenAI-compatible chat API and store structured verdicts.

Only jobs that passed the rule filters, aren't duplicates, and haven't been analyzed yet are
sent, so after the first run this is a handful of calls a day.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from .config import resume_text
from .db import DB
from .net import HttpError, get_json, post_json
from .text import trim_for_ai

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

SYSTEM = """You screen job postings for one candidate and return strict JSON.

For every job in the input, output one object:
{{"job_id": "<id exactly as given>",
  "min_years_required": <number or null>,
  "is_within_limit": <true or false>,
  "match_score": <integer 0-100>,
  "reason": "<one sentence, at most 20 words>",
  "flags": [<zero or more short strings>]}}

Return {{"results": [ ... ]}} with exactly one object per job and nothing else.

How to judge:
- min_years_required: the minimum years of professional experience the posting REQUIRES.
  If it says "5+ years preferred, 2+ required", use 2. Years with one specific tool are not
  total experience unless the posting makes them a hard requirement. If no number is stated,
  infer from level cues (new grad / junior / level I -> 0, level II / mid-level -> 2,
  senior -> 5, staff / principal -> 8). Use null only when there are no cues at all.
- is_within_limit: true if someone with up to {max_years} years of experience could realistically
  be hired (min_years_required <= {max_years}, or unstated and the role isn't senior).
- match_score: fit with the candidate's skills, target roles and preferences.
  80-100 strong fit, 50-79 partial, under 50 weak. Score low for roles the candidate avoids.
  Missing a few listed skills is normal; judge the overall fit. Ignore location and remote
  status here, they are scored separately.
- flags: any that apply from "senior-level", "frontend-only", "not-engineering", "requires-phd",
  "security-clearance", "internship", "contract", "location-mismatch", "visa-restricted"."""


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    key: str
    extra: dict = field(default_factory=dict)
    json_mode: bool = True


def providers(cfg: dict) -> list[Provider]:
    ai = cfg["ai"]
    out = []
    for name in [ai.get("provider", "gemini"), *ai.get("fallback", [])]:
        p = ai.get("providers", {}).get(name)
        if not p:
            log.warning("ai: provider '%s' is not configured", name)
            continue
        key = os.environ.get(p.get("api_key_env", ""), "")
        if key:
            out.append(Provider(name, p["base_url"].rstrip("/"), p["model"], key,
                                p.get("extra", {}), p.get("json_mode", True)))
    return out


def profile_block(cfg: dict) -> str:
    p = cfg["profile"]
    loc = cfg["filters"]["location"]
    lines = [
        f"Experience: {p.get('years_experience', '?')} years",
        f"Summary: {p.get('summary', '')}",
        f"Skills: {', '.join(p.get('skills', []))}",
        f"Target roles: {', '.join(p.get('target_roles', []))}",
        f"Preferences: {p.get('preferences', '')}",
        f"Avoid: {', '.join(p.get('avoid', []))}",
    ]
    onsite = loc.get("onsite", ["*"])
    if "*" not in onsite:
        # Only the first few names; the filter already handled location, this is for flags.
        lines.append(f"Can work on-site in: {', '.join(onsite[:3]) or 'nowhere'}; "
                     f"remote: {'yes' if loc.get('remote', True) else 'no'}")
    resume = resume_text(cfg)
    if resume:
        lines.append(f"Resume:\n{resume}")
    return "\n".join(lines)


def build_messages(cfg: dict, batch: list) -> list[dict]:
    chars = int(cfg["ai"].get("description_chars", 2500))
    blocks = []
    for i, row in enumerate(batch, 1):
        blocks.append(
            f"### job_id: {i}\nTitle: {row['title']}\nCompany: {row['company']}\n"
            f"Location: {row['location'] or 'n/a'}\n{trim_for_ai(row['description'], chars)}"
        )
    max_years = cfg["profile"].get("max_years_required", 3)
    return [
        {"role": "system", "content": SYSTEM.format(max_years=max_years)},
        {"role": "user", "content": f"CANDIDATE\n{profile_block(cfg)}\n\nJOBS\n\n" + "\n\n".join(blocks)},
    ]


# --- response parsing --------------------------------------------------------------------

def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start < 0 or end <= start:
        raise ValueError("no JSON found in response")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from None


def _as_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "yes", "no"):
        return v.strip().lower() in ("true", "yes")
    return None


def _as_float(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        m = re.search(r"\d+(?:\.\d+)?", str(v))
        return float(m.group(0)) if m else None


def parse_results(text: str, expected: set[str], max_years: float) -> dict[str, dict]:
    """Validated results keyed by job_id. Raises ValueError if the response isn't usable JSON."""
    data = _extract_json(text)
    items = data.get("results", data.get("jobs")) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("response has no results list")
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        jid = str(it.get("job_id", "")).strip()
        if jid not in expected:
            continue
        score = _as_float(it.get("match_score"))
        if score is None:
            continue
        min_years = _as_float(it.get("min_years_required"))
        within = _as_bool(it.get("is_within_limit", it.get("is_within_3_years")))
        if within is None:
            within = min_years is None or min_years <= max_years
        flags = it.get("flags") or []
        if isinstance(flags, str):
            flags = [flags]
        out[jid] = {
            "min_years_required": min_years,
            "is_within_limit": within,
            "match_score": int(max(0, min(100, round(score)))),
            "reason": str(it.get("reason") or "").strip()[:300],
            "flags": [str(f).strip()[:40] for f in flags if str(f).strip()][:8],
        }
    return out


# --- API calls ---------------------------------------------------------------------------

def chat(p: Provider, messages: list[dict]) -> str:
    body = {"model": p.model, "messages": messages, "temperature": 0.1, **p.extra}
    if p.json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {p.key}"}
    try:
        resp = post_json(f"{p.base_url}/chat/completions", body, headers=headers, timeout=180)
    except HttpError as e:
        if e.status == 400 and p.json_mode and "response_format" in e.body:
            log.info("ai: %s doesn't support JSON mode, retrying without it", p.name)
            p.json_mode = False
            return chat(p, messages)
        raise
    return resp["choices"][0]["message"]["content"] or ""


def list_models(p: Provider) -> list[str]:
    data = get_json(f"{p.base_url}/models", headers={"Authorization": f"Bearer {p.key}"})
    return sorted(m.get("id", "") for m in data.get("data", []))


def analyze_batch(provs: list[Provider], messages: list[dict], expected: set[str],
                  max_years: float) -> tuple[dict[str, dict], str | None, bool]:
    """Returns (results, model used, any provider reachable)."""
    reachable = False
    for p in provs:
        for attempt in (1, 2):   # one retry on unusable JSON
            try:
                text = chat(p, messages)
            except Exception as e:
                log.warning("ai: %s failed: %s", p.name, e)
                break   # try the next provider
            reachable = True
            try:
                return parse_results(text, expected, max_years), f"{p.name}/{p.model}", True
            except ValueError as e:
                log.warning("ai: %s returned unusable output (attempt %d): %s", p.name, attempt, e)
    return {}, None, reachable


def pending(db: DB, limit: int):
    return db.q(
        "SELECT * FROM jobs WHERE rule_status IN ('pass', 'unclear') AND duplicate_of IS NULL"
        " AND closed = 0 AND status = 'new' AND ai_status IS NULL AND ai_attempts < ?"
        " ORDER BY rule_score DESC, first_seen DESC LIMIT ?",
        (MAX_ATTEMPTS, limit),
    )


def run(cfg: dict, db: DB) -> dict:
    ai = cfg["ai"]
    stats = {"calls": 0, "analyzed": 0, "failed": 0, "pending": 0}
    if not ai.get("enabled", True):
        stats["skipped"] = "disabled in config"
        return stats
    provs = providers(cfg)
    if not provs:
        stats["skipped"] = "no API key set"
        log.info("ai: skipped, no API key set for any configured provider")
        return stats

    batch_size = max(1, int(ai.get("batch_size", 10)))
    max_calls = int(ai.get("max_calls_per_run", 30))
    interval = float(ai.get("min_interval_seconds", 8))
    max_years = float(cfg["profile"].get("max_years_required", 3))
    rows = pending(db, batch_size * max_calls)

    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        if stats["calls"]:
            time.sleep(interval)
        ids = {str(i): row["id"] for i, row in enumerate(batch, 1)}
        results, model, reachable = analyze_batch(provs, build_messages(cfg, batch), set(ids), max_years)
        stats["calls"] += 1
        if not reachable:
            log.warning("ai: no provider reachable, stopping; remaining jobs wait for the next run")
            break
        for local_id, job_id in ids.items():
            if local_id in results:
                db.save_ai_result(job_id, results[local_id], model)
                stats["analyzed"] += 1
        missing = [job_id for local_id, job_id in ids.items() if local_id not in results]
        db.bump_ai_attempts(missing)
        stats["failed"] += len(missing)

    stats["pending"] = len(pending(db, 100000))
    return stats
