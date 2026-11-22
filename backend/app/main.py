"""FastAPI app: threads, SSE runs, HITL resume. Stateless process; all state in the checkpointer/store."""
import asyncio
import json
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore
from langgraph.types import Command
from pydantic import BaseModel, Field

from .agents.pr_review.graph import build_graph
from .agents.pr_review.llm import text_of
from .core.config import DATA_DIR, settings
from .security.guard import redact_secrets, validate_user_message

RUN_TIMEOUT_S = 240


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(DATA_DIR / "checkpoints.db")))
        store = await stack.enter_async_context(AsyncSqliteStore.from_conn_string(str(DATA_DIR / "store.db")))
        await store.setup()
        db = await aiosqlite.connect(DATA_DIR / "threads.db")
        stack.push_async_callback(db.close)
        await db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, title TEXT, updated REAL)")
        await db.commit()
        app.state.graph = build_graph(saver, store)
        app.state.db = db
        yield


app = FastAPI(title="RepoMate", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


def cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}


class RunBody(BaseModel):
    message: str


class ResumeBody(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    approved_ids: list[str] = []
    edits: dict[str, str] = {}


@app.get("/api/health")
async def health():
    return {"ok": True, "model": settings.model_name, "has_anthropic_key": bool(settings.anthropic_api_key),
            "has_github_token": bool(settings.github_token)}


@app.post("/api/threads")
async def create_thread():
    tid = uuid.uuid4().hex
    await app.state.db.execute("INSERT INTO threads VALUES (?,?,?)", (tid, "New review", time.time()))
    await app.state.db.commit()
    return {"id": tid, "title": "New review"}


@app.get("/api/threads")
async def list_threads():
    cur = await app.state.db.execute("SELECT id,title,updated FROM threads ORDER BY updated DESC LIMIT 50")
    return [{"id": r[0], "title": r[1], "updated": r[2]} for r in await cur.fetchall()]


@app.delete("/api/threads/{tid}")
async def delete_thread(tid: str):
    await app.state.db.execute("DELETE FROM threads WHERE id=?", (tid,))
    await app.state.db.commit()
    try:
        await app.state.graph.checkpointer.adelete_thread(tid)
    except Exception:
        pass
    return {"ok": True}


async def _pending(snapshot):
    for t in snapshot.tasks:
        for i in t.interrupts:
            return i.value
    return None


@app.get("/api/threads/{tid}")
async def get_thread(tid: str):
    snap = await app.state.graph.aget_state(cfg(tid))
    v = snap.values or {}
    msgs = []
    for m in v.get("messages", []):
        if isinstance(m, HumanMessage):
            msgs.append({"role": "user", "content": text_of(m)})
        elif isinstance(m, AIMessage) and text_of(m).strip():
            msgs.append({"role": "assistant", "content": text_of(m)})
    return {"id": tid, "messages": msgs, "plan": v.get("plan", []), "activity": v.get("activity", []),
            "warnings": v.get("warnings", []), "skipped": v.get("skipped", []),
            "pending": await _pending(snap), "posted": v.get("posted")}


async def _touch(tid: str, title: str | None = None):
    if title:
        await app.state.db.execute("UPDATE threads SET title=?, updated=? WHERE id=? AND title='New review'",
                                   (title[:60], time.time(), tid))
    await app.state.db.execute("UPDATE threads SET updated=? WHERE id=?", (time.time(), tid))
    await app.state.db.commit()


def sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, default=str)}\n\n"


def friendly_error(e: Exception) -> str:
    name = type(e).__name__
    s = str(e)
    if "authentication" in s.lower() or "api_key" in s.lower() or "401" in s or name == "AuthenticationError":
        return "The model API key is missing or invalid. Set ANTHROPIC_API_KEY in backend/.env and restart."
    if "rate" in s.lower() and "limit" in s.lower():
        return "The model is rate limited right now. Please retry in a moment."
    if isinstance(e, asyncio.TimeoutError):
        return "The run took too long and was stopped. Partial results are saved in this thread."
    return f"Something went wrong ({name}). Your thread is saved; you can retry."


async def event_stream(tid: str, graph_input):
    graph = app.state.graph
    try:
        async with asyncio.timeout(RUN_TIMEOUT_S):
            async for mode, chunk in graph.astream(graph_input, cfg(tid),
                                                   stream_mode=["updates", "messages", "custom"]):
                if mode == "custom":
                    yield sse(chunk)
                elif mode == "messages":
                    msg, meta = chunk
                    if meta.get("langgraph_node") == "chat" and isinstance(msg, AIMessageChunk):
                        t = text_of(msg)
                        if t:
                            yield sse({"type": "token", "text": redact_secrets(t)})
                elif mode == "updates":
                    if "__interrupt__" in chunk:
                        iv = chunk["__interrupt__"]
                        iv = iv[0] if isinstance(iv, (list, tuple)) else iv
                        yield sse({"type": "interrupt", "value": iv.value})
                    else:
                        for node in chunk:
                            yield sse({"type": "node", "node": node})
    except Exception as e:  # never crash the stream
        yield sse({"type": "error", "message": friendly_error(e)})
    yield sse({"type": "done"})


HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.post("/api/threads/{tid}/run")
async def run(tid: str, body: RunBody):
    text, err = validate_user_message(body.message)
    snap = await app.state.graph.aget_state(cfg(tid))
    if err or await _pending(snap):
        msg = err or "Please approve or reject the pending review first."

        async def bad():
            yield sse({"type": "error", "message": msg})
            yield sse({"type": "done"})
        return StreamingResponse(bad(), media_type="text/event-stream", headers=HEADERS)
    await _touch(tid, text)
    return StreamingResponse(event_stream(tid, {"messages": [HumanMessage(text)]}),
                             media_type="text/event-stream", headers=HEADERS)


@app.post("/api/threads/{tid}/resume")
async def resume(tid: str, body: ResumeBody):
    snap = await app.state.graph.aget_state(cfg(tid))
    if not await _pending(snap):
        raise HTTPException(409, "Nothing is waiting for approval in this thread.")
    await _touch(tid)
    return StreamingResponse(event_stream(tid, Command(resume=body.model_dump())),
                             media_type="text/event-stream", headers=HEADERS)
