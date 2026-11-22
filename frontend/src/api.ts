export type Finding = {
  id: string; file: string; line: number | null; severity: string; category: string
  title: string; comment: string; suggestion?: string | null
}
export type Approval = {
  type: 'approval'; summary: string; verdict: string; findings: Finding[]; warnings: string[]
  skipped: { file: string; reason: string }[]; can_post: boolean
  pr: { title: string; html_url: string; author: string }
}
export type Activity = {
  id: string; name: string; args: Record<string, unknown>; status: 'running' | 'ok' | 'error'
  ms?: number; preview?: string
}
export type Msg = { role: 'user' | 'assistant'; content: string }
export type Thread = { id: string; title: string; updated: number }
export type ThreadDetail = {
  id: string; messages: Msg[]; plan: string[]; activity: Activity[]; warnings: string[]
  skipped: { file: string; reason: string }[]; pending: Approval | null
}
export type StreamEvent =
  | { type: 'plan'; steps: string[]; targets: string[]; skipped: { file: string; reason: string }[] }
  | ({ type: 'tool_start' } & Omit<Activity, 'status'>)
  | ({ type: 'tool_end' } & Activity)
  | { type: 'memory'; text: string }
  | { type: 'token'; text: string }
  | { type: 'node'; node: string }
  | { type: 'interrupt'; value: Approval }
  | { type: 'error'; message: string }
  | { type: 'done' }

const j = async <T,>(r: Response): Promise<T> => {
  if (!r.ok) throw new Error(`Request failed (${r.status})`)
  return r.json()
}

export const api = {
  health: () => fetch('/api/health').then(j<{ model: string; has_anthropic_key: boolean; has_github_token: boolean }>),
  threads: () => fetch('/api/threads').then(j<Thread[]>),
  newThread: () => fetch('/api/threads', { method: 'POST' }).then(j<Thread>),
  deleteThread: (id: string) => fetch(`/api/threads/${id}`, { method: 'DELETE' }),
  thread: (id: string) => fetch(`/api/threads/${id}`).then(j<ThreadDetail>),
}

/** POST + read an SSE stream, invoking onEvent for each event. Never throws; reports errors as events. */
export async function stream(url: string, body: unknown, onEvent: (e: StreamEvent) => void, signal?: AbortSignal) {
  try {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
    })
    if (!r.ok || !r.body) throw new Error(`Server error (${r.status})`)
    const reader = r.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      let i
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i)
        buf = buf.slice(i + 2)
        if (chunk.startsWith('data: ')) {
          try { onEvent(JSON.parse(chunk.slice(6))) } catch { /* ignore malformed event */ }
        }
      }
    }
  } catch (e) {
    if ((e as Error).name !== 'AbortError')
      onEvent({ type: 'error', message: `Connection problem: ${(e as Error).message}. Is the backend running?` })
  }
  onEvent({ type: 'done' })
}
