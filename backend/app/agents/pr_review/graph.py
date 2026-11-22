"""PR Review agent: LangGraph state machine.

START -> guard_input -> (review) fetch_pr -> plan -> [analyze_file x N] -> synthesize
      -> human_review (interrupt) -> post_review | skip_post -> remember -> END
      -> (chat)  chat <-> chat_tools -> END   (follow-ups in the same thread)
"""
import asyncio
import operator
import re
import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.store.base import BaseStore
from langgraph.types import Send, interrupt

from ...core.config import settings
from ...core.errors import ToolError
from ...security.guard import (UNTRUSTED_NOTICE, redact_secrets, scan_injection,
                               validate_user_message, wrap_untrusted)
from ...tools import github as gh
from .llm import get_llm, text_of
from .schemas import SEV_ORDER, FileReview, Summary


def accum(a: list | None, b: Any) -> list:
    """Append reducer; the string 'RESET' clears the list (new review in the same thread)."""
    if isinstance(b, str) and b == "RESET":
        return []
    return (a or []) + b


class State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    route: str
    pr_ref: dict
    pr: dict
    files: list[dict]
    targets: list[dict]
    skipped: list[dict]
    plan: list[str]
    prefs: list[str]
    findings: Annotated[list[dict], accum]
    warnings: Annotated[list[str], accum]
    activity: Annotated[list[dict], accum]
    final_findings: list[dict]
    summary: str
    verdict: str
    decision: dict
    posted: dict


NOISE = re.compile(
    r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|go\.sum|Pipfile\.lock"
    r"|\.min\.(js|css)$|(^|/)(dist|build|vendor|node_modules)/|\.snap$"
    r"|\.(png|jpe?g|gif|svg|ico|pdf|woff2?|ttf|zip|map|lock)$)", re.I)
DOC = re.compile(r"\.(md|txt|rst)$", re.I)


def _w(ev: dict) -> None:
    gh._writer()(ev)


def _last_human(state: State) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return text_of(m)
    return ""


def annotate_patch(patch: str) -> str:
    """Prefix added/context lines with their NEW-file line numbers so the model can cite lines."""
    out, n = [], 0
    for line in patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            n = int(m.group(1))
            out.append(line)
        elif line.startswith("+"):
            out.append(f"{n:>5}| {line}")
            n += 1
        elif line.startswith("-"):
            out.append(f"     | {line}")
        else:
            out.append(f"{n:>5}| {line}")
            n += 1
    return "\n".join(out)


# ---------------------------------------------------------------- nodes

async def guard_input(state: State) -> dict:
    text, err = validate_user_message(_last_human(state))
    if err:
        return {"route": "stop", "messages": [AIMessage(err)]}
    ref = gh.parse_pr_url(text)
    if ref:
        return {"route": "review", "pr_ref": ref, "findings": "RESET", "warnings": "RESET",
                "activity": "RESET", "final_findings": [], "posted": {}, "decision": {}}
    if state.get("pr"):
        return {"route": "chat"}
    if "github.com" in text and "/pull/" not in text:
        msg = "That looks like a GitHub link but not a pull request. I need a PR URL like https://github.com/owner/repo/pull/123."
    else:
        msg = ("Hi! I'm RepoMate. Paste a **GitHub pull request URL** "
               "(e.g. `https://github.com/owner/repo/pull/123`) and I'll review it.")
    return {"route": "stop", "messages": [AIMessage(msg)]}


async def fetch_pr(state: State, *, store: BaseStore | None = None) -> dict:
    ref = state["pr_ref"]
    owner, repo, num = ref["owner"], ref["repo"], ref["number"]
    activity: list[dict] = []
    try:
        pr, rec = await gh.traced("get_pr_metadata", ref, lambda: gh.get_pr_metadata(owner, repo, num),
                                  lambda r: f'#{num} "{r["title"][:60]}" by {r["author"]}, {r["changed_files"]} files')
        activity.append(rec)
        files, rec = await gh.traced("list_pr_files", ref, lambda: gh.list_pr_files(owner, repo, num),
                                     lambda r: f"{len(r)} files")
        activity.append(rec)
    except Exception as e:
        if getattr(e, "activity", None):
            activity.append(e.activity)
        reason = str(e) if isinstance(e, ToolError) else "Unexpected error fetching the PR."
        return {"route": "stop", "activity": activity,
                "messages": [AIMessage(f"I couldn't fetch that PR: {reason}")]}

    warnings: list[str] = []
    hits = scan_injection(pr["title"] + "\n" + pr["body"])
    if hits:
        warnings.append(f"Possible prompt injection in PR title/description ({', '.join(hits[:3])}). Treated as data, not instructions.")
    if pr["changed_files"] > settings.max_changed_files_hard:
        warnings.append(f"PR has {pr['changed_files']} files; only the first {settings.max_changed_files_hard} were listed.")
    if pr["state"] != "open":
        warnings.append(f"PR is {pr['state']}; reviewing anyway.")

    prefs: list[str] = []
    if store is not None:
        try:
            items = await store.asearch(("prefs", "default"), limit=20)
            prefs = [i.value["text"] for i in items]
            if prefs:
                _w({"type": "memory", "text": f"Loaded {len(prefs)} saved preference(s)"})
        except Exception:
            pass
    return {"route": "ok", "pr": pr, "files": files, "prefs": prefs, "activity": activity, "warnings": warnings}


