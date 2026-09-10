import { useEffect, useState } from 'react'
import { evidenceCommand, getEvidence } from './api'
import type { EvidenceItem, EvidenceSummary } from './api'

const states: Record<string, string> = {
  running: '處理中', succeeded: '已完成', failed: '未完成', cancelled: '已停止', interrupted: '已中斷',
}
const errors: Record<string, string> = {
  evidence_disk_full: 'Clipen 儲存磁碟空間不足，請釋放空間後再試；毛片不會被刪除。',
  source_changed: '來源檔案已改變，請重新檢查素材。',
  time_metadata_missing: '缺少可靠的來源時間資訊，未推測時間位置。',
  frame_decode_failed: '無法解碼畫面。', scene_decode_failed: '無法取得場景變化。',
  frame_time_mapping_invalid: '無法可靠對應來源時間，結果未採用。',
  audio_decode_failed: '無法解碼音訊。', evidence_scope_changed: '素材範圍已改變，請重新擷取。',
  outcome_unknown: '中斷時無法確認 AI 是否完成；不會自動重送，可能已使用額度。',
  controller_stopped: 'Clipen 已關閉，已完成的本機成果保留。',
}
function timeAt(item: EvidenceItem, pts: string): string {
  if (!item.source) return '時間未知'
  const ticks = BigInt(pts) - BigInt(item.source.start_pts)
  const seconds = Number(ticks) * item.source.time_base.num / item.source.time_base.den
  return `${seconds.toFixed(2)} 秒`
}

