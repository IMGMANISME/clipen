import { useEffect, useRef, useState } from 'react'
import {
  cancelEditAgent,
  getEditAgent,
  startEditAgent,
} from './api'
import type { EditAgentResult, EditAgentRun } from './api'

type Props = {
  projectId: string
  csrf: string
  canStart: boolean
  onResultAvailable?: () => void
}

const activeStates = new Set(['queued', 'running'])

const phaseCopy: Record<string, string> = {
  queued: '正在等待 AI 剪輯方向樣片工作',
  starting: '正在建立安全剪輯工作區',
  editing: 'AI 正在精剪 45–60 秒剪輯方向樣片',
  completed: 'AI 剪輯方向樣片已建立',
  failed: 'AI 剪輯方向樣片沒有完成',
  cancelled: 'AI 剪輯方向樣片已取消',
  interrupted: 'Clipen 關閉時中斷了這次樣片工作',
}

const failureCopy: Record<string, string> = {
  provider_unavailable: '目前無法啟動 Codex，請確認登入狀態後重新啟動 Clipen。',
  provider_timed_out: '這次 AI 剪輯方向樣片超過時間上限，已安全停止。',
  provider_protocol_error: 'Codex 沒有依照 Clipen 的安全流程完成剪輯。',
  provider_cancelled: '這次 AI 剪輯方向樣片已取消。',
  provider_launch_failed: 'Clipen 無法啟動這次 Codex 樣片工作。',
  controller_stopped: 'Clipen 關閉時已安全停止這次剪輯，可以重新開始。',
  user_cancelled: '這次 AI 剪輯已由你取消。',
}

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : 'Clipen 無法完成這次 AI 剪輯方向樣片。'
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat('zh-TW').format(value)
}

function runLabel(run: EditAgentRun): string {
  return phaseCopy[run.phase] ?? (run.state === 'running' ? phaseCopy.editing : '正在更新剪輯狀態')
}

const trackCopy = { video: '畫面', audio: '音訊', title: '字卡', graphic: '插圖' }

function TimelineResultView({ result }: { result: EditAgentResult }) {
  const timeline = result.timeline
  const frameRate = timeline.frame_rate.num / timeline.frame_rate.den
  const seconds = timeline.duration_frames / frameRate
  const items = timeline.tracks.flatMap((track) => track.items)
  const mediaCount = items.filter((item) => item.kind === 'media').length
  const titleCount = items.filter((item) => item.kind === 'text').length
  const effectCount = items.reduce((sum, item) => sum + (item.effects?.length ?? 0), 0)
  const proof = result.is_style_proof
  const incompleteProof = !proof
    && result.proof_stats.duration_seconds >= 30
    && result.proof_stats.duration_seconds <= 75
  const timelineLabel = proof
    ? 'AI 剪輯方向樣片時間軸'
    : incompleteProof
      ? '未完成的剪輯方向樣片時間軸'
      : '舊版完整初剪時間軸'

  return (
    <div className="agent-timeline" aria-label={timelineLabel}>
      <div className="agent-timeline-heading"><div><strong>{timelineLabel}</strong><p>{result.summary}</p></div><span>{seconds.toFixed(1)} 秒</span></div>
      <dl className="agent-timeline-summary">
        <div><dt>軌道</dt><dd>{timeline.tracks.length}</dd></div>
        <div><dt>媒體片段</dt><dd>{mediaCount}</dd></div>
        <div><dt>字卡</dt><dd>{titleCount}</dd></div>
        <div><dt>效果</dt><dd>{effectCount}</dd></div>
      </dl>
      <div className="agent-track-list">
        {timeline.tracks.map((track) => (
          <div className={`agent-track track-${track.kind}`} key={track.track_id}>
            <div className="agent-track-label"><strong>{trackCopy[track.kind]}</strong><span>{track.items.length} 個</span></div>
            <div className="agent-track-lane">
              {track.items.map((item, index) => {
                const left = item.start_frame / timeline.duration_frames * 100
                const width = item.duration_frames / timeline.duration_frames * 100
                const label = item.kind === 'text' ? item.text : item.kind === 'graphic' ? '插圖' : `片段 ${index + 1}`
                const effects = item.effects?.length ?? 0
                return <div
                  className={`agent-track-item item-${item.kind}`}
                  key={item.item_id}
                  style={{ left: `${left}%`, width: `${Math.max(width, .65)}%` }}
                  title={`${label ?? item.item_id}${effects ? ` · ${effects} 個效果` : ''}`}
                ><span>{label}</span>{effects > 0 && <b>{effects} fx</b>}</div>
              })}
            </div>
          </div>
        ))}
      </div>
      <p className="agent-timeline-note">{proof
        ? '這支樣片只用來判斷選材、順序與鏡頭節奏。字幕、音樂、音效、轉場與成片視覺會由後續製作階段處理。'
        : incompleteProof
          ? '鏡頭長度已符合短樣片範圍，但代表性的字卡或效果沒有完成。這版會保留供檢查，不能當成完成的剪輯方向樣片。'
          : '這是舊流程產生的完整初剪，不符合新的短樣片規格。它會保留供比較，但不能作為新版預覽來源。'}</p>
    </div>
  )
}

