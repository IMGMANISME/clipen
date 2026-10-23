import type { EditAgentStatus } from './api'
import { productionIssue, productionProblem } from './productionMessages'
import { useProvider } from './providerContext'

type Props = { status: EditAgentStatus; busy: boolean; onResume: () => void; onCancel: () => void; onRefresh: () => void; onEdit: () => void; onView?: () => void }
function timeLabel(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '時間未取得' : date.toLocaleTimeString('zh-TW', { hour12: false })
}

export function ProductionStatusPanel({ status, busy, onResume, onCancel, onRefresh, onEdit, onView }: Props) {
  const provider = useProvider()
  const production = status.production
  const run = production ? status.latest_run : null
  if (!production || !run) return null
  const active = ['queued', 'running'].includes(run.state)
  const failed = ['failed', 'interrupted'].includes(run.state)
  // Timeouts and account usage limits leave a valid draft that can continue later.
  const canResume = run.state === 'failed' && ['provider_timed_out', 'provider_usage_limit'].includes(run.error_code ?? '')
  const problem = productionProblem(run.error_code, provider.name)
  const activity = production.progress?.activity ?? []
  const job = production.local_jobs.find((entry) => entry.state === 'running')
  const previewRunning = production.preview && ['queued', 'running'].includes(production.preview.state)
  const title = failed ? problem.title : run.state === 'cancelled' ? '已停止製作'
    : run.phase === 'user_edit' ? '修改已儲存' : run.state === 'succeeded' ? '影片完成了' : run.state === 'queued' ? '等待開始製作' : 'AI 正在製作'
  const current = job ? (job.kind === 'setup' ? '首次使用，正在準備語音功能。' : '正在整理對話與字幕。')
    : previewRunning ? '正在輸出影片。' : activity.at(-1)?.label ?? '正在準備製作。'
  const download = job?.kind === 'setup' && job.total_bytes && job.total_bytes > 0
    ? Math.max(0, Math.min(100, Math.round((job.completed_bytes ?? 0) / job.total_bytes * 100))) : null
  const issues = [...new Map((production.validation?.issues ?? []).map((issue) => [issue.code, issue])).values()]

  return <section className={`production-status-panel ${failed ? 'has-problem' : ''} ${active ? 'is-active' : ''}`} aria-labelledby="production-status-heading">
    <div className="production-status-heading" role="status" aria-live="polite"><h3 id="production-status-heading">{title}</h3></div>
    <p>{failed ? problem.next : active ? current : run.state === 'cancelled' ? '草稿已保留，可以先看看目前的結果。' : '播放下方影片，看看是否符合你的期待。'}</p>
    {active && download !== null && <div className="production-job"><progress aria-label="準備語音功能" value={download} max={100} /><span>語音功能準備進度 {download}%</span></div>}
    {canResume && <p>會沿用原本約 {Number(production.intent.target_duration_seconds ?? 60) / 60} 分鐘的剪輯與風格設定，並接續使用 {provider.name} 額度。</p>}
    <div className="production-recovery-actions">
      {failed && (production.preview?.state === 'succeeded' || production.last_successful_preview?.state === 'succeeded') && <a className="button secondary" href="#production-output" onClick={onView}>查看影片</a>}
      {canResume && <button className="button primary" type="button" disabled={busy} onClick={onResume}>繼續這份草稿</button>}
      {failed && <button className="button secondary" type="button" disabled={busy} onClick={onEdit}>重新製作</button>}
      {active && <button className="button ghost" type="button" disabled={busy} onClick={onCancel}>停止製作</button>}
    </div>
    <details className="production-more"><summary>查看製作詳情</summary>
      {failed && <p>{problem.reason} {canResume ? `繼續會沿用已選素材、剪輯與風格，並接續使用 ${provider.name} 額度。` : '草稿已保留。重新製作會建立新工作，不會接續這份草稿。'}</p>}
      <button className="button ghost" type="button" onClick={onRefresh}>重新整理狀態</button>
      {issues.length > 0 && <div className="production-checks"><strong>{active ? 'AI 還在處理的項目' : '尚待完成的項目'}</strong>
        <ul>{issues.map((issue) => <li key={issue.code}>{productionIssue(issue.code)}</li>)}</ul>
      </div>}
      {activity.length > 0 && <div className="production-activity"><strong>最近製作紀錄</strong>
        <ol>{activity.map((entry) => <li key={entry.sequence}><time dateTime={entry.created_at}>{timeLabel(entry.created_at)}</time><span>{entry.label}</span></li>)}</ol>
        <p>紀錄代表曾執行的操作，結果以草稿與預覽為準。</p>
      </div>}
      <details className="production-diagnostics"><summary>診斷資料</summary>
        <pre>{JSON.stringify({ run_id: run.run_id, state: run.state, phase: run.phase, error_code: run.error_code,
          tool_calls: run.budget.tool_calls, max_tool_calls: run.budget.max_tool_calls,
          preview_error: production.preview?.error_code ?? null, local_jobs: production.local_jobs,
          validation: production.validation, recent_tools: activity.filter((entry) => entry.tool).map((entry) => ({ tool: entry.tool, at: entry.created_at })) }, null, 2)}</pre>
      </details>
    </details>
  </section>
}