async def plan(state: State) -> dict:
    files = state["files"]
    targets, skipped = [], []
    for f in files:
        if NOISE.search(f["filename"]):
            skipped.append({"file": f["filename"], "reason": "generated/lock/binary asset"})
        elif not f.get("patch"):
            skipped.append({"file": f["filename"], "reason": "no textual diff (binary, rename or too large)"})
        elif f["status"] == "removed":
            skipped.append({"file": f["filename"], "reason": "file deleted"})
        else:
            targets.append(f)
    targets.sort(key=lambda f: (bool(DOC.search(f["filename"])), -(f["additions"] + f["deletions"])))
    for f in targets[settings.max_files:]:
        skipped.append({"file": f["filename"], "reason": f"over budget (max {settings.max_files} files)"})
    targets = targets[: settings.max_files]
    steps = [f"Fetched PR #{state['pr_ref']['number']} ({len(files)} files)",
             f"Review {len(targets)} file(s) in parallel for bugs, security, performance, readability"
             + (f"; skip {len(skipped)}" if skipped else ""),
             "Merge, de-duplicate and rank findings; write walkthrough",
             "Ask you to approve findings before anything is posted",
             "Post the review (or give you markdown) and remember the outcome"]
    _w({"type": "plan", "steps": steps, "targets": [t["filename"] for t in targets], "skipped": skipped})
    return {"targets": targets, "skipped": skipped, "plan": steps}


def fan_out(state: State):
    if not state.get("targets"):
        return "synthesize"
    pr = state["pr"]
    return [Send("analyze_file", {"file": f, "title": pr["title"], "body": pr["body"],
                                  "prefs": state.get("prefs", [])}) for f in state["targets"]]


FILE_SYSTEM = (
    "You are a meticulous senior engineer doing a pull-request code review of ONE file. "
    "Find real problems introduced by the change: bugs, security issues, performance problems, unclear code, "
    "missing tests/docs. Be specific and avoid generic advice and style nitpicks unless asked. "
    "Only comment on lines that were changed. Cite the NEW-file line number shown at the left. "
    "Judge the change against the PR's stated intent: do NOT flag changes consistent with it. "
    "Only report problems visible in the diff shown; do NOT speculate about code you cannot see "
    "(no 'verify elsewhere' or 'might remain' findings). Prefer fewer, higher-confidence findings. "
    "If the change looks fine, return no findings.\n" + UNTRUSTED_NOTICE
)


async def analyze_file(payload: dict) -> dict:
    f = payload["file"]
    name = f["filename"]
    patch = f["patch"]
    truncated = len(patch) > settings.max_patch_chars
    patch = patch[: settings.max_patch_chars]
    findings: list[dict] = []
    warnings: list[str] = []
    added = "\n".join(l for l in patch.splitlines() if l.startswith("+"))
    hits = scan_injection(added)
    if hits:
        warnings.append(f"{name}: prompt-injection-like text in the diff ({', '.join(hits[:2])}). Ignored as instructions.")
        findings.append({"file": name, "line": None, "severity": "high", "category": "security",
                         "title": "Possible prompt injection in PR content",
                         "comment": "This change contains text that tries to instruct an AI reviewer: "
                                    f"`{hits[0][:80]}`. Verify it is intentional and remove it.", "suggestion": None})
    if truncated:
        warnings.append(f"{name}: diff truncated to {settings.max_patch_chars} chars.")

    prefs = ("\nUser review preferences (follow them):\n- " + "\n- ".join(payload["prefs"])) if payload["prefs"] else ""
    messages = [
        SystemMessage(FILE_SYSTEM + prefs),
        HumanMessage(
            wrap_untrusted("pr_title", payload["title"]) + "\n"
            + wrap_untrusted("pr_description", payload["body"][:1500]) + "\n"
            + f"File: {name} (status: {f['status']})\n"
            + wrap_untrusted("diff", annotate_patch(patch))),
    ]
    structured = get_llm().with_structured_output(FileReview)
    try:
        result, rec = await gh.traced(
            "review_file", {"file": name}, lambda: structured.ainvoke(messages),
            lambda r: f"{len(r.findings)} finding(s)" if r else "no result")
        for rf in (result.findings if result else []):
            findings.append({"file": name, **rf.model_dump()})
        act = [rec]
    except Exception as e:
        act = [e.activity] if getattr(e, "activity", None) else []
        warnings.append(f"{name}: could not be analyzed ({type(e).__name__}); skipped.")
    return {"findings": findings, "warnings": warnings, "activity": act}


