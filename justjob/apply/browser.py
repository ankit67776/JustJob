"""Filling a form in a real, visible browser, then watching for you to press Submit.

Playwright (an optional extra) drives your installed Edge or Chrome with a browser profile of its
own in data/browser, so cookies persist between applications. The agent never submits: it fills
what it can, outlines what needs you, and waits. When the page shows a confirmation it saves a
screenshot and marks the job applied. If you close the window instead, nothing is sent.

While it waits it keeps reading what the fields hold, and reads them once more the moment you
press Submit. After a confirmed submission those final answers are what memory.py learns from.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from pathlib import Path

from ..db import DB
from ..text import now_iso
from . import memory
from .answers import DECLINE_RE, Answer, home_country, norm, same
from .forms import Question, Target

log = logging.getLogger(__name__)

CONFIRM_RE = re.compile(
    r"thank(s| you)\b[^.!\n]{0,60}\bappl(y|ying|ication)|application (was |has been |is )?(successfully )?"
    r"(submitted|received|sent|complete)|we('ve| have) received your application|successfully applied", re.I)
CONFIRM_URL = re.compile(r"/confirmation|thank[-_]?you|/thanks\b|application[-_]submitted", re.I)

HIGHLIGHT_JS = """([ids, message]) => {
  const mark = (el) => {
    const box = el.closest('fieldset, [class*="field"], [class*="question"]') || el.parentElement || el;
    box.style.outline = '3px solid #E8A33D'; box.style.outlineOffset = '4px'; box.style.borderRadius = '6px';
    return box;
  };
  let first = null;
  for (const id of ids) {
    const el = document.getElementById(id) || document.querySelector(`[name="${CSS.escape(id)}"]`)
      || document.querySelector(`[data-field-path="${CSS.escape(id)}"]`);
    if (el) { const box = mark(el); first = first || box; }
  }
  document.getElementById('justjob-banner')?.remove();
  const bar = document.createElement('div');
  bar.id = 'justjob-banner';
  bar.textContent = message;
  bar.title = 'Click to hide';
  Object.assign(bar.style, {position: 'fixed', zIndex: 2147483647, left: '50%', top: '12px', transform: 'translateX(-50%)',
    background: '#17201D', color: '#fff', padding: '10px 16px', borderRadius: '10px', font: '600 14px system-ui, sans-serif',
    boxShadow: '0 6px 24px rgba(0,0,0,.25)', maxWidth: '90vw', cursor: 'pointer'});
  bar.onclick = () => bar.remove();
  document.body.appendChild(bar);
  (first || document.querySelector('form') || document.body).scrollIntoView({block: 'center'});
}"""

# Clicks the option labelled `text` inside the field whose input has this id or name: the nearest
# ancestor holding such an option is that field's own box, so another question's "Yes" is never hit.
CHOICE_JS = """([key, text]) => {
  const el = document.getElementById(key) || document.querySelector(`[name="${CSS.escape(key)}"]`)
    || document.querySelector(`[data-field-path="${CSS.escape(key)}"]`);
  if (!el) return 'missing';
  const want = text.trim().toLowerCase().replace(/\\s+/g, ' ');
  for (let box = el, i = 0; box && i < 8; box = box.parentElement, i++) {
    const hit = [...box.querySelectorAll('button, label, [role=radio], [role=option], [role=checkbox]')]
      .find(c => c.innerText.trim().toLowerCase().replace(/\\s+/g, ' ') === want);
    if (hit) { hit.click(); return 'ok'; }
  }
  return 'nomatch';
}"""


# What each field holds right now: {id: [texts]} ([] = nothing chosen, null = field not on the page).
# Typed fields give their value. For the rest, the nearest box around the input that holds no other
# question is searched for what's chosen: pressed Yes/No buttons (Ashby), the chosen value of a
# searchable dropdown (Greenhouse's react-select), ticked radios and checkboxes, a native <select>.
SNAPSHOT_JS = """(specs) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const find = (id) => document.getElementById(id) || document.querySelector(`[name="${CSS.escape(id)}"]`)
    || document.querySelector(`[data-field-path="${CSS.escape(id)}"]`);
  const els = specs.map((s) => find(s.id));
  const labelOf = (i) => clean((i.labels && i.labels[0] && i.labels[0].innerText) || i.closest('label')?.innerText
                                || i.parentElement?.innerText || i.value);
  const chosenIn = (box) => {
    let hits = [...box.querySelectorAll('[aria-pressed="true"], [aria-checked="true"]:not(input)')];
    if (!hits.length) hits = [...box.querySelectorAll('[class*="single-value"], [class*="singleValue"], ' +
                                                      '[class*="multi-value__label"], [class*="multiValue__label"]')];
    if (hits.length) return hits.map((n) => clean(n.innerText));
    const ticked = [...box.querySelectorAll('input[type=radio]:checked, input[type=checkbox]:checked')];
    if (ticked.length) return ticked.map(labelOf);
    const sel = box.tagName === 'SELECT' ? box : box.querySelector('select');
    return sel ? [...sel.selectedOptions].map((o) => clean(o.text)).filter((t) => t) : null;
  };
  const typed = ['text', 'textarea', 'email', 'phone', 'url', 'number', 'date', 'location'];
  const textLike = 'input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]), textarea';
  const out = {};
  specs.forEach((s, n) => {
    const el = els[n];
    if (!el) { out[s.id] = null; return; }
    // Ashby fields without an id are found by their wrapper; the input is inside it.
    const input = el.matches('input, textarea, select') ? el : el.querySelector(textLike);
    const own = input && input.matches(textLike) ? input : null;
    if (typed.includes(s.kind) && own && own.getAttribute('role') !== 'combobox') { out[s.id] = [own.value]; return; }
    let found = null;
    for (let box = el, i = 0; box && i < 6 && !found; box = box.parentElement, i++) {
      if (i && els.some((o, m) => m !== n && o && box.contains(o))) break;
      found = chosenIn(box);
    }
    out[s.id] = found || (own && own.value ? [own.value] : []);
  });
  return out;
}"""

# Reads the form once more when you press Submit, before the page moves on. Runs in the capture
# phase, so the reading comes before the page's own handlers.
HOOK_JS = """(specs) => {
  window.__justjobSpecs = specs;
  window.__justjobRead = """ + SNAPSHOT_JS + """;
  if (window.__justjobHooked) return;
  window.__justjobHooked = true;
  const send = () => { try { window.__justjobSubmit(window.__justjobRead(window.__justjobSpecs)); } catch (e) {} };
  document.addEventListener('submit', send, true);
  document.addEventListener('click', (ev) => {
    const b = ev.target.closest && ev.target.closest('button, input[type=submit]');
    if (b && (b.type === 'submit' || /submit|apply|send/i.test(b.innerText || b.value || ''))) send();
  }, true);
}"""

TYPED_KINDS = ("text", "textarea", "email", "phone", "url", "number")


def _attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def best_index(texts: list[str], want: str) -> int | None:
    w = norm(want)
    normed = [norm(t) for t in texts]
    if w in normed:
        return normed.index(w)
    hits = [i for i, t in enumerate(normed) if t and (f" {w} " in f" {t} " or f" {t} " in f" {w} ")]
    if len(hits) == 1:
        return hits[0]
    return 0 if len(texts) == 1 else None


class Filler:
    """Fills one form. Subclasses know their ATS's page."""
    ready = "form"
    submit = 'button:has-text("Submit")'

    def __init__(self, page, prof: dict, resume: Path | None):
        self.page, self.prof, self.resume = page, prof, resume
        self.final: dict[str, list[str]] = {}   # question id -> what the field last held
        self.specs: list[dict] = []

    def el(self, qid: str):
        return self.page.locator(f'[id="{_attr(qid)}"]').first

    def pause(self) -> None:
        self.page.wait_for_timeout(random.randint(60, 200))   # human pace; bot checks score instant forms low

    def text(self, loc, value: str) -> bool:
        loc.scroll_into_view_if_needed()
        loc.fill(str(value))
        return True

    def file(self, loc) -> bool:
        if not self.resume or not self.resume.exists():
            return False
        loc.set_input_files(str(self.resume))
        return True

    def options_of(self, loc):
        """The options of the dropdown `loc` opened. Pages hold other, hidden option lists (the phone
        number's country picker has 245), so only the list this input controls counts."""
        controls = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns")
        if controls:
            return self.page.locator(f'[id="{_attr(controls)}"] [role="option"]:visible')
        return self.page.locator('[role="option"]:visible')

    def open_and_type(self, loc, text: str, delay: int):
        loc.scroll_into_view_if_needed()
        loc.click()
        loc.fill("")
        loc.press_sequentially(text, delay=delay)
        return self.options_of(loc)

    def combobox(self, loc, value: str) -> bool:
        """Searchable dropdowns: type part of the option, then click the matching one."""
        options = self.open_and_type(loc, str(value)[:40], 12)
        try:
            options.first.wait_for(state="visible", timeout=5000)
        except Exception:
            loc.press("Escape")
            return False
        i = best_index([t.strip() for t in options.all_inner_texts()], str(value))
        if i is None:
            loc.press("Escape")
            return False
        options.nth(i).click()
        return True

    def place(self, loc, value: str) -> bool:
        """City search boxes: type the city, take the first suggestion that names it."""
        city = str(value).split(",")[0].strip()
        options = self.open_and_type(loc, city, 40)
        try:
            options.first.wait_for(state="visible", timeout=8000)
        except Exception:
            loc.press("Escape")
            return False
        texts = options.all_inner_texts()
        i = next((i for i, t in enumerate(texts) if norm(city) in norm(t)), 0)
        options.nth(i).click()
        return True

    def settle(self) -> None:
        """Wait for the page's scripts to take over the form; values typed before that get wiped."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

    def choice(self, qid: str, value: str) -> bool:
        return self.page.evaluate(CHOICE_JS, [qid, str(value)]) == "ok"

    def fill(self, q: Question, value) -> bool:
        raise NotImplementedError

    def fill_all(self, questions: list[Question], answers: dict[str, Answer]) -> dict:
        """Fill every answered question. Returns what was filled and what's left for you."""
        filled, todo, typed = [], [], []
        couldnt = "The agent couldn't fill this field. Please do it."
        for q in questions:
            a = answers.get(q.id)
            if not a or a.empty:
                if (a and a.needs_you) or q.required:
                    todo.append({"id": q.id, "label": q.label, "why": a.note if a else "No answer."})
                continue
            try:
                ok = self.fill(q, a.value)
            except Exception as e:   # one stubborn field shouldn't stop the rest
                log.info("apply: couldn't fill %s: %s", q.id, e)
                ok = False
            if ok:
                filled.append(q.label)
                if q.kind in TYPED_KINDS:
                    typed.append((q, str(a.value)))
                if a.needs_you:
                    todo.append({"id": q.id, "label": q.label, "why": a.note or "Check this one."})
            else:
                todo.append({"id": q.id, "label": q.label, "why": couldnt})
            self.pause()
        # A late re-render can wipe typed values; check each one and type it again if so.
        for q, value in typed:
            try:
                loc = self.el(q.id)
                if not same(q, loc.input_value(), value):
                    loc.fill(value)
                ok = same(q, loc.input_value(), value)
            except Exception:
                ok = False
            if not ok:
                filled.remove(q.label)
                todo.append({"id": q.id, "label": q.label, "why": couldnt})
        return {"filled": filled, "todo": todo}

    def highlight(self, report: dict) -> None:
        n, left = len(report["filled"]), len(report["todo"])
        message = (f"Just Job filled {n} field{'s' if n != 1 else ''}. "
                   + (f"{left} need{'s' if left == 1 else ''} you (outlined in orange). " if left else "")
                   + "Check everything, then press Submit.")
        self.page.evaluate(HIGHLIGHT_JS, [[t["id"] for t in report["todo"]], message])

    def confirmed(self) -> bool:
        if CONFIRM_URL.search(self.page.url):
            return True
        if self.page.locator(self.submit).count():
            return False   # still on the form
        return bool(CONFIRM_RE.search(self.page.inner_text("body", timeout=3000)[:20000]))

    # -- reading your final answers back --------------------------------------------------------

    def keep(self, reading: dict) -> None:
        """Merge a reading of the form; a field that has vanished (the page moving on) keeps its last value."""
        for qid, texts in (reading or {}).items():
            if texts is not None:
                self.final[qid] = texts

    def read(self) -> None:
        if self.specs:
            self.keep(self.page.evaluate(SNAPSHOT_JS, self.specs))

    def watch(self, questions: list[Question]) -> None:
        """Start reading the form: now, while you work on it, and when you press Submit."""
        self.specs = [{"id": q.id, "kind": q.kind} for q in questions if q.kind != "file"]
        try:
            self.page.expose_binding("__justjobSubmit", lambda source, reading: self.keep(reading))
            self.page.evaluate(HOOK_JS, self.specs)
            self.read()
        except Exception as e:   # reading back is for learning; filling must not depend on it
            log.info("apply: couldn't watch the form: %s", e)

    def wait_for_submit(self, minutes: float) -> str:
        """'submitted', 'closed' (you closed the window) or 'timeout'."""
        deadline = time.monotonic() + minutes * 60
        while time.monotonic() < deadline:
            if self.page.is_closed():
                return "closed"
            try:
                if self.confirmed():
                    return "submitted"
                self.read()
                self.page.wait_for_timeout(1500)
            except Exception:
                if self.page.is_closed():
                    return "closed"
                time.sleep(1.5)   # mid-navigation
        return "timeout"


