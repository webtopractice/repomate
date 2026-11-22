import { useState } from 'react'
import type { Approval } from '../api'

export default function ApprovalCard({ a, onDecide, disabled }: {
  a: Approval; disabled: boolean
  onDecide: (b: { action: string; approved_ids: string[]; edits: Record<string, string> }) => void
}) {
  const [sel, setSel] = useState<Set<string>>(new Set(a.findings.map(f => f.id)))
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [editing, setEditing] = useState<string | null>(null)

  const toggle = (id: string) => setSel(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })

  return (
    <div className="approval">
      <div className="head">
        <strong>Review ready: your approval needed</strong>
        <span className={`verdict ${a.verdict}`}>{a.verdict?.replace('_', ' ')}</span>
      </div>
      <p>{a.summary}</p>
      {a.skipped.length > 0 && (
        <details><summary>{a.skipped.length} file(s) skipped</summary>
          <ul>{a.skipped.map(s => <li key={s.file}><code>{s.file}</code>: {s.reason}</li>)}</ul></details>
      )}
      {a.findings.length === 0 && <p className="muted">No issues found.</p>}
      {a.findings.map(f => (
        <div key={f.id} className={`finding ${sel.has(f.id) ? '' : 'off'}`}>
          <label>
            <input type="checkbox" checked={sel.has(f.id)} onChange={() => toggle(f.id)} />
            <span className={`sev ${f.severity}`}>{f.severity}</span>
            <strong>{f.title}</strong>
            <code>{f.file}{f.line ? `:${f.line}` : ''}</code>
          </label>
          {editing === f.id ? (
            <textarea value={edits[f.id] ?? f.comment} onChange={e => setEdits({ ...edits, [f.id]: e.target.value })}
              onBlur={() => setEditing(null)} autoFocus />
          ) : (
            <p onDoubleClick={() => setEditing(f.id)} title="Double-click to edit">{edits[f.id] ?? f.comment}</p>
          )}
          {f.suggestion && <pre>{f.suggestion}</pre>}
        </div>
      ))}
      {!a.can_post && <p className="muted">No GitHub write token configured: approving will give you the review as markdown to copy.</p>}
      <div className="actions">
        <button className="primary" disabled={disabled}
          onClick={() => onDecide({ action: 'approve', approved_ids: [...sel], edits })}>
          Approve {sel.size} &amp; post
        </button>
        <button disabled={disabled} onClick={() => onDecide({ action: 'reject', approved_ids: [], edits: {} })}>
          Reject
        </button>
      </div>
    </div>
  )
}
