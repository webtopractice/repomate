import type { Activity } from '../api'

const ICON = { running: '⏳', ok: '✓', error: '✗' } as const

export default function ActivityPanel({ plan, activity, notes, busy }:
  { plan: string[]; activity: Activity[]; notes: string[]; busy: boolean }) {
  return (
    <aside className="activity">
      <h3>Agent activity {busy && <span className="pulse" />}</h3>
      {notes.length > 0 && (
        <div className="warn">
          <strong>Security / warnings</strong>
          <ul>{notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </div>
      )}
      {plan.length > 0 && (
        <>
          <h4>Plan</h4>
          <ol className="plan">{plan.map((s, i) => <li key={i}>{s}</li>)}</ol>
        </>
      )}
      <h4>Tool calls</h4>
      {activity.length === 0 && <p className="muted">Nothing yet.</p>}
      <ul className="tools">
        {activity.map(a => (
          <li key={a.id} className={a.status}>
            <div className="row">
              <span className="ico">{ICON[a.status]}</span>
              <code>{a.name}</code>
              {a.ms !== undefined && <span className="ms">{a.ms} ms</span>}
            </div>
            {Object.keys(a.args).length > 0 && <div className="args">{JSON.stringify(a.args)}</div>}
            {a.preview && <div className="preview">{a.preview}</div>}
          </li>
        ))}
      </ul>
    </aside>
  )
}
