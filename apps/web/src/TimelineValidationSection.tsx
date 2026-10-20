import { useEffect, useState } from 'react'
import { EditAgentSection } from './EditAgentSection'
import { FinishingSection } from './FinishingSection'
import {
  acceptTimelineWarnings,
  buildEditTimeline,
  cancelPreviewRender,
  getEditTimeline,
  getPlanning,
  getPreviewRender,
  startPreviewRender,
} from './api'
import type {
  EditTimelineState,
  EditablePlan,
  PreviewRender,
  PreviewRenderStatus,
} from './api'

type Props = { projectId: string; csrf: string }
type OutputPreset = 'landscape_1080p' | 'portrait_1080p'
type FrameRate = 24 | 25 | 30 | 60

const stateCopy = {
  not_ready: ['3', '建立第一個剪輯版本', '選擇輸出畫面後，Clipen 會把已確認的計畫轉成精確且可重現的時間軸。'],
  stale: ['↻', '剪輯計畫已更新', '目前時間軸來自舊版計畫，請重新建立後再繼續。'],
  validation_required: ['!', '需要重新檢查', '時間軸已存在，但找不到與它完全一致的安全檢查。'],
  ready: ['✓', '可以產生結構預覽', '目前剪輯版本已通過來源、內容與輸出能力檢查。'],
  needs_confirmation: ['!', '開始前需要你的確認', '這些項目不會阻止剪輯，但可能影響畫面或輸出結果。'],
  output_pending: ['✓', '可以製作 AI 剪輯方向樣片', '目前項目只影響尚未完成的預覽或輸出能力，不影響 AI 安全地建立短樣片時間軸。'],
  blocked: ['×', '這份剪輯版本還不能使用', '請查看下方問題，修正後重新建立剪輯版本。'],
} as const

const activeRenderStates = new Set(['queued', 'running', 'cancelling'])

const renderFailureCopy: Record<string, string> = {
  ffmpeg_unavailable: '系統找不到 FFmpeg，目前無法產生初剪。請確認 FFmpeg 已安裝後重啟 Clipen。',
  ffmpeg_start_failed: '無法啟動本機影片輸出程式。',
  ffmpeg_render_failed: '影片輸出失敗，可能是素材格式或來源磁碟暫時無法讀取。',
  media_changed: '毛片在分析後曾被變更，請重新檢查素材再建立時間軸。',
  media_offline: '目前讀不到毛片，請確認外接硬碟仍有連接。',
  media_unavailable: 'Clipen 無法安全開啟時間軸中的毛片。',
  preview_video_required: '這份時間軸沒有可供預覽的影像片段。',
  preview_video_stream_missing: '其中一支毛片找不到可播放的影像串流，請重新檢查素材。',
  preview_audio_stream_missing: '其中一支獨立音訊素材找不到可播放的音訊串流。',
  preview_platform_unsupported: '目前的初剪輸出尚未支援這個作業系統。',
  preview_title_runtime_missing: 'Clipen 的字卡元件尚未安裝完成，請重新執行 install。',
  preview_title_font_missing: '系統找不到可顯示繁體中文的字型，暫時無法產生字卡。',
  preview_title_limit_exceeded: '這一版的字卡數量超過本機預覽上限，請先精簡字卡。',
  preview_style_proof_required: '這是舊版完整初剪，請先製作新的 45–60 秒剪輯方向樣片。',
  controller_restarted: 'Clipen 關閉時中斷了輸出，請重新產生。',
}

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : 'Clipen 無法讀取剪輯版本。'
}

function renderFailureMessage(render: PreviewRender): string {
  if (render.state === 'cancelled') return '這次初剪已取消，時間軸與毛片都沒有被修改。'
  if (render.state === 'interrupted') return 'Clipen 關閉時中斷了輸出，請重新產生。'
  return renderFailureCopy[render.error_code ?? ''] ?? '初剪影片沒有成功產生，請再試一次。'
}

