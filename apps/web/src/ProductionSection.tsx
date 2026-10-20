import { useEffect, useState } from 'react'
import { cancelEditAgent, getEditAgent, getProductionSkills, startProduction } from './api'
import type { EditAgentStatus, ProductionSkill } from './api'

type Props = { projectId: string; csrf: string; brief: string; sourceReady: boolean }

export function ProductionSection({ projectId, csrf, brief, sourceReady }: Props) {
  const [skills, setSkills] = useState<ProductionSkill[]>([])
  const [skillId, setSkillId] = useState('travel-program')
  const [intent, setIntent] = useState(brief || '以人物對話與實際體驗串起一段旅遊節目，有清楚故事、節目字幕、配樂、適合的轉場與畫面內後製元素，不使用獨立字卡。')
  const [status, setStatus] = useState<EditAgentStatus | null>(null)
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const active = Boolean(status?.latest_run && ['queued', 'running'].includes(status.latest_run.state))
  useEffect(() => {
    let disposed = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const next = await getEditAgent(projectId)
        if (disposed) return
        setStatus(next)
        const pending = next.latest_run && (['queued', 'running'].includes(next.latest_run.state)
          || (next.latest_run.state === 'succeeded' && next.latest_run.provider_attempt_id === null))
        if (pending || next.production?.local_jobs.some((job) => job.state === 'running')
          || (next.production?.preview && ['queued', 'running'].includes(next.production.preview.state))) {
          timer = window.setTimeout(() => void poll(), 2000)
        }
      } catch (caught) {
        if (!disposed) setError(caught instanceof Error ? caught.message : '無法讀取製作狀態。')
      } finally {
        if (!disposed) setLoading(false)
      }
    }
    void getProductionSkills().then((value) => { if (!disposed) setSkills(value) })
      .catch((caught) => { if (!disposed) setError(String(caught)) })
    void poll()
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [projectId, active])

  async function start() {
    if (busy || active || !consent || !sourceReady || !intent.trim()) return
    setBusy(true); setError(null)
    try {
      await startProduction(csrf, projectId, skillId, intent)
      setStatus(await getEditAgent(projectId)); setConsent(false)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '無法開始製作。')
    } finally { setBusy(false) }
  }

  async function cancel() {
    if (!status?.latest_run || busy) return
    setBusy(true); setError(null)
    try {
      await cancelEditAgent(csrf, projectId, status.latest_run.run_id)
      setStatus(await getEditAgent(projectId))
    } catch (caught) { setError(caught instanceof Error ? caught.message : '無法取消製作。') }
    finally { setBusy(false) }
  }

  const production = status?.production
  const run = production ? status?.latest_run : null
  const preview = production?.preview
  const timeline = production?.draft.timeline
  const components = timeline?.tracks.find((track) => track.track_id === 'program-components')?.items.length ?? 0
  const captions = timeline?.tracks.find((track) => track.track_id === 'agent-subtitles')?.items.length ?? 0
  const duration = timeline ? timeline.duration_frames * timeline.frame_rate.den / timeline.frame_rate.num : 0

  return <section className="production-workspace" aria-labelledby="production-heading">
    <div className="panel-heading"><div><span className="eyebrow">SKILL · AI 製作</span><h2 id="production-heading">從素材製作你的節目</h2>
      <p>AI 依節目風格探索素材、安排故事，製作字幕、配樂、剪輯與畫面元素，再預覽修改。</p></div></div>
    {error && <p className="inline-message error" role="alert">{error}</p>}
    {loading && <p role="status">正在讀取製作工作…</p>}
    <div className="production-brief">
      <label htmlFor="production-skill">節目風格</label>
      <select id="production-skill" value={skillId} disabled={Boolean(active) || busy} onChange={(event) => setSkillId(event.target.value)}>
        {skills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>{skill.name} · {skill.version}</option>)}
      </select>
      <p>統一敘事、字幕、資訊標註、箭頭圈選、反應動態與聲音風格。</p>
      <label htmlFor="production-intent">這次想做什麼影片？</label>
      <textarea id="production-intent" value={intent} maxLength={8000} rows={4} disabled={Boolean(active) || busy} onChange={(event) => setIntent(event.target.value)} />
      {!active && <>
        <p>先製作 30–75 秒節目樣片。無須先完成舊分析或確認剪輯計畫；最多 120 次工具操作、4 次本機預覽。</p>
        <label className="consent-check"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
          <span>同意使用 Codex 額度分析縮圖與口語、依需要下載本機語音模型，並搜尋及下載有來源與授權資料的配樂。原片與音訊留在本機。</span></label>
        <button className="button primary" disabled={busy || loading || !sourceReady || !consent || !intent.trim() || skills.length === 0} onClick={() => void start()}>{busy ? '正在啟動…' : '依這套風格製作節目'}</button>
      </>}
    </div>
    {active && <div className="edit-agent-running" role="status"><strong>AI 正在製作</strong>
      <p>已使用 {status?.latest_run?.budget.tool_calls} 次工具操作；製作中的版本與預覽會保留在下方。</p>
      {production?.local_jobs.filter((job) => job.state === 'running').map((job) => <p key={job.job_id}>
        {job.kind === 'setup' ? '正在準備本機語音模型' : job.kind === 'align' ? '正在對齊來源字詞時間' : '正在轉寫來源口語'}
        {job.total_bytes ? ` · ${Math.round((job.completed_bytes ?? 0) / job.total_bytes * 100)}%` : '…'}
      </p>)}
      <button className="button ghost" disabled={busy} onClick={() => void cancel()}>停止並保留草稿</button></div>}
    {production && <div className="production-output">
      <div><span className="eyebrow">{production.skill.name} · {production.skill.version}</span>
        <h3>{run?.state === 'succeeded' ? '樣片待你驗收' : active ? '製作中的草稿' : '已保留製作草稿'}</h3>
        <p>版本 {production.draft.ordinal} · {duration.toFixed(1)} 秒 · {captions} 段字幕 · {components} 個畫面元件</p>
        <p>{production.draft.summary}</p>
        {run?.error_code && <p role="alert">這次製作未完成：{run.error_code}。草稿已保留，未自動重送 AI 工作。</p>}
      </div>
      {production.validation && !production.validation.valid && <div className="inline-message" role="status">
        <strong>這版仍有待完成項目</strong><ul>{production.validation.issues.map((issue, index) => <li key={`${issue.code}-${index}`}>
          {issue.message ?? ({ program_component_collision: '畫面元件互相重疊，需要調整位置或時間。', component_anchor_missing: '畫面元件失去來源錨點，需要修正。', text_layout_collision: '字幕或文字互相重疊，需要重新排版。' }[issue.code] ?? '這版有需修正的聲畫或來源問題。')}
        </li>)}</ul>
      </div>}
      {preview?.state === 'succeeded' ? <>
        {preview.stale && <p className="inline-message">這是先前修訂的預覽，目前草稿尚有更新。</p>}
        <video key={preview.render_id} controls preload="metadata" src={preview.media_url} aria-label="節目樣片預覽" />
        <a className="button ghost" href={preview.media_url} download>下載這版樣片</a>
      </> : <p>{preview && ['queued', 'running'].includes(preview.state) ? '正在合成預覽…' : preview?.state === 'failed' ? '這次預覽輸出失敗，草稿仍保留。' : 'AI 尚未產生可觀看預覽。'}</p>}
      {production.production_contents?.music.map((music, index) => <div className="finishing-music-credit" key={`${music.name}-${index}`}>
        <strong>{music.role === 'music' ? '配樂' : '聲音素材'}：{music.name}</strong>
        <p>{music.provenance.creator ? `作者：${music.provenance.creator} · ` : ''}{music.provenance.license === 'by' ? `CC BY ${music.provenance.license_version ?? ''}` : music.provenance.license === 'cc0' ? 'CC0' : music.provenance.license ?? '授權條件尚未取得'}</p>
        {music.provenance.attribution && <p>署名內容：{music.provenance.attribution}</p>}
        {music.provenance.changes && <p>使用方式：{music.provenance.changes}</p>}
        {music.provenance.source_url && <a href={music.provenance.source_url} target="_blank" rel="noreferrer">曲目來源</a>}{' '}
        {music.provenance.license_url && <a href={music.provenance.license_url} target="_blank" rel="noreferrer">授權條件</a>}
      </div>)}
      {production.export_url && <a className="button ghost" href={production.export_url} download>下載可編輯時間軸、Skill 與來源紀錄</a>}
      <p>預覽與 AI 自評不代表已通過你的驗收。聲音量測與逐字稿也不代表 AI 已聽過成片。</p>
      {run?.usage && <p>本次用量：輸入 {run.usage.input_tokens.toLocaleString()}、快取輸入 {run.usage.cached_input_tokens.toLocaleString()}、輸出 {run.usage.output_tokens.toLocaleString()} Tokens。</p>}
      {run && !run.usage && <p>實際 AI 用量尚未取得，不代表零用量。</p>}
    </div>}
    {status?.result && status.result.run_id !== status.latest_run?.run_id && <details className="production-output">
      <summary>上一次完成的版本</summary>
      <p>這版已保留供比較，仍需依實際觀看結果驗收。</p>
      {status.result.preview_url && <video controls preload="metadata" src={status.result.preview_url} aria-label="上一次完成版本" />}
      {status.result.export_url && <a href={status.result.export_url} download>下載這版可編輯時間軸</a>}
    </details>}
  </section>
}
