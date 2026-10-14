import { useEffect, useState } from 'react'
import {
  acceptTimelineWarnings,
  buildEditTimeline,
  getEditTimeline,
  getPlanning,
} from './api'
import type { EditTimelineState, EditablePlan } from './api'

type Props = { projectId: string; csrf: string }
type OutputPreset = 'landscape_1080p' | 'portrait_1080p'
type FrameRate = 24 | 25 | 30 | 60

const stateCopy = {
  not_ready: ['3', '建立第一個剪輯版本', '選擇輸出畫面後，Clipen 會把已確認的計畫轉成精確且可重現的時間軸。'],
  stale: ['↻', '剪輯計畫已更新', '目前時間軸來自舊版計畫，請重新建立後再繼續。'],
  validation_required: ['!', '需要重新檢查', '時間軸已存在，但找不到與它完全一致的安全檢查。'],
  ready: ['✓', '可以開始剪輯', '目前剪輯版本已通過來源、內容與輸出能力檢查。'],
  needs_confirmation: ['!', '開始前需要你的確認', '這些項目不會阻止剪輯，但可能影響畫面或匯出結果。'],
  output_pending: ['✓', '剪輯版本已保存', '時間軸已建立完成。目前版本還不會產生影片檔或 DaVinci Resolve 工程。'],
  blocked: ['×', '這份剪輯版本還不能使用', '請查看下方問題，修正後重新建立剪輯版本。'],
} as const

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : 'Clipen 無法讀取剪輯版本。'
}

export function TimelineValidationSection({ projectId, csrf }: Props) {
  const [status, setStatus] = useState<EditTimelineState | null>(null)
  const [plan, setPlan] = useState<EditablePlan | null>(null)
  const [preset, setPreset] = useState<OutputPreset>('landscape_1080p')
  const [frameRate, setFrameRate] = useState<FrameRate>(30)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function applyStatus(timeline: EditTimelineState) {
    setStatus(timeline)
    if (timeline.timeline) {
      setPreset(timeline.timeline.output.preset)
      setFrameRate(timeline.timeline.output.frame_rate)
    }
  }

  async function refresh() {
    const [timeline, planning] = await Promise.all([
      getEditTimeline(projectId),
      getPlanning(projectId),
    ])
    applyStatus(timeline)
    setPlan(planning.current_plan?.status === 'confirmed' ? planning.current_plan : null)
  }

  useEffect(() => {
    let mounted = true
    Promise.all([getEditTimeline(projectId), getPlanning(projectId)])
      .then(([timeline, planning]) => {
        if (!mounted) return
        applyStatus(timeline)
        setPlan(planning.current_plan?.status === 'confirmed' ? planning.current_plan : null)
      })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
    return () => { mounted = false }
  }, [projectId])

  async function build() {
    if (!plan || busy) return
    setBusy(true)
    setError(null)
    try {
      applyStatus(await buildEditTimeline(csrf, projectId, plan, preset, frameRate))
    } catch (caught) {
      setError(errorMessage(caught))
      try { await refresh() } catch { /* Preserve the command error. */ }
    } finally {
      setBusy(false)
    }
  }

  async function acceptWarnings() {
    const report = status?.validation?.report
    if (!report || busy) return
    setBusy(true)
    setError(null)
    try {
      await acceptTimelineWarnings(csrf, projectId, report.report_id)
      await refresh()
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  const currentState = status?.state ?? 'not_ready'
  const issues = status?.validation?.report?.issues ?? []
  const outputPendingOnly = currentState === 'blocked'
    && issues.length > 0
    && issues.every((issue) => issue.rule_id === 'timeline.capability.element_unverified')
  const displayState = outputPendingOnly ? 'output_pending' : currentState
  const copy = stateCopy[displayState]
  const visibleSourceIssues = outputPendingOnly ? [] : issues
  const groupedIssues = Array.from(visibleSourceIssues.reduce((groups, issue) => {
    const key = `${issue.severity}:${issue.rule_id}:${issue.message}:${issue.suggestion}`
    const existing = groups.get(key)
    groups.set(key, existing ? { issue: existing.issue, count: existing.count + 1 } : { issue, count: 1 })
    return groups
  }, new Map<string, { issue: (typeof issues)[number]; count: number }>()).values())
  const visibleIssues = groupedIssues.slice(0, 20)
  const canBuild = plan !== null

  return (
    <section className="timeline-validation-section" aria-labelledby="timeline-validation-title">
      <div className="analysis-heading"><div>
        <p className="eyebrow">04 · 剪輯</p>
        <h2 id="timeline-validation-title">建立剪輯版本</h2>
        <p>Clipen 會把你確認的計畫固定成精確時間軸；毛片只會被引用，不會被改動。</p>
      </div></div>

      {!status && !error && <div className="validation-card" role="status"><span className="small-spinner" aria-hidden="true" /><div><strong>正在讀取剪輯版本</strong><p>這次查詢不會讀取或修改毛片。</p></div></div>}
      {error && <div className="inline-message error" role="alert">{error}</div>}

      {status && <>
        {canBuild && <div className="timeline-builder">
          <div><strong>輸出畫面</strong><p>這只決定時間軸尺寸與格率，之後仍可重新建立。</p></div>
          <label>畫面比例<select value={preset} onChange={(event) => setPreset(event.target.value as OutputPreset)}><option value="landscape_1080p">橫式 16:9 · 1920 × 1080</option><option value="portrait_1080p">直式 9:16 · 1080 × 1920</option></select></label>
          <label>格率<select value={frameRate} onChange={(event) => setFrameRate(Number(event.target.value) as FrameRate)}>{[24, 25, 30, 60].map((rate) => <option key={rate} value={rate}>{rate} fps</option>)}</select></label>
          <button className="button primary" disabled={busy || !plan} onClick={() => void build()}>{busy ? '正在建立…' : status.timeline ? '重新建立剪輯版本' : '建立剪輯版本'}</button>
        </div>}

        {status.timeline && <dl className="timeline-summary">
          <div><dt>畫面</dt><dd>{status.timeline.output.width} × {status.timeline.output.height}</dd></div>
          <div><dt>格率</dt><dd>{status.timeline.output.frame_rate} fps</dd></div>
          <div><dt>長度</dt><dd>{status.timeline.duration_seconds.toLocaleString()} 秒</dd></div>
          <div><dt>片段</dt><dd>{status.timeline.tracks.reduce((sum, track) => sum + track.item_count, 0).toLocaleString()} 個</dd></div>
        </dl>}

        <div className={`validation-card state-${displayState}`} role={displayState === 'blocked' ? 'alert' : 'status'}>
          <span className="validation-symbol" aria-hidden="true">{copy[0]}</span>
          <div className="validation-content"><strong>{copy[1]}</strong><p>{copy[2]}</p>
            {visibleIssues.length > 0 && <ul className="validation-issues">{visibleIssues.map(({ issue, count }) => <li key={`${issue.severity}:${issue.rule_id}`}><strong>{issue.message}{count > 1 ? `（${count.toLocaleString()} 個片段）` : ''}</strong><span>{issue.suggestion}</span></li>)}</ul>}
            {groupedIssues.length > visibleIssues.length && <p className="validation-overflow">另有 {(groupedIssues.length - visibleIssues.length).toLocaleString()} 類問題。</p>}
          </div>
          {currentState === 'needs_confirmation' && <button className="button primary" disabled={busy} onClick={() => void acceptWarnings()}>{busy ? '正在保存確認…' : `接受這 ${issues.length.toLocaleString()} 項警告`}</button>}
        </div>
      </>}
    </section>
  )
}
