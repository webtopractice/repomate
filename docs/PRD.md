# RepoMate: Product Requirements Document (PRD)

Status: **DRAFT. Awaiting approval. No code is written until you approve.**
Track: **03, Builder Tools** ("Agents for developers and communities")
Model: `claude-haiku-5-5` (API key supplied by you, kept in `.env`, never committed)
Repo: https://github.com/webtopractice/repomate

---

## 1. Purpose

Developers lose hours on PR review, on chasing bugs, and on docs that go stale. RepoMate is a **builder-tool agent** that takes a real task from start to finish: *"Review this PR"*. It reads the PR, plans, calls tools, finds issues, drafts review comments, **asks a human to approve**, then posts them to GitHub. It acts. It does not just chat.

The product is a platform with several agents behind it. The MVP ships one agent with one feature, **PR Review**, on a base that the other three Track-03 ideas can plug into:

| Phase | Agent / feature | Status |
|---|---|---|
| **MVP** | PR Review agent | Build now |
| v2 | Bug-fix agent (issue → patch → draft PR) | Later |
| v3 | Docs Q&A and doc generation | Later |
| v4 | Onboarding buddy, event ops | Later |

## 2. How we will be judged (from the slides) and how we answer

| Criterion | Pts | Our answer |
|---|---|---|
| **Real-world usefulness** | 30 | PR review is a daily pain that is real and specific. Works on any public GitHub PR URL. Output is inline, actionable comments, severity-ranked, plus a summary. Reviews follow the way tools like CodeRabbit, Copilot review and PR-Agent work. |
| **Agent design** | 25 | A LangGraph state machine with **planning** (a plan node), **tools** (GitHub read and write), **memory** (checkpointer plus long-term store) and **actions** (posts reviews). The UI shows every tool call live. |
| **Unbreakable** | 25 | Layered defences against prompt injection, bad input and tool failure (see §7). Each failure degrades gracefully into a partial result. It never crashes. |
| **Demo and clarity** | 20 | One screen, one input box, a live streaming tool timeline, an approval card. Runs in about 60 seconds. A 3-minute demo script is included. |

## 3. Requirements traceability (your prompt → design)

| Your requirement | Decision |
|---|---|
| Use LangGraph | LangGraph (Python) with `StateGraph`, `ToolNode`, `interrupt`, checkpointer and store, following the official LangChain/LangGraph docs |
| Next.js frontend | **Next.js (App Router) + TypeScript.** Your note said "next.js (react vite)", which is ambiguous. See open question Q1 |
| Persistent memory across the conversation | Short-term: thread state persisted by a **SQLite checkpointer**. Long-term: LangGraph **Store** (per-user and per-repo preferences, such as "ignore style nits" or "repo uses pytest"). Survives restarts |
| UI shows tool calls | Streamed through SSE (`stream_mode=["updates","messages","custom"]`) into a live "Agent activity" timeline: tool name, args, status, duration, result preview |
| One agent, one feature first | PR Review only. Agent registry and a per-agent subgraph make adding more a plug-in step |
| Prompt injection, bad input, tool failure | §7 |
| Simple, live, fast | Single `docker-less` local run. Two commands. Haiku 5.5 for speed. Parallel per-file analysis. Streaming output |
| Reference online PR agents and architecture | Patterns borrowed: PR-Agent (Qodo) for diff compression and structured findings, CodeRabbit for walkthrough plus line comments, the LangGraph "human-in-the-loop" and "supervisor/subgraph" references. We will cite these in the README |
| Human in the loop, checkpoints, threads | `interrupt()` before any write action. Resume with `Command(resume=...)`. `thread_id` per conversation. Time-travel and replay from checkpoints |
| Standard project architecture | Layered monorepo (§6) |
| Dependencies isolated from the root environment | Python `venv` in `backend/.venv`. `npm` local to `frontend/node_modules`. **No global installs.** Pinned versions |
| PRD first, then MVP after approval | This document |
| Push to your origin | Push to `webtopractice/repomate` **only after your approval, and with your confirmation at push time** |

## 4. MVP scope: PR Review agent