async def synthesize(state: State) -> dict:
    seen, merged = set(), []
    for fd in state.get("findings", []):
        key = (fd["file"], fd.get("line"), fd["title"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        fd = {**fd, "comment": redact_secrets(fd["comment"]), "title": redact_secrets(fd["title"]),
              "suggestion": redact_secrets(fd["suggestion"]) if fd.get("suggestion") else None}
        merged.append(fd)
    merged.sort(key=lambda x: (SEV_ORDER[x["severity"]], x["file"], x.get("line") or 0))
    merged = merged[:25]
    for i, fd in enumerate(merged, 1):
        fd["id"] = f"f{i}"

    pr = state["pr"]
    brief = "\n".join(f"- [{x['severity']}] {x['file']}:{x.get('line')} {x['title']}" for x in merged) or "(no findings)"
    msgs = [SystemMessage("You summarize pull-request reviews for maintainers. Be concise, honest and plain; "
                          "describe the findings as your own review, never as 'supplied' by someone. "
                          + UNTRUSTED_NOTICE),
            HumanMessage(wrap_untrusted("pr_title", pr["title"]) + "\n"
                         + wrap_untrusted("pr_description", pr["body"][:1000])
                         + f"\nStats: {pr['changed_files']} files, +{pr['additions']}/-{pr['deletions']}\n"
                         + f"Findings:\n{brief}")]
    try:
        s, rec = await gh.traced("summarize_review", {"findings": len(merged)},
                                 lambda: get_llm().with_structured_output(Summary).ainvoke(msgs),
                                 lambda r: f"verdict: {r.verdict}")
        summary, verdict, act = redact_secrets(s.walkthrough), s.verdict, [rec]
    except Exception as e:
        act = [e.activity] if getattr(e, "activity", None) else []
        crit = any(x["severity"] in ("critical", "high") for x in merged)
        summary = (f"Automated summary unavailable. {len(merged)} finding(s) across "
                   f"{len({x['file'] for x in merged})} file(s).")
        verdict = "request_changes" if crit else "comment"
        return {"final_findings": merged, "summary": summary, "verdict": verdict, "activity": act,
                "warnings": ["Summary model call failed; used a fallback summary."]}
    return {"final_findings": merged, "summary": summary, "verdict": verdict, "activity": act}


async def human_review(state: State) -> dict:
    """Human-in-the-loop checkpoint: nothing is posted without explicit approval."""
    decision = interrupt({
        "type": "approval", "summary": state.get("summary", ""), "verdict": state.get("verdict"),
        "findings": state.get("final_findings", []), "warnings": state.get("warnings", []),
        "skipped": state.get("skipped", []), "can_post": bool(settings.github_token),
        "pr": {k: state["pr"][k] for k in ("title", "html_url", "author")},
    })
    if not isinstance(decision, dict):
        decision = {"action": "reject"}
    return {"decision": decision}


def route_decision(state: State) -> str:
    d = state.get("decision") or {}
    return "post" if d.get("action") == "approve" and d.get("approved_ids") else "skip"


def review_markdown(state: State, findings: list[dict]) -> str:
    pr = state["pr"]
    lines = [f"## RepoMate review: {pr['title']}", "", state.get("summary", ""), ""]
    if not findings:
        lines.append("No issues found in the reviewed changes.")
    for fd in findings:
        loc = f"`{fd['file']}`" + (f":{fd['line']}" if fd.get("line") else "")
        lines.append(f"- **[{fd['severity']}] {fd['title']}** ({loc})  \n  {fd['comment']}")
        if fd.get("suggestion"):
            lines.append(f"  - Suggestion: {fd['suggestion']}")
    return "\n".join(lines)


async def post_review(state: State) -> dict:
    d = state["decision"]
    ids, edits = set(d.get("approved_ids", [])), d.get("edits", {}) or {}
    approved = []
    for fd in state.get("final_findings", []):
        if fd["id"] in ids:
            c = redact_secrets(str(edits.get(fd["id"], fd["comment"]))[:2000])
            approved.append({**fd, "comment": c})
    md = review_markdown(state, approved)
    ref, pr = state["pr_ref"], state["pr"]
    inline = [{"path": a["file"], "line": a["line"],
               "body": f"**[{a['severity']}] {a['title']}**\n\n{a['comment']}"
                       + (f"\n\nSuggestion: {a['suggestion']}" if a.get("suggestion") else "")}
              for a in approved if a.get("line")]
    general = [a for a in approved if not a.get("line")]
    body = state.get("summary", "") + ("\n\n" + "\n".join(f"- **{a['title']}** (`{a['file']}`): {a['comment']}"
                                                          for a in general) if general else "")
    act = []
    posted: dict = {"ok": False, "markdown": md}
    for attempt, (b, c) in enumerate([(body, inline), (md, [])]):  # 2nd try: single body, no inline
        try:
            res, rec = await gh.traced("post_pr_review", {"comments": len(c), **ref},
                                       lambda: gh.post_pr_review(ref["owner"], ref["repo"], ref["number"],
                                                                 pr.get("head_sha"), b, c),
                                       lambda r: r.get("url") or "posted")
            act.append(rec)
            posted = {"ok": True, "url": res.get("url"), "markdown": md}
            break
        except Exception as e:
            if getattr(e, "activity", None):
                act.append(e.activity)
            posted["error"] = str(e) if isinstance(e, ToolError) else "unexpected error"
            if "422" not in posted["error"]:
                break
    if posted["ok"]:
        msg = f"Review posted to GitHub: {posted['url']}"
    else:
        msg = (f"I couldn't post to GitHub ({posted['error']}). Nothing was lost. "
               f"Here is your approved review to copy:\n\n{md}")
    return {"posted": posted, "activity": act, "messages": [AIMessage(msg)]}


async def skip_post(state: State) -> dict:
    d = state.get("decision") or {}
    if d.get("action") == "approve":
        msg = "No findings were selected, so nothing was posted."
    else:
        msg = "Review discarded. Nothing was posted. Ask me anything about the PR or send another URL."
    return {"posted": {"ok": False, "skipped": True}, "messages": [AIMessage(msg)]}


async def remember(state: State, *, store: BaseStore | None = None) -> dict:
    if store is not None:
        ref = state["pr_ref"]
        try:
            await store.aput(("reviews", f"{ref['owner']}/{ref['repo']}"), f"pr-{ref['number']}", {
                "title": state["pr"]["title"], "summary": state.get("summary", ""),
                "findings": len(state.get("final_findings", [])),
                "posted": bool(state.get("posted", {}).get("ok"))})
            _w({"type": "memory", "text": f"Saved review of {ref['owner']}/{ref['repo']}#{ref['number']} to long-term memory"})
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------- follow-up chat

@tool
def get_file_content(path: str, ref: str | None = None) -> str:
    """Fetch the full content of a file in the PR's repository (for extra context)."""
    return ""


@tool
def get_pr_diff(path: str) -> str:
    """Get the diff of one changed file from the reviewed PR."""
    return ""


@tool
def save_preference(text: str) -> str:
    """Remember a durable user preference for future reviews (e.g. 'ignore style nits')."""
    return ""


CHAT_TOOLS = [get_file_content, get_pr_diff, save_preference]


async def chat(state: State, *, store: BaseStore | None = None) -> dict:
    pr, ref = state["pr"], state["pr_ref"]
    findings = "\n".join(f"- {f['id']} [{f['severity']}] {f['file']}:{f.get('line')} {f['title']}: {f['comment'][:200]}"
                         for f in state.get("final_findings", [])) or "(none)"
    memory = ""
    if store is not None:
        try:
            items = await store.asearch(("reviews", f"{ref['owner']}/{ref['repo']}"), limit=5)
            memory = "\n".join(f"- {i.key}: {i.value['title']} ({i.value['findings']} findings)" for i in items)
        except Exception:
            pass
    prefs = "\n".join(f"- {p}" for p in state.get("prefs", []))
    sys = (f"You are RepoMate, a helpful code-review agent. You reviewed {ref['owner']}/{ref['repo']}#{ref['number']}.\n"
           "Answer follow-up questions concisely. Use tools when you need file content or a diff. "
           "Use save_preference only when the user states a lasting preference.\n"
           f"Findings from this review:\n{findings}\nPast reviews in this repo:\n{memory or '(none)'}\n"
           f"Saved preferences:\n{prefs or '(none)'}\n" + UNTRUSTED_NOTICE)
    msgs = state["messages"][-16:]
    rounds = 0
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, ToolMessage):
            rounds += 1
    llm = get_llm() if rounds >= settings.max_chat_tool_rounds else get_llm().bind_tools(CHAT_TOOLS)
    try:
        resp = await llm.ainvoke([SystemMessage(sys)] + msgs)
    except Exception as e:
        return {"messages": [AIMessage(f"Sorry, the model call failed ({type(e).__name__}). Please try again.")]}
    if isinstance(resp, AIMessage) and resp.tool_calls and rounds >= settings.max_chat_tool_rounds:
        resp = AIMessage(text_of(resp) or "I've used my tool budget; here is what I have so far.")
    return {"messages": [resp]}


def chat_route(state: State) -> str:
    last = state["messages"][-1]
    return "chat_tools" if isinstance(last, AIMessage) and last.tool_calls else "end"


async def chat_tools(state: State, *, store: BaseStore | None = None) -> dict:
    last: AIMessage = state["messages"][-1]
    ref, pr = state["pr_ref"], state["pr"]
    out, act = [], []
    for call in last.tool_calls:
        name, args = call["name"], call.get("args") or {}
        try:
            if name == "get_file_content":
                res, rec = await gh.traced(name, args, lambda: gh.get_file_content(
                    ref["owner"], ref["repo"], str(args.get("path", "")), args.get("ref") or pr.get("head_sha")))
                content = wrap_untrusted(f"file:{args.get('path')}", res)
            elif name == "get_pr_diff":
                async def _d():
                    p = next((f for f in state.get("files", []) if f["filename"] == args.get("path")), None)
                    if not p or not p.get("patch"):
                        raise ToolError("No diff for that path in this PR.")
                    return p["patch"][: settings.max_patch_chars]
                res, rec = await gh.traced(name, args, _d)
                content = wrap_untrusted(f"diff:{args.get('path')}", res)
            elif name == "save_preference":
                async def _s():
                    t = redact_secrets(str(args.get("text", ""))[:200]).strip()
                    if not t or store is None:
                        raise ToolError("Nothing to save.")
                    await store.aput(("prefs", "default"), uuid.uuid4().hex[:8], {"text": t})
                    return t
                res, rec = await gh.traced(name, args, _s, lambda r: f"saved: {r}")
                content = "Saved."
            else:
                raise ToolError(f"Unknown tool {name}")
            act.append(rec)
        except Exception as e:
            if getattr(e, "activity", None):
                act.append(e.activity)
            content = f"Tool error: {e if isinstance(e, ToolError) else 'unexpected failure'}. Continue without it."
        out.append(ToolMessage(content=content, tool_call_id=call["id"], name=name))
    return {"messages": out, "activity": act}


# ---------------------------------------------------------------- assembly

def build_graph(checkpointer, store=None):
    g = StateGraph(State)
    for n, f in [("guard_input", guard_input), ("fetch_pr", fetch_pr), ("plan", plan),
                 ("analyze_file", analyze_file), ("synthesize", synthesize), ("human_review", human_review),
                 ("post_review", post_review), ("skip_post", skip_post), ("remember", remember),
                 ("chat", chat), ("chat_tools", chat_tools)]:
        g.add_node(n, f)
    g.add_edge(START, "guard_input")
    g.add_conditional_edges("guard_input", lambda s: s["route"],
                            {"review": "fetch_pr", "chat": "chat", "stop": END})
    g.add_conditional_edges("fetch_pr", lambda s: "stop" if s.get("route") == "stop" else "plan",
                            {"plan": "plan", "stop": END})
    g.add_conditional_edges("plan", fan_out, ["analyze_file", "synthesize"])
    g.add_edge("analyze_file", "synthesize")
    g.add_edge("synthesize", "human_review")
    g.add_conditional_edges("human_review", route_decision, {"post": "post_review", "skip": "skip_post"})
    g.add_edge("post_review", "remember")
    g.add_edge("skip_post", "remember")
    g.add_edge("remember", END)
    g.add_conditional_edges("chat", chat_route, {"chat_tools": "chat_tools", "end": END})
    g.add_edge("chat_tools", "chat")
    return g.compile(checkpointer=checkpointer, store=store)
