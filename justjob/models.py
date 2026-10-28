from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Job:
    """A posting normalized from any source."""

    source: str              # fetcher name, e.g. "greenhouse"
    source_id: str           # unique within the source, e.g. "anthropic/4461450008"
    title: str
    company: str
    url: str                 # where you apply (or the posting page that links to it)
    location: str = ""
    remote: bool | None = None
    description: str = ""    # plain text
    posted_at: str | None = None   # ISO 8601 UTC
    salary: str = ""
    board: str = ""          # e.g. "greenhouse:anthropic"; used to notice closed postings

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"


@dataclass
class BoardResult:
    """What one fetch task returns.

    `snapshot` means `jobs` is the board's complete current listing, so stored jobs from this
    board that are missing from it have closed. Sources that only expose recent postings set it
    to False and their jobs age out instead.
    """

    board: str
    jobs: list[Job]
    snapshot: bool
    # IDs currently listed on the board, when that differs from `jobs` (e.g. jobs whose details
    # failed to download are left out of `jobs` but are still listed).
    listed_ids: list[str] | None = field(default=None)
