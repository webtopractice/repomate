"""GitHub tools: resilient async REST wrappers + a tracing helper that streams tool calls to the UI."""
import asyncio
import re
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx

from ..core.config import settings
from ..core.errors import ToolError
from ..security.guard import redact_secrets

API = "https://api.github.com"
PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


def parse_pr_url(text: str) -> dict | None:
    m = PR_URL_RE.search(text or "")
    if not m:
        return None
    return {"owner": m.group(1), "repo": m.group(2), "number": int(m.group(3))}


def _headers(accept: str = "application/vnd.github+json") -> dict:
    h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "repomate"}
    if settings.github_token:
        h["Authorization"] = f"Bearer {settings.github_token}"
    return h


async def _request(method: str, path: str, *, params=None, json=None, accept=None) -> httpx.Response:
    url = path if path.startswith("http") else f"{API}{path}"
    last: Exception | None = None
    for attempt in range(settings.tool_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.tool_timeout_s) as c:
                r = await c.request(method, url, params=params, json=json,
                                    headers=_headers(accept or "application/vnd.github+json"))
            if r.status_code in (500, 502, 503, 504):
                raise ToolError(f"GitHub temporarily unavailable ({r.status_code})", retryable=True)
            if r.status_code == 401:
                raise ToolError("GitHub token is invalid or expired.")
            if r.status_code in (403, 429):
                if r.headers.get("x-ratelimit-remaining") == "0" or r.status_code == 429:
                    reset = r.headers.get("x-ratelimit-reset")
                    when = ""
                    if reset and reset.isdigit():
                        when = f" Resets in ~{max(0, int(reset) - int(time.time())) // 60} min."
                    raise ToolError("GitHub API rate limit reached." + when +
                                    ("" if settings.github_token else " Add a GITHUB_TOKEN to raise the limit."))
                raise ToolError("GitHub refused the request (403): token lacks permission for this action.")
            if r.status_code == 404:
                raise ToolError("Not found on GitHub (it may not exist, or it is private).")
            if r.status_code == 422:
                raise ToolError(f"GitHub rejected the payload (422): {r.text[:200]}")
            if r.status_code >= 400:
                raise ToolError(f"GitHub error {r.status_code}")
            return r
        except ToolError as e:
            last = e
            if not e.retryable:
                raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = ToolError(f"Network problem talking to GitHub: {type(e).__name__}", retryable=True)
        if attempt < settings.tool_retries:
            await asyncio.sleep(0.5 * (2 ** attempt))
    raise last or ToolError("GitHub request failed")


# ---- Tools ---------------------------------------------------------------

async def get_pr_metadata(owner: str, repo: str, number: int) -> dict:
    r = (await _request("GET", f"/repos/{owner}/{repo}/pulls/{number}")).json()
    return {
        "title": r.get("title") or "",
        "body": (r.get("body") or "")[:4000],
        "author": (r.get("user") or {}).get("login"),
        "state": r.get("state"),
        "draft": r.get("draft"),
        "base": (r.get("base") or {}).get("ref"),
        "head_sha": (r.get("head") or {}).get("sha"),
        "changed_files": r.get("changed_files", 0),
        "additions": r.get("additions", 0),
        "deletions": r.get("deletions", 0),
        "html_url": r.get("html_url"),
    }


async def list_pr_files(owner: str, repo: str, number: int) -> list[dict]:
    files: list[dict] = []
    for page in range(1, 4):  # max 300 files
        r = await _request("GET", f"/repos/{owner}/{repo}/pulls/{number}/files",
                           params={"per_page": 100, "page": page})
        batch = r.json()
        files += [{
            "filename": f["filename"], "status": f["status"], "additions": f["additions"],
            "deletions": f["deletions"], "patch": f.get("patch"),
        } for f in batch]
        if len(batch) < 100:
            break
    return files


async def get_file_content(owner: str, repo: str, path: str, ref: str | None = None) -> str:
    if ".." in path or path.startswith("/"):
        raise ToolError("Invalid file path.")
    r = await _request("GET", f"/repos/{owner}/{repo}/contents/{path}",
                       params={"ref": ref} if ref else None, accept="application/vnd.github.raw+json")
    return r.text[:20000]


async def post_pr_review(owner: str, repo: str, number: int, head_sha: str | None,
                         body: str, comments: list[dict]) -> dict:
    payload: dict[str, Any] = {"body": redact_secrets(body), "event": "COMMENT"}
    if head_sha:
        payload["commit_id"] = head_sha
    if comments:
        payload["comments"] = [{"path": c["path"], "line": c["line"], "side": "RIGHT",
                                "body": redact_secrets(c["body"])} for c in comments]
    r = await _request("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload)
    return {"url": r.json().get("html_url")}


# ---- Tracing -------------------------------------------------------------

def _writer() -> Callable[[dict], None]:
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except Exception:
        return lambda _e: None


def _preview(result: Any) -> str:
    s = str(result)
    return redact_secrets(s[:200] + ("..." if len(s) > 200 else ""))


async def traced(name: str, args: dict, fn: Callable[[], Awaitable[Any]],
                 summarize: Callable[[Any], str] | None = None) -> tuple[Any, dict]:
    """Run a tool, stream start/end events, return (result, activity_record).

    On failure raises the original exception with `.activity` attached."""
    w = _writer()
    cid = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    w({"type": "tool_start", "id": cid, "name": name, "args": args})
    try:
        result = await fn()
        rec = {"id": cid, "name": name, "args": args, "status": "ok",
               "ms": int((time.perf_counter() - t0) * 1000),
               "preview": summarize(result) if summarize else _preview(result)}
        w({"type": "tool_end", **rec})
        return result, rec
    except Exception as e:
        msg = str(e) if isinstance(e, ToolError) else f"{type(e).__name__}: {e}"
        rec = {"id": cid, "name": name, "args": args, "status": "error",
               "ms": int((time.perf_counter() - t0) * 1000), "preview": msg[:200]}
        w({"type": "tool_end", **rec})
        e.activity = rec  # type: ignore[attr-defined]
        raise