export function EditAgentSection({ projectId, csrf, canStart, onResultAvailable }: Props) {
  const [run, setRun] = useState<EditAgentRun | null>(null)
  const [result, setResult] = useState<EditAgentResult | null>(null)
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const onResultAvailableRef = useRef(onResultAvailable)

  useEffect(() => {
    onResultAvailableRef.current = onResultAvailable
  }, [onResultAvailable])

  useEffect(() => {
    let mounted = true
    getEditAgent(projectId)
      .then((status) => { if (mounted) { setRun(status.latest_run); setResult(status.result) } })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
      .finally(() => { if (mounted) setLoading(false) })
    return () => { mounted = false }
  }, [projectId])

  const active = run ? activeStates.has(run.state) : false
  // finish_edit commits the final revision before the Codex process exits and
  // reports usage. Keep polling through that short durable handoff so a fast
  // UI refresh does not permanently display "未回報" for a known attempt.
  const finalizing = run?.state === 'succeeded' && run.provider_attempt_id === null
  const polling = active || finalizing

  useEffect(() => {
    if (!polling) return
    let mounted = true
    let timer: number | undefined
    const poll = async () => {
      try {
        const status = await getEditAgent(projectId)
        if (!mounted) return
        setRun(status.latest_run)
        setResult(status.result)
        const keepPolling = status.latest_run && (
          activeStates.has(status.latest_run.state)
          || (status.latest_run.state === 'succeeded' && status.latest_run.provider_attempt_id === null)
        )
        if (keepPolling) {
          timer = window.setTimeout(() => void poll(), 1000)
        } else if (status.latest_run?.state === 'succeeded' && status.result) {
          onResultAvailableRef.current?.()
        }
      } catch (caught) {
        if (mounted) setError(errorMessage(caught))
      }
    }
    timer = window.setTimeout(() => void poll(), 600)
    return () => {
      mounted = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [polling, projectId])

  async function start() {
    if (!canStart || !consent || polling || busy) return
    setBusy(true)
    setError(null)
    try {
      setRun(await startEditAgent(csrf, projectId))
      setResult(null)
      setConsent(false)
    } catch (caught) {
      setError(errorMessage(caught))
      try {
        const status = await getEditAgent(projectId)
        setRun(status.latest_run)
        setResult(status.result)
      } catch { /* Keep command error. */ }
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    if (!run || !active || busy) return
    setBusy(true)
    setError(null)
    try {
      setRun(await cancelEditAgent(csrf, projectId, run.run_id))
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  const failed = run && ['failed', 'cancelled', 'interrupted'].includes(run.state)
  const succeeded = run?.state === 'succeeded'
  const proofReady = succeeded && result?.is_style_proof === true
  const incompleteResult = result?.is_style_proof === false
    && result.proof_stats.duration_seconds >= 30
    && result.proof_stats.duration_seconds <= 75
  const legacyResult = result?.is_style_proof === false && !incompleteResult
  const calls = run?.budget.tool_calls ?? 0
  const maxCalls = run?.budget.max_tool_calls ?? 48
  const progress = active ? Math.min(96, Math.max(4, Math.round(calls / maxCalls * 100))) : 0
  const uncachedInput = run?.usage
    ? Math.max(0, run.usage.input_tokens - run.usage.cached_input_tokens)
    : null

  return (
    <section className="edit-agent-card" aria-labelledby="edit-agent-title">
      <div className="edit-agent-heading">
        <span className="preview-step">2</span>
        <div>
          <strong id="edit-agent-title">先製作 45–60 秒 AI 剪輯方向樣片</strong>
          <p>AI 會精確選擇入點與出點，只驗證選材、順序與鏡頭節奏，不代表最終成片質感。Codex 不能修改毛片；關閉瀏覽器不會中斷工作。</p>
        </div>
      </div>

      {loading && <div className="edit-agent-status" role="status"><span className="small-spinner" aria-hidden="true" /><div><strong>正在讀取 AI 樣片狀態</strong><p>不會因此呼叫 AI。</p></div></div>}
      {error && <div className="inline-message error" role="alert">{error}</div>}

      {!loading && polling && run && (
        <div className="edit-agent-running" role="status" aria-live="polite">
          <div className="edit-agent-progress-copy"><div><strong>{finalizing ? '正在保存 Token 用量…' : runLabel(run)}</strong><span>{calls} / {maxCalls} 次工具操作</span></div><p>{finalizing ? 'AI 剪輯版本已保存，正在等待 Codex 回報這次工作的最終用量。' : 'AI 只會讀取已確認的計畫、核准的素材資訊與縮圖，並建立新的可回復時間軸版本。'}</p></div>
          <progress max="100" value={finalizing ? 99 : progress}>{finalizing ? 99 : progress}%</progress>
          {!finalizing && <button className="button ghost" disabled={busy} onClick={() => void cancel()}>{busy ? '正在取消…' : '取消樣片製作'}</button>}
        </div>
      )}

      {!loading && succeeded && !finalizing && run && (
        <div className="edit-agent-result success" role="status">
          <div><span aria-hidden="true">{proofReady ? '✓' : '!'}</span><div><strong>{proofReady ? 'AI 剪輯方向樣片已建立' : incompleteResult ? '剪輯方向樣片尚未完成' : '這是舊版完整初剪'}</strong><p>{proofReady ? '精剪時間軸已保存；先判斷選材與節奏是否值得進入成片製作。' : incompleteResult ? '鏡頭結構已保存，但代表性的字卡或效果沒有完成。請修正後重新製作。' : '舊結果已保留，但不符合新的短樣片規格。請製作新的剪輯方向樣片。'}</p></div></div>
          <dl>
            <div><dt>工具操作</dt><dd>{formatTokens(run.budget.tool_calls)} 次</dd></div>
            <div><dt>新 Input</dt><dd>{uncachedInput !== null ? formatTokens(uncachedInput) : '未回報'}</dd></div>
            <div><dt>快取 Input</dt><dd>{run.usage ? formatTokens(run.usage.cached_input_tokens) : '未回報'}</dd></div>
            <div><dt>Output</dt><dd>{run.usage ? formatTokens(run.usage.output_tokens) : '未回報'}</dd></div>
            <div><dt>處理總量</dt><dd>{run.usage ? formatTokens(run.usage.total_tokens) : '未回報'}</dd></div>
          </dl>
          {run.usage && <p className="agent-usage-note">處理總量包含快取內容，不等於同額新增 Quota；實際帳號額度仍以 Codex 計算為準。</p>}
          {result && <TimelineResultView result={result} />}
        </div>
      )}

      {!loading && failed && run && (
        <div className="edit-agent-result error" role="alert"><div><span aria-hidden="true">×</span><div><strong>{runLabel(run)}</strong><p>{failureCopy[run.error_code ?? ''] ?? '剪輯版本與毛片都保持不變，可以重新開始。'}</p></div></div></div>
      )}

      {!loading && !succeeded && result && (
        <div className="preserved-agent-result">
          <strong>{incompleteResult ? '已保留未完成的剪輯方向樣片' : legacyResult ? '已保留舊版完整初剪' : '已保留上一次成功的 AI 剪輯方向樣片'}</strong>
          <p>{incompleteResult ? '可以檢查鏡頭結構，但它不會被當作完成結果輸出。' : legacyResult ? '舊結果只供比較；請重新製作符合新規格的短樣片。' : '這次未完成的操作不會覆蓋可用結果；下方預覽仍以這個版本為準。'}</p>
          <TimelineResultView result={result} />
        </div>
      )}

      {!loading && !polling && (
        <div className="edit-agent-consent">
          <div>
            <strong>{proofReady ? '需要重新製作嗎？' : incompleteResult ? '重新建立完整方向樣片' : legacyResult ? '製作新的剪輯方向樣片' : '開始前確認'}</strong>
            <p>這會使用一次 Codex 工作額度。Clipen 會保存本次實際 Token；不會上傳完整影片或音訊。</p>
          </div>
          <label className="consent-check"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span>我同意使用 Codex 額度製作這次剪輯方向樣片</span></label>
          <button className="button primary" disabled={!canStart || !consent || busy} onClick={() => void start()}>{busy ? '正在啟動…' : proofReady ? '重新製作方向樣片' : '製作新的方向樣片'}</button>
          {!canStart && <p className="edit-agent-blocked">請先完成上方的剪輯版本檢查。</p>}
        </div>
      )}
    </section>
  )
}
