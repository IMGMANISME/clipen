import { useEffect, useState } from 'react'
import { cancelFinishing, finishingCommand, getFinishing, resumeFinishing } from './api'
import type { FinishingStatus } from './api'

const activeStates = new Set(['queued', 'running', 'cancelling'])
const phases: Record<string, string> = {
  queued: '正在準備', understanding: 'AI 正在理解影片與這次修改方向',
  finding_music: '正在搜尋有來源與授權資料的配樂',
  auditioning: '正在下載候選曲並分析實際音訊',
  editing: 'AI 正在比較配樂，安排敘事、剪點與聲音',
  rendering: '正在本機合成並檢查樣片',
}

export function FinishingSection({ projectId, csrf, directionId, directionUrl, archive = false }: {
  projectId: string; csrf: string; directionId?: string | null; directionUrl?: string | null; archive?: boolean
}) {
  const [status, setStatus] = useState<FinishingStatus | null>(null)
  const [feedback, setFeedback] = useState('')
  const [consent, setConsent] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const run = status?.latest_run
  const active = !!run && activeStates.has(run.state)
  const preflight = status?.preflight
  const result = status?.result
  const music = result?.result?.music

  useEffect(() => {
    let mounted = true
    getFinishing(projectId).then(next => {
      if (mounted) { setStatus(next); setConfirmed(false); setConsent(false) }
    }).catch(caught => { if (mounted) setError(caught instanceof Error ? caught.message : '無法讀取製作狀態') })
    return () => { mounted = false }
  }, [projectId, directionId])

  useEffect(() => {
    if (!active) return
    let mounted = true
    let timer: number
    const poll = async () => {
      try {
        const next = await getFinishing(projectId)
        if (!mounted) return
        setStatus(next)
        if (next.latest_run && activeStates.has(next.latest_run.state)) {
          timer = window.setTimeout(() => void poll(), 1000)
        }
      } catch (caught) {
        if (mounted) {
          setError(caught instanceof Error ? caught.message : '無法讀取製作進度')
          timer = window.setTimeout(() => void poll(), 2500)
        }
      }
    }
    timer = window.setTimeout(() => void poll(), 600)
    return () => { mounted = false; window.clearTimeout(timer) }
  }, [active, projectId])

  async function produce() {
    if (!preflight || !consent || !confirmed || busy || active) return
    setBusy(true); setError(null); setCopied(false)
    try {
      const nextRun = await finishingCommand<NonNullable<FinishingStatus['latest_run']>>(
        csrf, projectId, 'produce', {
          request_id: crypto.randomUUID(), consent: true, direction_confirmed: true,
          context_hash: preflight.context_hash, feedback,
        },
      )
      setStatus(current => current && { ...current, latest_run: nextRun })
      setConsent(false)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '無法開始 AI 製作')
    } finally { setBusy(false) }
  }

  async function cancel() {
    if (!run || busy) return
    setBusy(true); setError(null)
    try {
      const nextRun = await cancelFinishing(csrf, projectId, run.run_id)
      setStatus(current => current && { ...current, latest_run: nextRun })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '無法取消製作')
    } finally { setBusy(false) }
  }

  async function resume() {
    if (!run || busy || active) return
    setBusy(true); setError(null)
    try {
      const nextRun = await resumeFinishing(csrf, projectId, run.run_id)
      setStatus(current => current && { ...current, latest_run: nextRun })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '無法繼續本機輸出')
    } finally { setBusy(false) }
  }

  return <section className="finishing-section" aria-labelledby="finishing-heading">
    <div className="section-title-row"><div>
      <p className="eyebrow">AI 製作 · 自動選曲與剪輯</p>
      <h2 id="finishing-heading">{archive ? '舊版製作紀錄' : '讓 AI 完成這支樣片'}</h2>
      <p>{archive ? '保留先前實驗結果供比較。新的製作請使用上方 AI 工具工作流程。' : 'AI 會結合你的需求、畫面證據與口語，搜尋配樂、比較候選曲，再調整鏡頭、字幕與聲音，直接交付一支樣片。'}</p>
    </div></div>
    {error && <p className="inline-message error" role="alert">{error}</p>}
    {status?.blocked && <p className="preview-render-message warning" role="status">{status.blocked.message}</p>}
    {status?.has_legacy_experiment && !result && <p>舊版的參數試驗紀錄仍保留；這裡會用新的自動製作流程重新產生樣片。</p>}

    {result?.playback_url && <>
      <div className="finishing-comparison">
        {directionUrl && <div><h3>方向樣片</h3><video controls preload="metadata" src={directionUrl} /></div>}
        <div><h3>{result.stale ? '上一個方向的製作樣片' : 'AI 製作樣片'}</h3>
          <video key={result.run_id} controls preload="metadata" src={result.playback_url} />
          <p>{result.result?.summary}</p>
          <p>{result.result?.duration_seconds?.toFixed(1)} 秒 · 已檢查影音與完整解碼，請看片判斷剪輯與聲音。</p>
        </div>
      </div>
      {music && <div className="finishing-music-credit">
        <p className="eyebrow">這次的配樂選擇</p>
        <h3>{music.title} <small>／ {music.provenance.creator}</small></h3>
        <p>{music.reason}</p>
        <details><summary>配樂與現場聲怎麼安排</summary><ul>{music.regions?.map((region,index) => <li key={index}>{region.reason}</li>)}</ul></details>
        <p><a href={music.provenance.source_url} target="_blank" rel="noreferrer">原始來源頁</a> · <a href={music.provenance.license_url} target="_blank" rel="noreferrer">{music.provenance.license === 'cc0' ? 'CC0' : `CC BY ${music.provenance.license_version}` } 授權條款</a></p>
        <p>授權標示來自音樂索引的最新曲目資料；原站條件仍以來源頁為準。下方保留作者、來源、授權連結與本次剪輯說明。</p>
        <details><summary>署名與改作說明</summary><p>{music.provenance.attribution}</p><p>{music.provenance.changes}</p>
          <button className="button ghost" onClick={async () => {
            try { await navigator.clipboard.writeText(`${music.provenance.attribution}\n${music.provenance.changes}`); setCopied(true) }
            catch { setError('無法複製，請直接選取署名文字。') }
          }}>{copied ? '已複製' : '複製署名文案'}</button>
        </details>
      </div>}
      <details className="finishing-decisions"><summary>AI 這次怎麼剪</summary>
        {result.result?.cut_changes?.map((change, index) => <p key={change.clip_id}><strong>鏡頭 {index + 1}</strong>：{change.reason}</p>)}
        {result.result?.music_alternatives?.map(item => <p key={item.title}>未選用《{item.title}》：{item.reason}</p>)}
      </details>
    </>}

    {!archive && <div className="finishing-produce">
      <label htmlFor="finishing-feedback"><strong>{result ? '看完後，告訴 AI 要怎麼改' : '這支樣片想呈現什麼感覺？（選填）'}</strong></label>
      <textarea id="finishing-feedback" maxLength={2000} rows={3} value={feedback}
        disabled={busy || active} onChange={event => setFeedback(event.target.value)}
        placeholder="例如：開頭快一點，中段保留街上的現場聲，配樂要有輕鬆探索的感覺，少放字卡。" />
      {preflight && <div className="finishing-consent">
        <p>會沿用已確認方向，檢視最多 {preflight.maximum_images} 張縮圖與已保存的分析；AI 最多執行 {preflight.maximum_ai_calls} 次。配樂會自動搜尋及下載，搜尋只使用一般音樂風格詞。</p>
        <label><input type="checkbox" checked={confirmed} disabled={busy || active} onChange={event => setConfirmed(event.target.checked)} />確認以目前方向樣片為基礎，讓 AI 調整剪輯與聲音</label>
        <label><input type="checkbox" checked={consent} disabled={busy || active} onChange={event => setConsent(event.target.checked)} />同意本次將縮圖、口語、分析及修改意見傳給 Codex，使用最多兩次 AI 額度，並搜尋下載公開配樂</label>
        <button className="button primary" disabled={busy || active || !consent || !confirmed} onClick={() => void produce()}>
          {result ? '依照意見重新製作' : 'AI 自動製作樣片'}
        </button>
      </div>}
    </div>}
    {active && run && <div className="preview-render-message" role="status">
      <strong>{phases[run.phase] ?? '正在更新製作狀態'}</strong>
      <progress max={1} value={run.progress} />
      <button className="button ghost" disabled={busy || run.state === 'cancelling'} onClick={() => void cancel()}>{run.state === 'cancelling' ? '正在取消…' : '取消這次製作'}</button>
    </div>}
    {run && ['failed','cancelled','interrupted'].includes(run.state) && <p className="inline-message error" role="alert">
      {run.state === 'cancelled' ? '已取消；先前樣片仍保留。' : run.state === 'interrupted' ? '上次製作已中斷，沒有自動重送 AI。' : run.result?.message ?? '這次沒有完成樣片，請稍後重試。'}
    </p>}
    {run?.local_resume_available && <div className="preview-render-message">
      <p>已保留 AI 的製作決策與配樂，可以直接重試本機合成，不會再次呼叫 AI。</p>
      <button className="button" disabled={busy || active} onClick={() => void resume()}>繼續本機輸出（不使用 AI）</button>
    </div>}
    {!!status?.recent_runs.length && <details><summary>本次與近期 AI 用量</summary>{status.recent_runs.map(item => <p key={item.run_id}>
      {item.usage ? `${item.usage.complete ? '完整回報' : '部分回報'}：輸入 ${item.usage.input_tokens.toLocaleString()} · 快取 ${item.usage.cached_input_tokens.toLocaleString()} · 輸出 ${item.usage.output_tokens.toLocaleString()} Tokens` : '實際用量尚未取得，不代表零用量'}
    </p>)}</details>}
  </section>
}