### User story
> As a developer, I paste a GitHub PR URL. RepoMate shows me its plan, fetches the PR, reviews every changed file, and drafts findings. I approve, edit or reject them. On approval it posts the review to GitHub. I can then ask follow-ups in the same thread ("explain finding 3", "is this thread-safe?").

### Agent flow (LangGraph)

```
START
  → guard_input        (validate URL/size, injection pre-screen, schema check)
  → plan               (LLM writes a short step plan, shown in the UI)
  → fetch_pr           (tools: PR metadata, file list, diffs)  ← retry/fallback
  → analyze_files      (parallel fan-out via Send: bug, security, perf and
                        readability checks per file; structured output)
  → synthesize         (dedupe, rank by severity, write walkthrough summary)
  → human_review       (interrupt(): approve / edit / reject findings)
  → post_review        (tool: GitHub create-review)  ← only after approval
  → remember           (write preferences and the review summary to the Store)
END
```

Follow-up chat after a review is routed by a `router` node (ReAct-style loop with tools) in the same thread, so the memory is shared.

### Tools (MVP)
| Tool | Type | Gated? |
|---|---|---|
| `get_pr_metadata(url)` | read | no |
| `list_pr_files(pr)` | read | no |
| `get_pr_diff(pr, file)` | read | no |
| `get_file_content(repo, path, ref)` (extra context) | read | no |
| `search_repo_code(repo, query)` | read | no |
| `post_pr_review(pr, comments, summary)` | **write** | **yes, requires human approval** |

Reads use the GitHub REST API with an optional fine-grained PAT. Public PRs work without a token at lower rate limits. Posting needs a PAT with PR write access. In no-token mode the "post" step falls back to "copy as markdown".

### Out of scope for MVP
Auto-fixing code, multi-repo indexing and vector search, GitHub App/webhook install, auth and multi-user accounts, other three agents.

## 5. Key design decisions

1. **Haiku 5.5 through `langchain-anthropic`** (`ChatAnthropic`). Structured output (`with_structured_output`) for findings, so malformed output can be validated and retried.
2. **Checkpointer:** `AsyncSqliteSaver` (file `data/checkpoints.db`). It is zero-setup and persistent. It is swappable for Postgres in prod, with the same interface.
3. **Store:** LangGraph `BaseStore` (SQLite-backed in dev) for cross-thread memory, namespaced `(user_id, repo)`.
4. **Streaming:** FastAPI SSE endpoint. The frontend consumes it using fetch streaming. The event types are `plan`, `tool_start`, `tool_end`, `token`, `interrupt`, `error`, `done`.
5. **Scalability:** a stateless API plus an external checkpointer means horizontal scale. Per-file fan-out bounds latency. A tool registry and agent registry give extension points.
6. **Diff budgeting:** large PRs are chunked and prioritized (skip lockfiles, generated, binaries and vendored files). The UI says what was skipped and why.

## 6. Architecture and project structure

```
repomate/
├─ backend/
│  ├─ pyproject.toml / requirements.txt (pinned)
│  ├─ .venv/                      (git-ignored, isolated)
│  └─ app/
│     ├─ main.py                  FastAPI app, CORS, lifespan (checkpointer/store)
│     ├─ api/                     routes: threads, runs (SSE), resume, health
│     ├─ core/                    config (pydantic-settings), logging, errors
│     ├─ agents/
│     │  ├─ registry.py           agent plug-in registry
│     │  └─ pr_review/            state.py, graph.py, nodes/, prompts/, schemas.py
│     ├─ tools/                   github_read.py, github_write.py, base wrappers
│     ├─ security/                sanitizer.py, injection_guard.py, redaction.py
│     ├─ memory/                  checkpointer.py, store.py
│     └─ tests/                   unit, graph tests, injection/failure suites
├─ frontend/                      Next.js + TS
│  └─ src/ app/, components/ (Chat, ToolTimeline, PlanPanel, ApprovalCard,
│            ThreadList), lib/ (sse client, api)
├─ docs/                          PRD, architecture, demo script
├─ .env.example                   (real .env is git-ignored)
└─ README.md
```

