import { useState } from 'react'
import type { EditAgentStatus } from './api'
import { productionIssue, productionProblem } from './productionMessages'
import { useProvider } from './providerContext'

type Props = { status: EditAgentStatus; busy: boolean; onResume: () => void; onCancel: () => void; onRefresh: () => void; onEdit: () => void; onView?: () => void; onRefine?: () => void }
function timeLabel(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-TW', { hour12: false, hour: '2-digit', minute: '2-digit' })
}

export function ProductionStatusPanel({ status, busy, onResume, onCancel, onRefresh, onEdit, onView, onRefine }: Props) {
  const provider = useProvider()
  const [copied, setCopied] = useState(false)
  const production = status.production
  const run = production ? status.latest_run : null
  if (!production || !run) return null
  const active = ['queued', 'running'].includes(run.state)
  const failed = ['failed', 'interrupted'].includes(run.state)
  // Timeouts, account usage limits and early stops leave a valid draft that can continue later.
  const canResume = run.state === 'failed' && ['provider_timed_out', 'provider_usage_limit', 'provider_stopped_early'].includes(run.error_code ?? '')
  // A draft held back by delivery checks, or an AI that stopped to ask for something, is fixed through a revision request.
  const blocked = run.state === 'failed' && run.error_code === 'delivery_blocked'
  const canRefine = run.state === 'failed' && ['delivery_blocked', 'provider_stopped_early'].includes(run.error_code ?? '')
  const problem = productionProblem(run.error_code, provider.name)
  const progress = production.progress
  const activity = progress?.activity ?? []
  const message = progress?.ai_message?.trim() || null
  const job = production.local_jobs.find((entry) => entry.state === 'running')
  const previewRunning = production.preview && ['queued', 'running'].includes(production.preview.state)
  const title = failed ? problem.title : run.state === 'cancelled' ? '已停止製作'
    : run.phase === 'user_edit' ? '修改已儲存' : run.state === 'succeeded' ? '影片完成了' : run.state === 'queued' ? '等待開始製作' : 'AI 正在製作'
  const current = job ? (job.kind === 'setup' ? '首次使用，正在準備語音功能。' : '正在整理對話與字幕。')
    : previewRunning ? '正在輸出影片。' : activity.at(-1)?.label ?? '正在準備製作。'
  const download = job?.kind === 'setup' && job.total_bytes && job.total_bytes > 0
    ? Math.max(0, Math.min(100, Math.round((job.completed_bytes ?? 0) / job.total_bytes * 100))) : null
  const issues = [...new Map((production.validation?.issues ?? []).map((issue) => [issue.code, issue])).values()]
  // When the AI stopped on its own, its last message is the clearest explanation; show it up front.
  const messageUpFront = failed && message !== null
  const diagnostics = JSON.stringify({ run_id: run.run_id, state: run.state, phase: run.phase, error_code: run.error_code,
    tool_calls: run.budget.tool_calls, max_tool_calls: run.budget.max_tool_calls,
    preview_error: production.preview?.error_code ?? null, local_jobs: production.local_jobs,
    validation: production.validation, recent_tools: activity.filter((entry) => entry.tool).map((entry) => ({ tool: entry.tool, at: entry.created_at })) }, null, 2)
  async function copyDiagnostics() {
    try { await navigator.clipboard.writeText(diagnostics); setCopied(true) } catch { setCopied(false) }
  }

  return <section className={`production-status-panel ${failed ? 'has-problem' : ''} ${active ? 'is-active' : ''}`} aria-labelledby="production-status-heading">
    <div className="production-status-heading" role="status" aria-live="polite"><h3 id="production-status-heading">{title}</h3></div>
    <p>{failed ? problem.next : active ? current : run.state === 'cancelled' ? '草稿已保留，可以先看看目前的結果。' : '播放下方影片，看看是否符合你的期待。'}</p>
    {active && download !== null && <div className="production-job"><progress aria-label="準備語音功能" value={download} max={100} /><span>語音功能準備進度 {download}%</span></div>}
    {messageUpFront && <blockquote className="production-ai-message"><strong>AI 的說明</strong><p>{message}</p></blockquote>}
    {blocked && issues.length > 0 && <ul className="production-blockers">{issues.map((issue) => <li key={issue.code}>{productionIssue(issue.code)}</li>)}</ul>}
    <div className="production-recovery-actions">
      {failed && (production.preview?.state === 'succeeded' || production.last_successful_preview?.state === 'succeeded') && <a className="button secondary" href="#production-output" onClick={onView}>查看影片</a>}
      {canRefine && onRefine && <button className="button primary" type="button" disabled={busy} onClick={onRefine}>用 AI 打磨修正</button>}
      {canResume && <button className={`button ${canRefine && onRefine ? 'secondary' : 'primary'}`} type="button" disabled={busy} onClick={onResume}>繼續這份草稿</button>}
      {failed && <button className="button secondary" type="button" disabled={busy} onClick={onEdit}>重新製作</button>}
      {active && <button className="button ghost" type="button" disabled={busy} onClick={onCancel}>停止製作</button>}
    </div>
    <details className="production-more"><summary>查看製作詳情</summary>
      {progress?.request && <div className="production-detail"><strong>你這次的要求</strong><p>{progress.request}</p></div>}
      {failed && <div className="production-detail"><strong>發生了什麼</strong>
        <p>{problem.reason}</p>
        <p>你的草稿都還在，沒有被刪掉。{canResume ? '「繼續這份草稿」會接著目前的進度做，' : ''}「重新製作」會另外做一支新影片。</p>
      </div>}
      {message && !messageUpFront && <div className="production-detail"><strong>AI 的說明</strong><p className="production-ai-text">{message}</p></div>}
      {issues.length > 0 && !blocked && <div className="production-checks"><strong>{active ? 'AI 還在處理的事' : '還需要處理的事'}</strong>
        <ul>{issues.map((issue) => <li key={issue.code}>{productionIssue(issue.code)}</li>)}</ul>
      </div>}
      {activity.length > 0 && <div className="production-activity"><strong>AI 做了哪些事</strong>
        <ol>{activity.map((entry) => <li key={entry.sequence}><time dateTime={entry.created_at}>{timeLabel(entry.created_at)}</time><span>{entry.label}</span></li>)}</ol>
        <p>這是製作過程的步驟，實際結果請以影片為準。</p>
      </div>}
      <div className="production-detail-footer">
        <button className="button ghost" type="button" onClick={onRefresh}>重新整理</button>
        <details className="production-diagnostics"><summary>回報問題用的技術資料</summary>
          <p>一般不需要看這裡。向我們回報問題時，可以複製這段資料一起附上，裡面不含你的影片或聲音檔案。</p>
          <button className="button ghost" type="button" onClick={() => void copyDiagnostics()}>{copied ? '已複製' : '複製技術資料'}</button>
          <pre>{diagnostics}</pre>
        </details>
      </div>
    </details>
  </section>
}
