import { useEffect, useState } from 'react'
import {
  acceptTimelineWarnings,
  getTimelineValidation,
} from './api'
import type { TimelineValidationState } from './api'

type Props = {
  projectId: string
  csrf: string
}

const stateCopy = {
  not_ready: {
    symbol: '3',
    title: '尚未建立剪輯版本',
    body: '素材分析會保留給下一步使用。等 AI 產生可編輯的剪輯計畫後，Clipen 才會檢查它能否安全執行與匯出。',
  },
  ready: {
    symbol: '✓',
    title: '可以開始剪輯',
    body: '目前的剪輯版本已通過檢查；若內容或輸出能力改變，Clipen 會要求重新檢查。',
  },
  needs_confirmation: {
    symbol: '!',
    title: '開始前需要你的確認',
    body: '這些項目不會阻止剪輯，但可能影響畫面或匯出結果。確認後只套用在目前這份剪輯版本。',
  },
  blocked: {
    symbol: '×',
    title: '目前無法開始剪輯',
    body: '請先修正下列問題。阻擋項目不能用確認按鈕略過。',
  },
} as const

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : 'Clipen 無法讀取剪輯檢查狀態。'
}

export function TimelineValidationSection({ projectId, csrf }: Props) {
  const [status, setStatus] = useState<TimelineValidationState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let mounted = true
    getTimelineValidation(projectId)
      .then((result) => { if (mounted) setStatus(result) })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
    return () => { mounted = false }
  }, [projectId])

  async function acceptWarnings() {
    if (!status?.report || busy) return
    setBusy(true)
    setError(null)
    try {
      setStatus(await acceptTimelineWarnings(csrf, projectId, status.report.report_id))
    } catch (caught) {
      setError(errorMessage(caught))
      try {
        setStatus(await getTimelineValidation(projectId))
      } catch {
        // Keep the actionable command error if refreshing also fails.
      }
    } finally {
      setBusy(false)
    }
  }

  const currentState = status?.state ?? 'not_ready'
  const copy = stateCopy[currentState]
  const issues = status?.report?.issues ?? []
  const visibleIssues = issues.slice(0, 20)

  return (
    <section className="timeline-validation-section" aria-labelledby="timeline-validation-title">
      <div className="analysis-heading">
        <div>
          <p className="eyebrow">03 · 計畫</p>
          <h2 id="timeline-validation-title">剪輯執行檢查</h2>
          <p>Clipen 會在動到素材前，確認剪輯內容、來源與 DaVinci Resolve 可編輯輸出能力是否一致。</p>
        </div>
      </div>

      {!status && !error && (
        <div className="validation-card" role="status">
          <span className="small-spinner" aria-hidden="true" />
          <div><strong>正在讀取剪輯檢查</strong><p>毛片不會因為這次查詢而被讀取或修改。</p></div>
        </div>
      )}

      {error && <div className="inline-message error" role="alert">{error}</div>}

      {status && (
        <div className={`validation-card state-${currentState}`} role={currentState === 'blocked' ? 'alert' : 'status'}>
          <span className="validation-symbol" aria-hidden="true">{copy.symbol}</span>
          <div className="validation-content">
            <strong>{copy.title}</strong>
            <p>{copy.body}</p>
            {visibleIssues.length > 0 && (
              <ul className="validation-issues">
                {visibleIssues.map((issue) => (
                  <li key={`${issue.rule_id}:${issue.location}:${issue.item_id ?? ''}`}>
                    <strong>{issue.message}</strong>
                    <span>{issue.suggestion}</span>
                  </li>
                ))}
              </ul>
            )}
            {issues.length > visibleIssues.length && (
              <p className="validation-overflow">另有 {(issues.length - visibleIssues.length).toLocaleString()} 個項目；修正剪輯計畫後會重新檢查。</p>
            )}
          </div>
          {currentState === 'needs_confirmation' && (
            <button className="button primary" disabled={busy} onClick={() => void acceptWarnings()}>
              {busy ? '正在保存確認…' : `接受這 ${issues.length.toLocaleString()} 項警告並繼續`}
            </button>
          )}
        </div>
      )}
    </section>
  )
}