```
Browser (Next.js) ──SSE/HTTP──► FastAPI ──► LangGraph PR-Review graph
   thread list, chat,                          │  ├─ Claude Haiku 5.5
   tool timeline,                              │  ├─ GitHub tools (read / gated write)
   approval card                               │  └─ guards (input / output)
                                               ▼
                              SQLite checkpointer (threads, HITL, replay)
                              SQLite Store (long-term memory)
```

## 7. "Unbreakable": the robustness plan (25 pts)

**Prompt injection** (PR diffs, titles and comments are *untrusted data*):
- All repo content is wrapped in delimited data blocks, and the system prompt states that it is data and not instructions.
- A cheap injection screen (heuristics plus a Haiku classifier) flags "ignore previous instructions", hidden Unicode and exfiltration attempts. Flagged content is quarantined and shown in the UI as a warning, and the review continues without obeying it.
- **Least privilege.** The LLM cannot call write tools directly. Write requires a human-approved interrupt, so even a successful injection cannot post or leak.
- The tool allowlist is enforced in code. Tool arguments are validated (the URL must be a github.com PR URL, and the repo must match the PR).
- Output scrubbing redacts secrets and tokens before display or posting.

**Bad input:** a Pydantic-validated request. Malformed URLs, non-PR links, private or 404 repos, empty or huge diffs, binary files and non-English text each get a clear, friendly error or a graceful skip. There are max-size and max-file caps.

**Tool failure:** timeouts, retries with exponential backoff and jitter, GitHub rate-limit handling (read the reset header and tell the user), per-file isolation (one file failing does not fail the review), and a **partial-result** path ("reviewed 7 of 9 files, 2 failed: reason"). LLM errors and malformed structured output get a bounded retry, then a fallback. The graph has a recursion limit and a step budget to stop loops.

**Test suite:** a red-team set of injected PRs, malformed inputs and simulated tool outages, runnable in CI and shown in the demo.

## 8. Frontend (live and clear)
- **Left:** thread list (persisted, resumable). **Center:** chat plus streamed review. **Right:** *Agent activity*, a live plan checklist and a timeline of tool calls (name, args, status, ms, result preview).
- **Approval card:** findings grouped by file and severity. Each can be toggled or edited, with Approve / Reject.
- Security banner when injection was detected. Error toasts are human-readable.

## 9. Isolated environment setup (no root pollution)
- `python -m venv backend/.venv` and `pip install -r requirements.txt` run **inside** the venv only.
- `npm install` inside `frontend/` only. No global packages, no changes to system PATH.
- Config comes from `.env` files (git-ignored). `.env.example` is committed.

## 10. Milestones
1. **PRD approval (you are here)**
2. Scaffold: repo, venv, backend and frontend skeletons, config, health check
3. Graph core: guard → plan → fetch → analyze → synthesize, with checkpointer
4. HITL and posting: interrupt, resume, gated write tool
5. Frontend: streaming, tool timeline, approval card, threads
6. Memory store and follow-up chat
7. Hardening: injection and failure tests
8. Docs, demo script, and push to origin (with your confirmation)

## 11. Success criteria
- A public PR URL gives a streamed review in **under ~60 s** for a typical PR (≤ 15 files).
- Tool calls are visible live. Thread survives a server restart. Approval is required to post.
- The red-team suite passes: no instruction from the diff is followed, and no crash on any bad input.

## 12. Open questions (need your answers)
- **Q1.** Frontend: **Next.js** (my default, the App Router) or **React + Vite**? Next.js and Vite are different tools, so please confirm one.
- **Q2.** Backend language: **Python (FastAPI)** is my default, since LangGraph's Python SDK is the most complete. OK?
- **Q3.** GitHub token: will you provide a fine-grained PAT for posting reviews? Without one, posting falls back to a "copy as markdown" mode.
- **Q4.** Python version available locally, and is Node 18+ installed? I'll check and report.
- **Q5.** Should the push to `webtopractice/repomate` happen once at the end, or after each milestone? (I'll ask before any push either way.)
- **Q6.** The Anthropic API key goes in `backend/.env`. Please share it when we reach milestone 2. It will never be committed.

---
**Please reply "approved" (plus answers to Q1-Q6, or "use defaults"), or tell me what to change.**