class GreenhouseFiller(Filler):
    ready = '#first_name, #email'

    def fill(self, q: Question, value) -> bool:
        if q.id == "race" and not self.el("race").count() and self.el("hispanic_ethnicity").count():
            return self.race_in_two_steps(str(value))
        loc = self.el(q.id)
        if not loc.count():
            # Some custom questions are Yes/No buttons around an input that has only a name.
            return q.kind in ("select", "boolean") and self.choice(q.id, value)
        if q.kind == "file":
            return self.file(loc)
        if q.kind == "location" or q.id == "candidate-location":
            return self.place(loc, value)
        if q.kind == "multiselect":
            return all([self.combobox(loc, v) for v in value])
        if q.kind in ("select", "boolean") or loc.get_attribute("role") == "combobox":
            return self.combobox(loc, value)
        if q.id == "phone" and self.el("country").count():
            self.combobox(self.el("country"), self.country())   # the phone's country is a required dropdown
        return self.text(loc, value)

    def country(self) -> str:
        return home_country(self.prof) or "India"

    def race_in_two_steps(self, value: str) -> bool:
        """Newer Greenhouse forms ask 'Are you Hispanic/Latino?' first and show the race list only after 'No'."""
        if DECLINE_RE.search(norm(value)):
            return self.combobox(self.el("hispanic_ethnicity"), "Decline")
        if norm(value) == "hispanic or latino":
            return self.combobox(self.el("hispanic_ethnicity"), "Yes")
        if not self.combobox(self.el("hispanic_ethnicity"), "No"):
            return False
        try:
            self.el("race").wait_for(timeout=3000)
        except Exception:
            return False
        return self.combobox(self.el("race"), value)


