import { useProvider } from './providerContext'
import { useEffect, useRef, useState } from 'react'
import {
  cancelEditAgent,
  acceptProgramReference,
  getEditAgent,
  startEditAgent,
} from './api'
import type { EditAgentResult, EditAgentRun, ProgramReference } from './api'

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
  provider_usage_limit: '{provider} 回報這次工作遇到帳號額度上限。請確認執行 Clipen 的 {provider} CLI 所登入帳號與額度，再手動重試；Clipen 沒有自動重送。',
  provider_authentication_required: '{provider} CLI 需要重新登入，這次工作沒有繼續執行。',
  provider_network_error: '{provider} 連線失敗，這次工作沒有完成；已保留先前結果。',
  provider_permission_error: '{provider} 的執行權限受阻，請檢查本次診斷紀錄。',
  provider_unavailable: '目前無法啟動 {provider}，請確認登入狀態後重新啟動 Clipen。',
  provider_timed_out: '這次 AI 剪輯方向樣片超過時間上限，已安全停止。',
  provider_protocol_error: '{provider} 沒有依照 Clipen 的安全流程完成剪輯。',
  provider_cancelled: '這次 AI 剪輯方向樣片已取消。',
  provider_launch_failed: 'Clipen 無法啟動這次 {provider} 樣片工作。',
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

const trackCopy = { video: '畫面', audio: '音訊', title: '字幕／文字', graphic: '插圖' }

function TimelineResultView({ result }: { result: EditAgentResult }) {
  const timeline = result.timeline
  const frameRate = timeline.frame_rate.num / timeline.frame_rate.den
  const seconds = timeline.duration_frames / frameRate
  const items = timeline.tracks.flatMap((track) => track.items)
  const mediaCount = items.filter((item) => item.kind === 'media').length
  const captionCount = timeline.tracks.filter((track) => track.track_id === 'agent-subtitles').reduce((sum, track) => sum + track.items.length, 0)
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
        <div><dt>節目段落</dt><dd>{result.episode?.beats.length ?? '—'}</dd></div>
        <div><dt>媒體片段</dt><dd>{mediaCount}</dd></div>
        <div><dt>口語字幕</dt><dd>{captionCount}</dd></div>
        <div><dt>字幕待核對</dt><dd>{result.subtitle_review?.draft_count ?? '—'}</dd></div>
      </dl>
      {result.production_contents && <details open>
        <summary>本版實際後製：{result.production_contents.cut_count} 個剪點、{result.production_contents.music.length} 段配樂、{result.production_contents.transitions.length} 個轉場</summary>
        {result.production_contents.music.length === 0 && <p>這個版本沒有配樂；新的完整後製工作必須加入。</p>}
        {result.production_contents.music.map((music, index) => <p key={index}>
          配樂：{music.provenance.title ?? music.name}／{music.provenance.creator ?? '作者未提供'}，{music.start_seconds.toFixed(1)}–{(music.start_seconds + music.duration_seconds).toFixed(1)} 秒，{music.gain_db} dB{music.duck ? '，對話時自動壓低' : ''}。
          {music.provenance.source_url && <a href={music.provenance.source_url} target="_blank" rel="noreferrer">來源</a>}{' '}
          {music.provenance.license_url && <a href={music.provenance.license_url} target="_blank" rel="noreferrer">授權：{music.provenance.license}</a>}
        </p>)}
        {result.production_contents.transitions.map((transition, index) => <p key={index}>轉場：{transition.at_seconds.toFixed(1)} 秒，{transition.kind === 'dip_black' ? '淡黑' : transition.kind === 'dip_white' ? '淡白' : transition.kind}。</p>)}
      </details>}
      {result.episode && <details><summary>{result.episode.premise}</summary>
        <p>觀眾會想知道：{result.episode.audience_question}</p>
        <ol>{result.episode.beats.map((beat) => <li key={beat.beat_id}>{beat.audience_takeaway}</li>)}</ol>
        <p>這段的回應：{result.episode.payoff}</p>
      </details>}
      {(result.subtitle_review?.draft_count ?? 0) > 0 && <p className="agent-timeline-note">字幕含 AI 辨識草稿，尚待聽音核對；沒有冒稱已由你確認。</p>}
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
        ? '這是 AI 透過工具製作的樣片時間軸。請檢查選材、敘事、文字、畫面及聲音；完整解碼與取樣檢視不代表成片品質已通過。'
        : incompleteProof
          ? '目前版本尚未通過樣片範圍檢查，已保留供你檢視。'
          : '這是舊流程產生的完整初剪，不符合新的短樣片規格。它會保留供比較，但不能作為新版預覽來源。'}</p>
      {result.editorial_review ? <details>
        <summary>查看 AI 的逐鏡判斷與文字依據</summary>
        <p>{result.editorial_review.story_assessment}</p>
        <ol>{result.editorial_review.shots.map((shot) => {
          const clip = items.find((item) => item.item_id === shot.item_id)
          return <li key={shot.item_id}>{clip ? `${(clip.start_frame / frameRate).toFixed(1)} 秒：` : ''}{shot.observation}</li>
        })}</ol>
        {result.editorial_review.text_checks.length > 0 && <ul>{result.editorial_review.text_checks.map((check) => {
          const text = items.find((item) => item.item_id === check.item_id)?.text
          return <li key={check.item_id}>「{text}」：{check.evidence}</li>
        })}</ul>}
        <p>聲音檢視限制：{result.editorial_review.audio_limitations}</p>
        <p>以上是 AI 自評；取樣畫面不代表已看完連續動作或聽過成片，仍需你驗收。</p>
      </details> : <p className="agent-timeline-note">這個舊版本沒有逐鏡審片與文字依據紀錄。</p>}
      {result.export_url && <a className="button ghost" href={result.export_url} download>下載可編輯時間軸與來源紀錄</a>}
    </div>
  )
}

