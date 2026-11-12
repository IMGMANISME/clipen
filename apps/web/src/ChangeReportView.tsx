import type { ChangeReport } from './api'

/** The account the AI gave of one change: what it is now, what changed and why, each note answered, and what it did not do. */
export function ChangeReportView({ report, request }: { report: ChangeReport; request?: string | null }) {
  const empty = !report.changes.length && !report.notes.length && !report.not_done.length
  return <div className="rv-report-body">
    {request && <p className="rv-report-request"><span>你的要求</span>{request}</p>}
    {report.summary && <p className="rv-report-summary">{report.summary}</p>}
    {report.changes.length > 0 && <section><h5>改了什麼</h5><ol className="rv-report-changes">
      {report.changes.map((change, index) => <li key={index}><strong>{change.what}</strong><span>{change.why}</span></li>)}</ol></section>}
    {report.notes.length > 0 && <section><h5>你的注記</h5><ul className="rv-report-notes">
      {report.notes.map((note) => <li key={note.reference_id} className={`is-${note.outcome}`}>
        <span className="rv-report-outcome">{note.outcome_label}</span>
        <div>{note.note && <q>{note.note}</q>}<p>{note.explanation}</p>
          {note.reason_label && <small className="rv-report-reason">{note.reason_label}</small>}</div></li>)}</ul></section>}
    {report.not_done.length > 0 && <section className="rv-report-missing"><h5>沒做到的地方</h5><ul>
      {report.not_done.map((item, index) => <li key={index} className={item.reason ? `is-${item.reason}` : undefined}>
        {item.reason_label && <span className="rv-report-reason">{item.reason_label}</span>}
        <strong>{item.what}</strong>{item.detail && <span>{item.detail}</span>}</li>)}</ul>
      {report.not_done.some((item) => item.reason === 'needs_you') && <p className="rv-hint">回覆「需要你決定或提供」的項目後，下一次修改 AI 會接著做。</p>}</section>}
    {empty && <p className="rv-hint">這一版是在 Clipen 要求修改說明之前做的，只有上面這句摘要。</p>}
  </div>
}
