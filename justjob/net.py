"""Tiny HTTP helpers on top of urllib, with retries for transient failures."""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "just-job/0.1 (personal job search tool)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = "", retry_after: float | None = None):
        super().__init__(f"HTTP {status} from {url}: {' '.join(body.split())[:200]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after   # seconds the server asked us to wait, if it said


def _safe(url: str) -> str:
    # Query strings (Adzuna) and Telegram bot paths carry secrets; keep them out of errors and logs.
    return re.sub(r"/bot[^/]+/", "/bot***/", url.split("?", 1)[0])


def _decode(raw: bytes, encoding: str | None) -> bytes:
    return gzip.decompress(raw) if encoding == "gzip" else raw


def request(
    url: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    headers: dict | None = None,
    data: dict | None = None,
    timeout: float = 60,
    retries: int = 2,
) -> bytes:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _decode(resp.read(), resp.headers.get("Content-Encoding"))
        except urllib.error.HTTPError as e:
            text = _decode(e.read(), e.headers.get("Content-Encoding")).decode("utf-8", "replace")
            wait = _retry_after(e)
            # A long Retry-After (Workable answers 429 with ~a day once a daily quota is used up)
            # isn't worth sleeping through; the caller decides what to do.
            if e.code in RETRY_STATUS and attempt < retries and (wait is None or wait <= 60):
                time.sleep(wait or 2 ** (attempt + 1))
                continue
            raise HttpError(e.code, _safe(url), text, wait) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise ConnectionError(f"{_safe(url)}: {e}") from None
    raise AssertionError("unreachable")


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    value = e.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def get_json(url: str, **kw):
    return json.loads(request(url, **kw))


def post_json(url: str, data: dict, **kw):
    return json.loads(request(url, method="POST", data=data, **kw))
