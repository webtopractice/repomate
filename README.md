# RepoMate

An agent for developers: paste a GitHub PR URL and RepoMate plans, reads the diff, finds issues, and asks you to approve before it posts anything. Built with LangGraph (Python/FastAPI) and React + Vite. Model: `claude-haiku-5-5`.

See [docs/PRD.md](docs/PRD.md) for the design.

## Run

Backend (isolated venv, nothing global):
```
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy ..\.env.example .env      # then set ANTHROPIC_API_KEY (and optionally GITHUB_TOKEN)
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```
Frontend:
```
cd frontend
npm install
npm run dev     # http://localhost:5173 (proxies /api to :8000)
```
Tests (offline, fake model and GitHub): `cd backend && .venv\Scripts\python -m pytest`

## How it works
- Graph: guard_input, fetch_pr, plan, parallel analyze_file, synthesize, human_review (interrupt), post_review, remember. Follow-up questions go through a tool-using chat node in the same thread.
- Memory: SQLite checkpointer (threads, resume after restart) plus LangGraph Store (preferences and past reviews).
- Human in the loop: nothing is posted until you approve. Without a write-capable GitHub token, approval returns the review as markdown.
- Robustness: PR content is wrapped as untrusted data, injection patterns are flagged, secrets are redacted, tools retry with backoff, and any failure degrades to a partial result.