export function EvidenceSection({ projectId, csrf, inventoryId }: { projectId: string; csrf: string; inventoryId: string }) {
  const [summary, setSummary] = useState<EvidenceSummary | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [approvedItem, setApprovedItem] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  const [offset, setOffset] = useState(0)
  const running = summary?.latest?.state === 'running' || summary?.latest?.observations.some(o => o.state === 'running')
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    async function refresh() {
      try {
        const next = await getEvidence(projectId, offset)
        if (!active) return
        setSummary(next)
        setError('')
        if (next.latest?.state === 'running' || next.latest?.observations.some(o => o.state === 'running')) {
          timer = setTimeout(() => void refresh(), 1500)
        }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : '無法讀取分析狀態。')
      }
    }
    void refresh()
    return () => { active = false; clearTimeout(timer) }
  }, [projectId, inventoryId, running, retry, offset])
  async function act(action: 'start' | 'cancel' | 'resume' | 'observe', item?: string) {
    if (busy || !summary) return
    setBusy(true)
    setError('')
    try {
      const payload = action === 'start'
        ? { request_id: crypto.randomUUID(), expected_revision: summary.scope?.project_revision, scope_id: summary.scope?.scope_id }
        : action === 'observe'
          ? { run_id: summary.latest?.run_id, request_id: crypto.randomUUID(), item_key: item, consent: true }
          : { run_id: summary.latest?.run_id }
      await evidenceCommand(projectId, csrf, action, payload)
      setSummary(await getEvidence(projectId, offset))
      setApprovedItem(null)
      setRetry(v => v + 1)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失敗，請重新讀取狀態。')
    } finally { setBusy(false) }
  }
  const latest = summary?.latest
  const availableCalls = Math.max(0, (summary?.limits.ai_calls_per_run ?? 3) - (latest?.observations.length ?? 0))
  return <section className="analysis-section evidence-section" aria-labelledby="evidence-title">
    <div className="analysis-heading"><div><p className="eyebrow">畫面與聲音證據</p><h2 id="evidence-title">先看取樣，再決定是否交給 AI</h2></div></div>
    <p>本機擷取不消耗 AI 額度，也不修改毛片；未變更的成果會重用。每個影片串流最多取 8 張縮圖；場景變化與音訊能量僅檢查前 120 秒。這不是完整影片理解。本機語音辨識若正在執行，擷取會等待資源。</p>
    {error && <div role="alert"><p>{error}</p><button className="button secondary" type="button" onClick={() => setRetry(v => v + 1)}>重新讀取狀態</button></div>}
    {!summary && !error && <p role="status">正在讀取證據分析狀態…</p>}
    {summary?.blocked_reason && <p>{summary.blocked_reason}</p>}
    {summary?.scope && <div className="evidence-actions">
      <span>{summary.scope.total} 個影音串流 · 最多 {summary.scope.video_streams * 8} 張縮圖</span>
      <button className="button primary" type="button" disabled={busy || running} onClick={() => void act('start')}>{latest ? '更新本機證據' : '擷取本機證據'}</button>
      {running && <button className="button secondary" type="button" disabled={busy} onClick={() => void act('cancel')}>停止分析</button>}
      {latest && !latest.stale && ['interrupted', 'cancelled'].includes(latest.state) && <button className="button secondary" type="button" disabled={busy || running} onClick={() => void act('resume')}>接續本機擷取</button>}
    </div>}
    {latest && <>
      <p role="status">本機擷取：{states[latest.state] ?? latest.state} · {latest.completed_count} / {latest.total} 個串流已處理 · {latest.issue_count} 個需注意</p>
      {latest.state === 'running' && latest.progress.display_name && <p role="status">正在處理 {latest.progress.display_name}：{latest.progress.phase === 'frames' ? `取樣畫面 ${latest.progress.frames} / 8` : latest.progress.phase === 'scenes' ? '偵測前 120 秒的場景變化' : '量測前 120 秒的音訊能量'}</p>}
      {latest.state === 'running' && <progress value={latest.completed_count} max={latest.total} aria-label="本機證據擷取進度" />}
      {latest.error_code && <p>{errors[latest.error_code] ?? '未完成，已保存的成果仍保留。'}</p>}
      {latest.stale && <p role="alert">以下是舊素材範圍的結果，不能再送出 AI 觀察。請重新擷取。</p>}
      <p>AI 觀察剩餘 {availableCalls} 次；每次最多 8 張縮圖、120 秒。Token 用量不等於帳號 Quota 百分比，無法保證固定額度成本。</p>
      {latest.total > latest.page_size && <nav className="evidence-actions" aria-label="證據結果分頁">
        <button className="button secondary" type="button" disabled={offset === 0 || busy} onClick={() => setOffset(v => Math.max(0, v - latest.page_size))}>上一頁</button>
        <span>第 {Math.floor(offset / latest.page_size) + 1} / {Math.ceil(latest.total / latest.page_size)} 頁</span>
        <button className="button secondary" type="button" disabled={offset + latest.page_size >= latest.total || busy} onClick={() => setOffset(v => v + latest.page_size)}>下一頁</button>
      </nav>}
      {latest.results.length === 0 && latest.state === 'running' && <p>此頁的串流尚待處理。</p>}
      {latest.results.map(item => {
        const observation = latest.observations.find(o => o.item_key === item.key)
        return <details className="evidence-item" key={item.key}>
          <summary>{item.role === 'reference' ? '範例作品' : '毛片'} · {item.display_name} · {item.kind === 'video' ? `${item.frames.length} 張畫面取樣` : '音訊能量'}{item.error_code ? ' · 需注意' : ''}</summary>
          {item.error_code ? <p role="alert">{errors[item.error_code] ?? '無法擷取此串流，請確認來源磁碟可讀取。'}</p> : <>
            {item.kind === 'video' ? <>
              <p>畫面：僅在下列時間點有取樣。語音請見本機逐字稿分析區；圖片無法證明聲音或畫面間的動作；預覽色彩不適合用來判斷精確調色。</p>
              <div className="evidence-frames">{item.frames.map((frame, index) => <figure key={frame.artifact_id}>
                <img loading="lazy" width="240" height="160" src={`/api/projects/${projectId}/evidence/${latest.run_id}/images/${frame.artifact_id}`} alt={`${item.display_name}，${timeAt(item, frame.pts)}的取樣畫面`} />
                <figcaption>{index + 1} · {timeAt(item, frame.pts)}</figcaption>
              </figure>)}</div>
              <p>{item.scene_error_code ? '場景變化分析未完成；已取得的畫面取樣仍保留。' : <>前 120 秒內偵測到 {item.scenes?.candidate_pts.length ?? 0} 個像素變化候選點{item.scenes?.candidate_limit_reached ? '（已達 64 點上限）' : ''}，不代表語意換場。</>}</p>
            </> : <p>聲音：已量測 {item.audio_features?.windows.length ?? 0} 個音訊能量視窗，其中 {item.audio_features?.windows.filter(w => w.low_energy).length ?? 0} 個低能量視窗；未判斷音樂、人聲或音效種類。</p>}
            <p>連續覆蓋：{item.coverage?.complete ? '此聲音串流的能量量測完成' : '仍有未分析範圍'}。</p>
            {!!item.coverage?.uncovered.length && <p>未連續分析：{item.coverage.uncovered.map(r => `${timeAt(item, r.start_pts)}～${timeAt(item, r.end_pts)}`).join('、')}</p>}
            {item.kind === 'video' && !observation && !latest.stale && <>
              {approvedItem === item.key ? <div className="evidence-consent">
                <p>將傳送上方 {item.frames.length} 張縮圖給 Codex，包含圖片中可見的私人資訊。不會傳送原始影片、音訊、完整逐字稿或 Repo。這會使用 AI 額度；失敗不會自動重試。</p>
                <button className="button primary" type="button" disabled={busy || running} onClick={() => void act('observe', item.key)}>同意傳送並開始觀察</button>
                <button className="button secondary" type="button" disabled={busy} onClick={() => setApprovedItem(null)}>先不要</button>
              </div> : <button className="button secondary" type="button" disabled={busy || running || latest.state !== 'succeeded' || availableCalls === 0 || !summary?.provider_available} onClick={() => setApprovedItem(item.key)}>讓 AI 觀察這批畫面</button>}
            </>}
          </>}
          {observation && <div className="evidence-observation">
            <h3>AI 圖片觀察：{states[observation.state] ?? observation.state}</h3>
            {observation.error_code && <p>{errors[observation.error_code] ?? '此次觀察未完成，不會自動重送。'}</p>}
            <p>{observation.usage ? `實際 input ${observation.usage.input_tokens.toLocaleString()} · cached input ${observation.usage.cached_input_tokens.toLocaleString()} · output ${observation.usage.output_tokens.toLocaleString()} tokens` : '尚無可確認的 Token 用量，不代表沒有消耗。'}</p>
            {observation.result && <>
              <ol>{observation.result.observations.map(o => <li key={o.index}>{o.description}<p>不確定處：{o.uncertainty}</p></li>)}</ol>
              <h4>{item.role === 'reference' ? '範例風格證據' : '畫面風格觀察'}</h4><p>{observation.result.style_summary}</p>
              <p>限制：{observation.result.limitations}</p>
            </>}
          </div>}
        </details>
      })}
    </>}
  </section>
}