export function EditAgentSection({ projectId, csrf, canStart, onResultAvailable }: Props) {
  const provider = useProvider()
  const [run, setRun] = useState<EditAgentRun | null>(null)
  const [result, setResult] = useState<EditAgentResult | null>(null)
  const [reference, setReference] = useState<ProgramReference | null>(null)
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
      .then((status) => { if (mounted) { setRun(status.latest_run); setResult(status.result); setReference(status.program_reference ?? null) } })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
      .finally(() => { if (mounted) setLoading(false) })
    return () => { mounted = false }
  }, [projectId])

  const active = run ? activeStates.has(run.state) : false
  // finish_edit commits the final revision before the provider process exits and
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

  async function acceptReference() {
    if (!result || busy) return
    setBusy(true)
    setError(null)
    try { setReference(await acceptProgramReference(csrf, projectId, result)) }
    catch (caught) { setError(errorMessage(caught)) }
    finally { setBusy(false) }
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
          <strong id="edit-agent-title">讓 AI 使用工具製作與檢視樣片</strong>
          <p>AI 自行選材與精剪、安排節目字幕、選取有來源授權的配樂並混音，以及在合適剪點加入轉場；不使用獨立字卡。這次交付 30–75 秒 SDR 完整後製樣片，仍需你看片驗收。</p>
        </div>
      </div>

      {loading && <div className="edit-agent-status" role="status"><span className="small-spinner" aria-hidden="true" /><div><strong>正在讀取 AI 樣片狀態</strong><p>不會因此呼叫 AI。</p></div></div>}
      {error && <div className="inline-message error" role="alert">{error}</div>}

      {!loading && polling && run && (
        <div className="edit-agent-running" role="status" aria-live="polite">
          <div className="edit-agent-progress-copy"><div><strong>{finalizing ? '正在保存 Token 用量…' : runLabel(run)}</strong><span>{calls} / {maxCalls} 次工具操作</span></div><p>{finalizing ? `AI 剪輯版本已保存，正在等待 ${provider.name} 回報這次工作的最終用量。` : 'AI 正在依需要調用素材、剪輯、畫面、聲音與預覽工具；每次修改都有可回復版本。'}</p></div>
          <progress max="100" value={finalizing ? 99 : progress}>{finalizing ? 99 : progress}%</progress>
          {!finalizing && <button className="button ghost" disabled={busy} onClick={() => void cancel()}>{busy ? '正在取消…' : '取消樣片製作'}</button>}
        </div>
      )}

      {!loading && succeeded && !finalizing && run && (
        <div className="edit-agent-result success" role="status">
          <div><span aria-hidden="true">{proofReady ? '✓' : '!'}</span><div><strong>{proofReady ? 'AI 剪輯方向樣片已建立' : incompleteResult ? '剪輯方向樣片尚未完成' : '這是舊版完整初剪'}</strong><p>{proofReady ? '精剪時間軸已保存；先判斷選材與節奏是否值得進入成片製作。' : incompleteResult ? '目前結果尚未通過樣片檢查，請查看工作紀錄。' : '舊結果已保留，但不符合新的短樣片規格。請製作新的剪輯方向樣片。'}</p></div></div>
          <dl>
            <div><dt>工具操作</dt><dd>{formatTokens(run.budget.tool_calls)} 次</dd></div>
            <div><dt>新 Input</dt><dd>{uncachedInput !== null ? formatTokens(uncachedInput) : '未回報'}</dd></div>
            <div><dt>快取 Input</dt><dd>{run.usage ? formatTokens(run.usage.cached_input_tokens) : '未回報'}</dd></div>
            <div><dt>Output</dt><dd>{run.usage ? formatTokens(run.usage.output_tokens) : '未回報'}</dd></div>
            <div><dt>處理總量</dt><dd>{run.usage ? formatTokens(run.usage.total_tokens) : '未回報'}</dd></div>
          </dl>
          {run.usage && <p className="agent-usage-note">處理總量包含快取內容，不等於同額新增 Quota；實際帳號額度仍以 {provider.name} 計算為準。</p>}
          {result && <TimelineResultView result={result} />}
        </div>
      )}

      {!loading && failed && run && (
        <div className="edit-agent-result error" role="alert"><div><span aria-hidden="true">×</span><div><strong>{runLabel(run)}</strong><p>{failureCopy[run.error_code ?? '']?.replaceAll('{provider}', provider.name) ?? '本次工作未完成；已保留原片與先前結果。'}</p><p>本次工具操作 {run.budget.tool_calls} 次。{run.usage ? `已回報：輸入 ${formatTokens(run.usage.input_tokens)}、輸出 ${formatTokens(run.usage.output_tokens)} Tokens。` : 'Token 用量未回報，不代表零用量。'}</p></div></div></div>
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
          {result?.creative_direction?.format === 'travel_program' && <div>
            <strong>{reference?.revision_id === result.revision_id ? '這版已設為節目基準' : '喜歡這版的節目方向？'}</strong>
            <p>保存你認可的敘事與字幕呈現。下一次 AI 會以此尋找另一個事件，不會直接重播相同素材；字幕和聲音仍需核對。</p>
            <button className="button ghost" disabled={busy || reference?.revision_id === result.revision_id} onClick={() => void acceptReference()}>以這版作為節目基準</button>
          </div>}
          <div>
            <strong>{proofReady ? '需要重新製作嗎？' : incompleteResult ? '重新建立完整方向樣片' : legacyResult ? '製作新的剪輯方向樣片' : '開始前確認'}</strong>
            <p>{reference ? 'AI 會參考你認可的節目方向，自行搜尋其他原片對話、選材、製作並回看修改。' : 'AI 會依節目方向搜尋原片對話、選材、製作並回看修改。'}最多 96 次工具操作、4 次本機預覽。Clipen 會保存實際 Token；配樂搜尋只傳一般音樂詞，不會上傳完整原片或音訊。</p>
          </div>
          <label className="consent-check"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span>我同意 AI 使用 {provider.name} 額度、讀取素材縮圖與口語，並依需要搜尋及下載有來源與授權資料的公開配樂</span></label>
          <button className="button primary" disabled={!canStart || !consent || busy} onClick={() => void start()}>{busy ? '正在啟動…' : '製作完整後製樣片'}</button>
          {!canStart && <p className="edit-agent-blocked">請先完成上方的剪輯版本檢查。</p>}
        </div>
      )}
    </section>
  )
}