export function TimelineValidationSection({ projectId, csrf }: Props) {
  const [status, setStatus] = useState<EditTimelineState | null>(null)
  const [plan, setPlan] = useState<EditablePlan | null>(null)
  const [preview, setPreview] = useState<PreviewRenderStatus | null>(null)
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
    const [timeline, planning, render] = await Promise.all([
      getEditTimeline(projectId),
      getPlanning(projectId),
      getPreviewRender(projectId),
    ])
    applyStatus(timeline)
    setPlan(planning.current_plan?.status === 'confirmed' ? planning.current_plan : null)
    setPreview(render)
  }

  useEffect(() => {
    let mounted = true
    Promise.all([
      getEditTimeline(projectId),
      getPlanning(projectId),
      getPreviewRender(projectId),
    ])
      .then(([timeline, planning, render]) => {
        if (!mounted) return
        applyStatus(timeline)
        setPlan(planning.current_plan?.status === 'confirmed' ? planning.current_plan : null)
        setPreview(render)
      })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
    return () => { mounted = false }
  }, [projectId])

  const renderActive = preview?.render
    ? activeRenderStates.has(preview.render.state)
    : false

  useEffect(() => {
    if (!renderActive) return
    let mounted = true
    let timer: number | undefined
    const poll = async () => {
      try {
        const next = await getPreviewRender(projectId)
        if (!mounted) return
        setPreview(next)
        if (next.render && activeRenderStates.has(next.render.state)) {
          timer = window.setTimeout(() => void poll(), 800)
        }
      } catch (caught) {
        if (mounted) setError(errorMessage(caught))
      }
    }
    timer = window.setTimeout(() => void poll(), 500)
    return () => {
      mounted = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [projectId, renderActive])

  async function build() {
    if (!plan || busy) return
    setBusy(true)
    setError(null)
    try {
      applyStatus(await buildEditTimeline(csrf, projectId, plan, preset, frameRate))
      setPreview(await getPreviewRender(projectId))
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

  async function startPreview() {
    if (!status?.timeline || busy || renderActive) return
    setBusy(true)
    setError(null)
    try {
      setPreview(await startPreviewRender(
        csrf,
        projectId,
        preview?.target_timeline_id ?? status.timeline.timeline_id,
      ))
    } catch (caught) {
      setError(errorMessage(caught))
      try { setPreview(await getPreviewRender(projectId)) } catch { /* Keep command error. */ }
    } finally {
      setBusy(false)
    }
  }

  async function cancelPreview() {
    if (!preview?.render || !renderActive) return
    setBusy(true)
    setError(null)
    try {
      setPreview(await cancelPreviewRender(
        csrf,
        projectId,
        preview.render.render_id,
      ))
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  const currentState = status?.state ?? 'not_ready'
  const issues = status?.validation?.report?.issues ?? []
  const draftSafeRules = new Set([
    'timeline.capability.element_unverified',
    'timeline.capability.effect_unknown',
    'timeline.capability.effect_delivery_unverified',
    'timeline.content.audio_missing',
    'timeline.reference.style_missing',
  ])
  const outputPendingOnly = currentState === 'blocked'
    && issues.length > 0
    && issues.every((issue) => draftSafeRules.has(issue.rule_id))
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
  const previewAllowed = displayState === 'ready' || displayState === 'output_pending'
  const agentAllowed = status?.can_execute === true || outputPendingOnly
  const currentRender = preview?.render ?? null
  const succeeded = currentRender?.state === 'succeeded' && !preview?.stale
  const retryable = currentRender !== null
    && ['failed', 'cancelled', 'interrupted'].includes(currentRender.state)
  const progress = Math.round((currentRender?.progress ?? 0) * 100)
  const aiStyleProof = preview?.source === 'ai' && preview.target_is_style_proof === true
  const incompleteAiProof = preview?.source === 'ai'
    && preview.target_is_style_proof === false
    && (preview.target_stats?.duration_seconds ?? 0) >= 30
    && (preview.target_stats?.duration_seconds ?? 0) <= 75
  const legacyAiDraft = preview?.source === 'ai'
    && preview.target_is_style_proof === false
    && !incompleteAiProof

  return (
    <section className="timeline-validation-section" aria-labelledby="timeline-validation-title">
      <div className="analysis-heading"><div>
        <p className="eyebrow">04 · 剪輯</p>
        <h2 id="timeline-validation-title">建立並檢查影片結構</h2>
        <p>先用 45–60 秒方向樣片驗證選材、順序與鏡頭節奏。字幕、音樂、音效、轉場與成片視覺會由後續製作階段處理；毛片只會被讀取。</p>
      </div></div>

      {!status && !error && <div className="validation-card" role="status"><span className="small-spinner" aria-hidden="true" /><div><strong>正在讀取剪輯版本</strong><p>這次查詢不會讀取或修改毛片。</p></div></div>}
      {error && <div className="inline-message error" role="alert">{error}</div>}

      {status && <>
        {canBuild && <div className="timeline-builder">
          <div><strong>1 · 建立時間軸</strong><p>設定預覽畫面。重新建立後，舊預覽會標示為過期。</p></div>
          <label>畫面比例<select value={preset} onChange={(event) => setPreset(event.target.value as OutputPreset)}><option value="landscape_1080p">橫式 16:9 · 1920 × 1080</option><option value="portrait_1080p">直式 9:16 · 1080 × 1920</option></select></label>
          <label>格率<select value={frameRate} onChange={(event) => setFrameRate(Number(event.target.value) as FrameRate)}>{[24, 25, 30, 60].map((rate) => <option key={rate} value={rate}>{rate} fps</option>)}</select></label>
          <button className="button primary" disabled={busy || renderActive || !plan} onClick={() => void build()}>{busy && !renderActive ? '正在建立…' : status.timeline ? '重新建立時間軸' : '建立時間軸'}</button>
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

        {status.timeline && (
          <EditAgentSection
            projectId={projectId}
            csrf={csrf}
            canStart={agentAllowed}
            onResultAvailable={() => void refresh()}
          />
        )}

        {status.timeline && previewAllowed && (
          <div className="preview-render-card">
            <div className="preview-render-heading">
              <div>
                <span className="preview-step optional">選用</span>
                <strong>{aiStyleProof ? '觀看 AI 剪輯方向樣片' : incompleteAiProof ? '剪輯方向樣片尚未完成' : legacyAiDraft ? '舊版完整初剪（停止使用）' : '觀看 AI 執行前的結構預覽'}</strong>
                <p>{aiStyleProof ? '這是 AI 工具工作流程產生的樣片；本機重新輸出不使用 AI Token。請看片檢查敘事、字幕、畫面與聲音，技術檢查不代表剪輯品質已通過。' : incompleteAiProof ? '目前版本尚未通過樣片範圍檢查，請查看工作紀錄。' : legacyAiDraft ? `這版長 ${Math.round(preview?.target_stats?.duration_seconds ?? 0)} 秒，未符合新的短樣片規格，不再重新輸出。請先在上方製作新的剪輯方向樣片。` : '這只會把上方的基礎時間軸在本機產生 MP4，不使用 AI Token。需要先檢查原始選材與順序時再使用。'}</p>
              </div>
              {!renderActive && currentRender?.state !== 'succeeded' && (
                <button className="button primary" disabled={busy || !preview?.can_start} onClick={() => void startPreview()}>
                  {retryable ? `重新產生${aiStyleProof ? '方向樣片' : incompleteAiProof ? '未完成樣片' : '結構預覽'}` : `產生${aiStyleProof ? '方向樣片' : incompleteAiProof ? '未完成樣片' : '結構預覽'}`}
                </button>
              )}
            </div>

            {currentRender && renderActive && (
              <div className="preview-render-progress" role="status" aria-live="polite">
                <div><strong>{currentRender.state === 'cancelling' ? '正在取消…' : '正在產生結構預覽…'}</strong><span>{progress}%</span></div>
                <progress max="100" value={progress}>{progress}%</progress>
                <p>可以留在此頁等待；關閉 Clipen 會中斷這次輸出，但不會影響毛片。</p>
                <button className="button ghost" disabled={busy || currentRender.state === 'cancelling'} onClick={() => void cancelPreview()}>取消輸出</button>
              </div>
            )}

            {succeeded && currentRender?.playback_url && (
              <div className="preview-player">
                <video
                  key={currentRender.render_id}
                  controls
                  preload="metadata"
                  src={currentRender.playback_url}
                >
                  你的瀏覽器無法播放這支結構預覽。
                </video>
                <div><strong>{aiStyleProof ? 'AI 剪輯方向樣片已完成' : incompleteAiProof ? '未完成樣片已輸出' : legacyAiDraft ? '這是舊版完整初剪' : '結構預覽已完成'}</strong><p>{aiStyleProof ? '請判斷這 45–60 秒的選材、順序與鏡頭節奏；字幕、音樂、音效與成片視覺尚未製作。' : incompleteAiProof ? '這版只用來檢查選材、順序與字卡；效果尚未完成，不代表最終質感。' : legacyAiDraft ? '這份結果已證明不可用，請回到上方製作新的短樣片。' : '請先確認選材、順序與原音。'}</p>{!legacyAiDraft && <button className="button ghost" disabled={busy} onClick={() => void startPreview()}>重新產生</button>}</div>
              </div>
            )}

            {currentRender && preview?.stale && currentRender.state === 'succeeded' && (
              <div className="preview-render-message warning"><strong>這是上一版時間軸的預覽</strong><p>{incompleteAiProof ? 'AI 已建立新的短樣片結構，但效果尚未完成。可以先在本機輸出檢查選材與順序。' : preview.source === 'ai' ? 'AI 已建立新的短樣片，請產生新版後再判斷內容。' : '時間軸已更新，請產生新的結構預覽再判斷內容。'}</p><button className="button primary" disabled={busy || !preview.can_start} onClick={() => void startPreview()}>{incompleteAiProof ? '產生未完成樣片' : '產生新版預覽'}</button></div>
            )}

            {currentRender && retryable && (
              <div className="preview-render-message error" role="alert"><strong>這次沒有產生結構預覽</strong><p>{renderFailureMessage(currentRender)}</p></div>
            )}
          </div>
        )}
        <details><summary>舊版製作實驗紀錄</summary><FinishingSection archive projectId={projectId} csrf={csrf} directionId={preview?.target_timeline_id} directionUrl={preview?.source === 'ai' && !preview.stale ? currentRender?.playback_url : null} /></details>
      </>}
    </section>
  )
}
