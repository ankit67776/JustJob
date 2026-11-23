"""Reads Greenhouse and Ashby application forms into one Question schema.

Both serve the questions without a key (Ashby through the GraphQL query its own job pages run), but
submitting through either API needs the employer's key. So the form is filled in a real browser
(browser.py) and you press Submit.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from ..net import HttpError, get_json
from ..text import html_to_text

SUPPORTED = ("greenhouse", "ashby")
KINDS = ("text", "textarea", "email", "phone", "url", "number", "date", "select", "multiselect", "boolean", "file",
         "location")


class FormError(Exception):
    """The form couldn't be read; the message is shown to you as is."""


@dataclass
class Question:
    id: str                  # the field's name on the form (also its element id on the page)
    label: str
    kind: str                # one of KINDS
    required: bool = False
    options: list[str] = field(default_factory=list)
    group: str = "custom"    # standard (name, email, resume, ...) | custom | eeo (voluntary disclosures)
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Question:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass(frozen=True)
class Target:
    ats: str
    board: str
    posting: str

    @property
    def apply_url(self) -> str:
        if self.ats == "greenhouse":
            return f"https://job-boards.greenhouse.io/{self.board}/jobs/{self.posting}"
        return f"https://jobs.ashbyhq.com/{self.board}/{self.posting}/application"


GH_URL = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([a-z0-9-]+)(?:/jobs/|&token=)(\d+)", re.I)
ASHBY_URL = re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def target(job) -> Target | None:
    """The supported form a job leads to, from its id or, for jobs found on other sites, its link."""
    source, _, rest = (job["id"] or "").partition(":")
    url = job["url"] or ""
    if source == "greenhouse" and "/" in rest:
        board, posting = rest.split("/", 1)
        return Target("greenhouse", board, posting)
    if source == "ashby" and "/" in rest:
        board, posting = rest.split("/", 1)
        return Target("ashby", board, posting)
    if m := GH_URL.search(url):
        return Target("greenhouse", m.group(1).lower(), m.group(2))
    if m := ASHBY_URL.search(url):
        return Target("ashby", m.group(1), m.group(2).lower())
    return None


def _label(text) -> str:
    return re.sub(r"\s+", " ", html_to_text(str(text or ""))).strip().rstrip("*").strip()


GH_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{posting}"
GH_KIND = {"input_text": "text", "textarea": "textarea", "input_file": "file",
           "multi_value_single_select": "select", "multi_value_multi_select": "multiselect"}
GH_STANDARD = {"first_name", "last_name", "preferred_name", "email", "phone", "resume", "cover_letter"}
GH_EEO = {"gender": "Gender", "race": "Race", "hispanic_ethnicity": "Are you Hispanic or Latino?",
          "veteran_status": "Veteran status", "disability_status": "Disability status"}


def greenhouse_questions(data: dict) -> list[Question]:
    out = []
    for q in data.get("questions") or []:
        fields = [f for f in q.get("fields") or [] if f.get("type") != "input_hidden"]
        if not fields:
            continue
        f = fields[0]   # "Resume/CV" also offers a paste-as-text box; the file comes first and is what we use
        name = f["name"]
        kind = {"email": "email", "phone": "phone"}.get(name, GH_KIND.get(f.get("type"), "text"))
        out.append(Question(name, _label(q.get("label")), kind, bool(q.get("required")),
                            [v["label"] for v in f.get("values") or [] if v.get("label")],
                            "standard" if name in GH_STANDARD else "custom", _label(q.get("description"))[:500]))
    for q in data.get("location_questions") or []:
        if any(f.get("name") == "location" for f in q.get("fields") or []):
            # The page shows this as a city search box with its own id.
            out.append(Question("candidate-location", "Location (city)", "location", bool(q.get("required")),
                                group="standard"))
    for block in data.get("compliance") or []:
        for q in block.get("questions") or []:
            for f in q.get("fields") or []:
                out.append(Question(f["name"], GH_EEO.get(f["name"], _label(q.get("label"))), "select",
                                    bool(q.get("required")), [v["label"] for v in f.get("values") or []], "eeo"))
    demo = data.get("demographic_questions")
    for q in (demo.get("questions") or []) if isinstance(demo, dict) else []:
        out.append(Question(f"demographic-{q.get('id')}", _label(q.get("label")),
                            "multiselect" if q.get("type") == "multi_value_multi_select" else "select",
                            bool(q.get("required")), [o.get("label") for o in q.get("answer_options") or [] if o.get("label")],
                            "eeo"))
    return out


ASHBY_GQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id title applicationForm { ...F } surveyForms { ...F }
  }
}
fragment F on FormRender {
  sections { fieldEntries { ... on FormFieldEntry { field isRequired descriptionHtml isHidden } } }
}"""
ASHBY_KIND = {"String": "text", "Email": "email", "Phone": "phone", "File": "file", "Url": "url", "Location": "location",
              "LongText": "textarea", "ValueSelect": "select", "MultiValueSelect": "multiselect", "Boolean": "boolean",
              "Number": "number", "Date": "date"}


def ashby_questions(data: dict) -> list[Question]:
    posting = ((data or {}).get("data") or {}).get("jobPosting")
    if not posting:
        raise FormError("Ashby doesn't know this posting anymore; it has probably closed.")
    forms = [("custom", posting.get("applicationForm"))] + [("eeo", f) for f in posting.get("surveyForms") or []]
    out = []
    for group, form in forms:
        for section in (form or {}).get("sections") or []:
            for entry in section.get("fieldEntries") or []:
                f = entry.get("field")
                if entry.get("isHidden") or not isinstance(f, dict) or not f.get("path"):
                    continue
                kind = ASHBY_KIND.get(f.get("type"), "text")
                options = [v["label"] for v in f.get("selectableValues") or [] if v.get("label")]
                if kind == "boolean":
                    options = ["Yes", "No"]
                out.append(Question(f["path"], _label(f.get("title")), kind, bool(entry.get("isRequired")), options,
                                    "standard" if f["path"].startswith("_systemfield_") else group,
                                    _label(entry.get("descriptionHtml"))[:500]))
    return out


def fetch_questions(t: Target) -> list[Question]:
    try:
        if t.ats == "greenhouse":
            questions = greenhouse_questions(get_json(GH_API.format(board=t.board, posting=t.posting),
                                                      params={"questions": "true"}))
        else:
            variables = {"organizationHostedJobsPageName": t.board, "jobPostingId": t.posting}
            questions = ashby_questions(get_json(ASHBY_GQL, params={"op": "ApiJobPosting", "query": ASHBY_QUERY,
                                                                   "variables": json.dumps(variables)},
                                                 headers={"apollo-require-preflight": "true"}))
    except HttpError as e:
        if e.status == 404:
            raise FormError("The posting is gone; it has probably closed.") from None
        raise FormError(f"Couldn't read the form ({t.ats} answered HTTP {e.status}). Try again later.") from None
    if not questions:
        raise FormError("This form has no questions we can read.")
    return questions