class AshbyFiller(Filler):
    ready = '[id="_systemfield_email"], [name="_systemfield_email"]'

    def el(self, qid: str):
        # Some inputs (the location search box) have neither id nor name, only a wrapper naming the field.
        a = _attr(qid)
        return self.page.locator(f'[id="{a}"], [name="{a}"], [data-field-path="{a}"] :is(input:not([type=radio]):not([type=checkbox]), '
                                 f'textarea)').first

    def fill(self, q: Question, value) -> bool:
        if q.kind == "file":
            loc = self.page.locator(f'input[type=file][id="{_attr(q.id)}"]').first
            return bool(loc.count()) and self.file(loc)
        if q.kind == "boolean":
            return self.choice(q.id, value)
        if q.kind == "multiselect":
            return all([self.choice(q.id, v) for v in value])
        loc = self.el(q.id)
        if q.kind == "select":
            if loc.count() and loc.get_attribute("role") == "combobox":
                return self.combobox(loc, value)
            return self.choice(q.id, value)
        if not loc.count():
            return False
        if q.kind == "location" or loc.get_attribute("role") == "combobox":
            return self.place(loc, value)
        return self.text(loc, value)


FILLERS = {"greenhouse": GreenhouseFiller, "ashby": AshbyFiller}


# --- one browser session at a time ---------------------------------------------------------

