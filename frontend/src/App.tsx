import { useCallback, useEffect, useRef, useState } from 'react'
import { api, stream } from './api'
import type { Activity, Approval, Msg, StreamEvent, Thread } from './api'
import ApprovalCard from './components/ApprovalCard'
import ActivityPanel from './components/ActivityPanel'

const EXAMPLE = 'https://github.com/pallets/flask/pull/6168'

export default function App() {
  const [threads, setThreads] = useState<Thread[]>([])
  const [tid, setTid] = useState<string | null>(null)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [live, setLive] = useState('')
  const [activity, setActivity] = useState<Activity[]>([])
  const [plan, setPlan] = useState<string[]>([])
  const [notes, setNotes] = useState<string[]>([])
  const [pending, setPending] = useState<Approval | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [input, setInput] = useState('')
  const [health, setHealth] = useState<{ model: string; has_anthropic_key: boolean } | null>(null)
  const bottom = useRef<HTMLDivElement>(null)

  const refreshThreads = useCallback(() => api.threads().then(setThreads).catch(() => {}), [])

  const load = useCallback(async (id: string) => {
    const d = await api.thread(id)
    setMsgs(d.messages); setActivity(d.activity); setPlan(d.plan); setPending(d.pending)
    setNotes(d.warnings)
  }, [])

  useEffect(() => {
    refreshThreads()
    api.health().then(setHealth).catch(() => setError('Backend not reachable on :8000'))
  }, [refreshThreads])
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs, live, pending])

  const select = async (id: string) => {
    setTid(id); setError(''); setLive('')
    await load(id).catch(() => setError('Could not load thread'))
  }
  const create = async () => {
    const t = await api.newThread(); await refreshThreads()
    setTid(t.id); setMsgs([]); setActivity([]); setPlan([]); setPending(null); setNotes([]); setError('')
    return t.id
  }
  const remove = async (id: string) => {
    await api.deleteThread(id); if (id === tid) { setTid(null); setMsgs([]) } refreshThreads()
  }

  const onEvent = (e: StreamEvent) => {
    switch (e.type) {
      case 'plan': setPlan(e.steps); break
      case 'tool_start': setActivity(a => [...a, { id: e.id, name: e.name, args: e.args, status: 'running' }]); break
      case 'tool_end': setActivity(a => a.map(x => (x.id === e.id ? { ...x, ...e } : x))); break
      case 'memory': setActivity(a => [...a, { id: 'm' + Math.random(), name: 'memory', args: {}, status: 'ok', preview: e.text }]); break
      case 'token': setLive(t => t + e.text); break
      case 'interrupt': setPending(e.value); break
      case 'error': setError(e.message); break
    }
  }

  const finish = async (id: string) => {
    setLive(''); await load(id).catch(() => {}); setBusy(false); refreshThreads()
  }

  const send = async (text: string) => {
    if (!text.trim() || busy) return
    const id = tid ?? (await create())
    setInput(''); setBusy(true); setError(''); setLive('')
    setMsgs(m => [...m, { role: 'user', content: text }])
    if (/github\.com\/.+\/pull\/\d+/.test(text)) { setActivity([]); setPlan([]); setPending(null) }
    await stream(`/api/threads/${id}/run`, { message: text }, onEvent)
    await finish(id)
  }

  const decide = async (body: { action: string; approved_ids: string[]; edits: Record<string, string> }) => {
    if (!tid) return
    setBusy(true); setPending(null); setError('')
    await stream(`/api/threads/${tid}/resume`, body, onEvent)
    await finish(tid)
  }

  return (
    <div className="app">
      <aside className="threads">
        <h1>RepoMate</h1>
        <button className="primary" onClick={create}>+ New review</button>
        <ul>
          {threads.map(t => (
            <li key={t.id} className={t.id === tid ? 'active' : ''} onClick={() => select(t.id)}>
              <span>{t.title}</span>
              <button className="x" onClick={ev => { ev.stopPropagation(); remove(t.id) }}>×</button>
            </li>
          ))}
        </ul>
        <div className="foot">
          {health ? `${health.model}${health.has_anthropic_key ? '' : ' (no API key!)'}` : 'offline'}
        </div>
      </aside>

      <main className="chat">
        <div className="messages">
          {msgs.length === 0 && !busy && (
            <div className="empty">
              <h2>Review any GitHub pull request</h2>
              <p>Paste a PR URL. I will plan, read the diff, find issues, and ask you before posting anything.</p>
              <button onClick={() => send(EXAMPLE)}>Try an example PR</button>
            </div>
          )}
          {msgs.map((m, i) => <div key={i} className={`msg ${m.role}`}>{m.content}</div>)}
          {live && <div className="msg assistant">{live}</div>}
          {busy && !live && <div className="msg assistant typing">Working…</div>}
          {pending && <ApprovalCard a={pending} onDecide={decide} disabled={busy} />}
          {error && <div className="error">{error}</div>}
          <div ref={bottom} />
        </div>
        <form className="composer" onSubmit={e => { e.preventDefault(); send(input) }}>
          <input value={input} onChange={e => setInput(e.target.value)} disabled={busy || !!pending}
            placeholder={pending ? 'Approve or reject the review above first' : 'Paste a PR URL or ask a follow-up…'} />
          <button className="primary" disabled={busy || !!pending || !input.trim()}>Send</button>
        </form>
      </main>

      <ActivityPanel plan={plan} activity={activity} notes={notes} busy={busy} />
    </div>
  )
}