_lock = threading.Lock()
LIVE: dict[str, dict] = {}   # job id -> what the review page shows while the browser is open


def busy() -> bool:
    return _lock.locked()


def _say(job_id: str, state: str, message: str) -> None:
    LIVE[job_id] = {"state": state, "message": message, "at": now_iso()}


def start(job_id: str, target: Target, questions: list[Question], answers: dict[str, Answer], *, prof: dict,
          resume: Path | None, db_path: str, data_dir: Path, browser: str = "msedge", wait_minutes: float = 45,
          headless: bool = False, job: dict | None = None) -> str | None:
    """Open the form in the background. Returns an error message, or None once it has started."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return "The apply agent needs Playwright: pip install -r requirements-extras.txt"
    if not _lock.acquire(blocking=False):
        return "Another application is open in the browser. Submit it or close that window first."
    _say(job_id, "filling", "Opening the browser…")
    args = (job_id, target, questions, answers, prof, resume, db_path, data_dir, browser, wait_minutes, headless,
            job or {"id": job_id})
    threading.Thread(target=_session, args=args, name="apply", daemon=True).start()
    return None


def _session(job_id, target, questions, answers, prof, resume, db_path, data_dir, browser, wait_minutes, headless, job):
    from playwright.sync_api import sync_playwright

    db = DB(db_path)
    try:
        db.update_application(job_id, now_iso(), state="filling", error=None)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(data_dir / "browser"), channel=browser or None, headless=headless,
                no_viewport=not headless, args=[] if headless else ["--start-maximized"])
            try:
                outcome = _drive(ctx, db, job_id, target, questions, answers, prof, resume, data_dir, wait_minutes, job)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
        log.info("apply: %s -> %s", job_id, outcome)
    except Exception as e:
        log.exception("apply: session for %s failed", job_id)
        db.update_application(job_id, now_iso(), state="failed", error=str(e)[:500])
        _say(job_id, "failed", f"Something went wrong: {str(e)[:200]}")
    finally:
        db.close()
        _lock.release()


def learned_line(outcome: dict) -> str:
    """'Saved 3 answers for next time. Your profile now says: ...' or ''."""
    n = len(outcome.get("saved") or [])
    line = f" Saved {n} answer{'s' if n != 1 else ''} for next time." if n else ""
    if outcome.get("profile"):
        line += " Your profile now says: " + " ".join(outcome["profile"])
    return line


def _drive(ctx, db, job_id, target, questions, answers, prof, resume, data_dir, wait_minutes, job) -> str:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(8000)
    filler = FILLERS[target.ats](page, prof, resume)
    page.goto(target.apply_url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector(filler.ready, timeout=45000)
    except Exception:
        if target.ats != "greenhouse":
            raise RuntimeError("The application form didn't load.") from None
        # Boards hosted on a company's own site still serve the form here.
        page.goto(f"https://job-boards.greenhouse.io/embed/job_app?for={target.board}&token={target.posting}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(filler.ready, timeout=30000)
    filler.settle()
    _say(job_id, "filling", "Filling in the form…")
    report = filler.fill_all(questions, answers)
    filler.highlight(report)
    filler.watch(questions)
    db.update_application(job_id, now_iso(), state="waiting", report=report)
    left = len(report["todo"])
    _say(job_id, "waiting", f"Filled {len(report['filled'])} fields"
         + (f"; {left} need you (outlined in orange)" if left else "")
         + ". Check the form in the browser window, then press Submit there.")

    outcome = filler.wait_for_submit(wait_minutes)
    now = now_iso()
    if outcome == "submitted":
        shots = data_dir / "applications"
        shots.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", job_id) + ".png"
        try:
            page.wait_for_timeout(1000)
            page.screenshot(path=str(shots / name), full_page=True)
        except Exception:
            name = None
        try:
            report["outcome"] = memory.learn(db, job, questions, answers, filler.final, now, prof)
        except Exception:   # learning must never cost you the record of a sent application
            log.exception("apply: learning from %s failed", job_id)
        db.update_application(job_id, now, state="submitted", submitted_at=now, screenshot=name, report=report)
        db.set_status(job_id, "applied", now)
        _say(job_id, "submitted", "Submitted. The job is marked as applied." + learned_line(report.get("outcome") or {}))
    elif outcome == "closed":
        db.update_application(job_id, now, state="closed")
        _say(job_id, "closed", "The browser window was closed before submitting. Nothing was sent.")
    else:
        db.update_application(job_id, now, state="closed", error=f"No submission after {wait_minutes:g} minutes.")
        _say(job_id, "closed", f"Stopped watching after {wait_minutes:g} minutes without a submission.")
    return outcome
