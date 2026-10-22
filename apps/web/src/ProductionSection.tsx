import { useEffect, useState } from 'react'
import { cancelEditAgent, getEditAgent, getProductionSkills, resumeProduction, startProduction } from './api'
import type { EditAgentStatus, ProductionSkill } from './api'
import { ProductionStatusPanel } from './ProductionStatusPanel'
import { ProductionMusicPicker } from './ProductionMusicPicker'
import { ProductionWorkbench } from './ProductionWorkbench'
import { PromptHistory } from './PromptHistory'
import { TemplateLibrary } from './TemplateLibrary'
import { StudioIcon } from './StudioIcon'
import type { StudioIconName } from './StudioIcon'
import { usePromptDraft } from './usePromptDraft'

type Props = { projectId: string; csrf: string; brief: string; sourceReady: boolean }

export function ProductionSection({ projectId, csrf, brief, sourceReady }: Props) {
  const [skills, setSkills] = useState<ProductionSkill[]>([])
  const [musicId, setMusicId] = useState('')
  const [skillId, setSkillId] = useState('travel-program')
  const [visualStyleId, setVisualStyleId] = useState('tpl-journal')
  const [templateId, setTemplateId] = useState('journal')
  const [fontStyleId, setFontStyleId] = useState('handwriting')
  const [targetMinutes, setTargetMinutes] = useState('10')
  const targetDuration = Number(targetMinutes) * 60
  const durationValid = targetMinutes.trim() !== '' && Number.isInteger(Number(targetMinutes)) && targetDuration >= 60 && targetDuration <= 1200
  const [intent, setIntent] = usePromptDraft(projectId, 'production', brief || '以人物對話與實際體驗串起一段旅遊節目，有清楚故事、節目字幕、配樂、適合的轉場、節目主標、動態大字與有設計感的畫面內動畫。保留人物與現場，不使用獨立字卡代替敘事。')
  const [status, setStatus] = useState<EditAgentStatus | null>(null)
  const [consent, setConsent] = useState(false)
  const [briefOpen, setBriefOpen] = useState(false)
  const [view, setView] = useState<'overview' | 'edit' | 'ai' | 'new'>('overview')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [connectionError, setConnectionError] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const active = Boolean(status?.latest_run && ['queued', 'running'].includes(status.latest_run.state))
  useEffect(() => {
    let disposed = false
    void getProductionSkills().then((value) => { if (!disposed) setSkills(value) })
      .catch((caught) => { if (!disposed) setError(String(caught)) })
    return () => { disposed = true }
  }, [projectId])
  useEffect(() => {
    let disposed = false
    let timer: number | undefined
    const poll = async () => {
      let retry = false
      try {
        const next = await getEditAgent(projectId)
        if (disposed) return
        setStatus(next); setConnectionError(false)
        setMusicId((value) => value || next.production?.intent.music_choice?.asset_id || '')
        retry = Boolean(next.latest_run && (['queued', 'running'].includes(next.latest_run.state)
          || (next.latest_run.state === 'succeeded' && next.latest_run.provider_attempt_id === null)))
          || Boolean(next.production?.local_jobs.some((job) => job.state === 'running'))
          || Boolean(next.production?.preview && ['queued', 'running'].includes(next.production.preview.state))
      } catch {
        if (disposed) return
        setConnectionError(true); retry = true
      } finally {
        if (!disposed) {
          setLoading(false)
          if (retry) timer = window.setTimeout(() => void poll(), 3000)
        }
      }
    }
    void poll()
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [projectId, refreshKey])

  async function start() {
    if (busy || loading || connectionError || active || !consent || !sourceReady || !intent.trim() || !durationValid) return
    setBusy(true); setError(null)
    try {
      await startProduction(csrf, projectId, skillId, intent, visualStyleId, fontStyleId, targetDuration, musicId || undefined, templateId || undefined)
      setStatus(await getEditAgent(projectId)); setConsent(false); setBriefOpen(false); setView('overview')
    } catch (caught) {
      setConnectionError(true)
      setError(caught instanceof Error ? caught.message : '無法確認是否已啟動，正在重新讀取狀態。')
    } finally { setBusy(false); setRefreshKey((value) => value + 1) }
  }

  async function resume() {
    if (!status?.latest_run || busy || active) return
    setBusy(true); setError(null)
    try {
      await resumeProduction(csrf, projectId, status.latest_run.run_id)
      setStatus(await getEditAgent(projectId)); setBriefOpen(false); setView('overview')
    } catch (caught) { setError(caught instanceof Error ? caught.message : '無法接續製作。') }
    finally { setBusy(false); setRefreshKey((value) => value + 1) }
  }

  async function cancel() {
    if (!status?.latest_run || busy) return
    setBusy(true); setError(null)
    try {
      await cancelEditAgent(csrf, projectId, status.latest_run.run_id)
      setStatus(await getEditAgent(projectId))
    } catch (caught) { setError(caught instanceof Error ? caught.message : '無法取消製作。') }
    finally { setBusy(false); setRefreshKey((value) => value + 1) }
  }

  const production = status?.production
  const run = production ? status?.latest_run : null
  const latestPreview = production?.preview
  const preview = latestPreview?.state === 'succeeded' ? latestPreview : production?.last_successful_preview ?? latestPreview
  const showingPreviousPreview = preview?.state === 'succeeded' && preview.render_id !== latestPreview?.render_id
  const timeline = production?.draft.timeline
  const components = timeline?.tracks.filter((track) => track.track_id === 'program-components' || track.track_id.startsWith('program-components-')).reduce((total, track) => total + track.items.length, 0) ?? 0
  const captions = timeline?.tracks.find((track) => track.track_id === 'agent-subtitles')?.items.length ?? 0
  const visualStyles = skills.find((skill) => skill.skill_id === skillId)?.visual_styles ?? []
  const fontStyles = skills.find((skill) => skill.skill_id === skillId)?.font_styles ?? []
  const displayedFont = active ? production?.skill.selected_font_style_id ?? 'default' : fontStyleId
  const selectedFont = fontStyles.find((font) => font.font_style_id === displayedFont)
  const displayedStyle = active ? production?.skill.selected_visual_style_id ?? 'variety' : visualStyleId
  const duration = timeline ? timeline.duration_frames * timeline.frame_rate.den / timeline.frame_rate.num : 0

  return <section className="production-workspace" aria-labelledby="production-heading">
    <div className="studio-heading"><div><p className="eyebrow">CLIPEN STUDIO</p><h2 id="production-heading">把故事，剪成作品。</h2><p>從第一個鏡頭，到每一個值得留下的細節。</p></div><span className="studio-local"><span />本機工作室</span></div>
    {production && <nav className="studio-view-nav" aria-label="製作工作區">{([
      ['overview', '影片總覽', 'play'], ['edit', '手動剪輯', 'edit'], ['ai', 'AI 打磨', 'sparkle'], ['new', '新片設定', 'plus'],
    ] as const).map(([id, label, icon]) => <button key={id} type="button" aria-pressed={view === id} onClick={() => { setView(id); if (id === 'new') setBriefOpen(true) }}><StudioIcon name={icon as StudioIconName} />{label}</button>)}</nav>}
    {error && <p className="inline-message error" role="alert">{error}</p>}
    {loading && <p role="status">正在讀取製作工作…</p>}
    {connectionError && <div className="production-connection-notice" role="status"><strong>暫時讀不到最新製作狀態</strong>
      <p>以下保留上次讀取的資料；這不代表 AI 已停止。正在自動重新連線。</p>
      <button type="button" className="button secondary" onClick={() => setRefreshKey((value) => value + 1)}>立即重試連線</button>
    </div>}
    {status?.production && (view === 'overview' || active || ['failed', 'interrupted'].includes(run?.state ?? '')) && <ProductionStatusPanel status={status} busy={busy}
      onView={() => setView('overview')} onResume={() => void resume()} onCancel={() => void cancel()} onRefresh={() => setRefreshKey((value) => value + 1)}
      onEdit={() => {
        setConsent(false); setBriefOpen(true); setView('new')
        window.requestAnimationFrame(() => document.getElementById('production-intent')?.focus())
      }} />}
    {production && <div className="production-output studio-screen" id="production-output" tabIndex={-1} hidden={view !== 'overview'}>
      <div className="screen-heading"><div><span className="studio-section-number">01 / PREVIEW</span><h3>你的影片</h3></div><span className="studio-version">版本 {production.draft.ordinal} · {Math.floor(duration / 60)}:{String(Math.round(duration % 60)).padStart(2, '0')}</span></div>
      {preview?.state === 'succeeded' ? <>
        {showingPreviousPreview && <p className="inline-message" role="status">{latestPreview && ['queued', 'running'].includes(latestPreview.state) ? '新預覽正在輸出，先保留上一份成功影片。' : '這次預覽未完成，上一份成功影片仍可播放與下載。'}</p>}
        {preview.stale && <p className="inline-message">這是先前修訂的預覽，目前草稿尚有更新。</p>}
        <video key={preview.render_id} controls preload="metadata" src={preview.media_url} aria-label="影片預覽" />
        <div className="screen-actions"><span><span className="screen-ready-dot" />{preview.stale || showingPreviousPreview ? '先前版本' : '目前預覽'}</span><a className="button secondary" href={preview.media_url} download><StudioIcon name="download" />下載這版影片</a><button type="button" className="button primary" onClick={() => setView('edit')}><StudioIcon name="edit" />繼續剪輯</button></div>
      </> : <p>{preview && ['queued', 'running'].includes(preview.state) ? '正在合成預覽…' : preview?.state === 'failed' ? '這次預覽輸出失敗，草稿仍保留。' : 'AI 尚未產生可觀看預覽。'}</p>}
      <details className="production-film-info"><summary>影片資訊與配樂來源</summary>
        <p>{production.skill.name} · {production.skill.version}</p>
        <p>版本 {production.draft.ordinal} · {duration.toFixed(1)} 秒 · {captions} 段字幕 · {components} 個畫面元件</p>
        <p>{production.draft.summary}</p>
      {production.production_contents?.music.map((music, index) => <div className="finishing-music-credit" key={`${music.name}-${index}`}>
        <strong>{music.role === 'music' ? '配樂' : '聲音素材'}：{music.name}</strong>
        <p>{music.provenance.creator ? `作者：${music.provenance.creator} · ` : ''}{music.provenance.license === 'by' ? `CC BY ${music.provenance.license_version ?? ''}` : music.provenance.license === 'cc0' ? 'CC0' : music.provenance.license === 'user_supplied' ? '使用者提供' : music.provenance.license ?? '授權條件尚未取得'}</p>
        {music.provenance.attribution && <p>署名內容：{music.provenance.attribution}</p>}
        {music.provenance.changes && <p>使用方式：{music.provenance.changes}</p>}
        {music.provenance.source_url && <a href={music.provenance.source_url} target="_blank" rel="noreferrer">曲目來源</a>}{' '}
        {music.provenance.license_url && <a href={music.provenance.license_url} target="_blank" rel="noreferrer">授權條件</a>}
      </div>)}
      {production.export_url && <a className="button ghost" href={production.export_url} download>下載剪輯資料</a>}
      {run?.usage && <p>最近一次 AI 執行用量：輸入 {run.usage.input_tokens.toLocaleString()}、快取輸入 {run.usage.cached_input_tokens.toLocaleString()}、輸出 {run.usage.output_tokens.toLocaleString()} Tokens。</p>}
      {run && !run.usage && <p>實際 AI 用量尚未取得，不代表零用量。</p>}
      </details>
    </div>}
    {production && run && timeline && timeline.duration_frames > 0 && <div hidden={view !== 'edit' && view !== 'ai'}><ProductionWorkbench key={run.run_id} mode={view === 'ai' ? 'ai' : 'manual'} onRefine={() => setView('ai')} projectId={projectId} csrf={csrf} runId={run.run_id} revisionId={production.draft.revision_id} preview={preview ?? null} active={active} onChanged={() => setRefreshKey((value) => value + 1)} musicId={musicId} onMusicChange={setMusicId} /></div>}
    {!loading && <details className="production-editor" hidden={Boolean(production) && view !== 'new'} open={!production || briefOpen}
      onToggle={(event) => setBriefOpen(event.currentTarget.open)}>
      <summary>{production ? active ? '本次製作需求' : '製作下一支影片' : '設定你的影片'}</summary>
      <div className="production-brief">
      <label htmlFor="production-skill">影片類型</label>
      <select id="production-skill" value={skillId} disabled={Boolean(active) || busy} onChange={(event) => setSkillId(event.target.value)}>
        {skills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>{skill.name}</option>)}
      </select>
      <label htmlFor="production-duration">預計片長（分鐘）</label>
      <input id="production-duration" type="number" min={1} max={20} step={1} inputMode="numeric"
        value={active ? Number(production?.intent.target_duration_seconds ?? 60) / 60 : targetMinutes}
        disabled={active || busy} aria-describedby="production-duration-hint" aria-invalid={!active && !durationValid}
        onChange={(event) => setTargetMinutes(event.target.value)} />
      <p id="production-duration-hint">可自訂 1–20 分鐘。AI 會依素材安排接近目標片長的影片。</p>
      {!active && !durationValid && <p className="inline-message error" role="alert">請輸入 1–20 的整數分鐘。</p>}
      {Boolean(skills.find((skill) => skill.skill_id === skillId)?.templates?.length) && <TemplateLibrary skillId={skillId} templates={skills.find((skill) => skill.skill_id === skillId)!.templates!} selectedId={active ? (production?.intent as { template?: { template_id: string } })?.template?.template_id ?? '' : templateId} disabled={active || busy} onSelect={(template) => { setTemplateId(template.template_id); setVisualStyleId(template.visual_style_id); setFontStyleId(template.font_style_id) }} />}
      <details className="template-customize"><summary>自訂畫面風格與字體</summary>
      {visualStyles.length > 0 && <><label htmlFor="production-visual-style">畫面風格</label><select id="production-visual-style" value={displayedStyle} disabled={active || busy} onChange={(e) => { setVisualStyleId(e.target.value); setTemplateId(skills.find((skill) => skill.skill_id === skillId)?.templates?.find((template) => template.visual_style_id === e.target.value)?.template_id ?? '') }}>{visualStyles.map((style) => <option value={style.visual_style_id} key={style.visual_style_id}>{style.name}</option>)}</select></>}
      {fontStyles.length > 0 && <div className="font-style-picker">
        <label htmlFor="production-font">字體搭配</label>
        <select id="production-font" value={displayedFont} disabled={active || busy} onChange={(event) => setFontStyleId(event.target.value)}>
          {fontStyles.map((font) => <option key={font.font_style_id} value={font.font_style_id}>{font.name}</option>)}
        </select>
        {selectedFont && <p>{selectedFont.description}</p>}
        {selectedFont?.preview_resource && <img src={`/api/production/skills/${encodeURIComponent(skillId)}/fonts/${encodeURIComponent(displayedFont)}/preview`} alt={`${selectedFont.name}主標、重點字與資訊字體預覽`} />}
        <p>主標與重點大字使用所選字體，對話字幕保持清楚易讀。落雁體與芫荽體已內附。</p>
      </div>}
      </details>
      <ProductionMusicPicker projectId={projectId} csrf={csrf} value={musicId} onChange={setMusicId} disabled={active || busy} />
      <label htmlFor="production-intent">這次想做什麼影片？</label>
      <textarea id="production-intent" value={active ? production?.intent.brief ?? intent : intent} maxLength={8000} rows={4} disabled={Boolean(active) || busy} onChange={(event) => setIntent(event.target.value)} />
      <PromptHistory projectId={projectId} csrf={csrf} kind="production" text={intent} onUse={setIntent} disabled={active || busy} />
      {!sourceReady && <p className="inline-message" role="status">目前無法使用來源素材。請回到「素材」頁確認磁碟位置與檢查結果，再開始製作。</p>}
      {!active && <>
        {production && <p>這會依下方設定製作一支新影片，目前版本會保留。</p>}
        <p>{durationValid ? `製作約 ${Number(targetMinutes)} 分鐘的影片；片長越長，分析、字幕與輸出需要的時間和 AI 額度越多。` : '設定片長後即可開始製作。'}</p>
        <label className="consent-check"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
          <span>同意使用 Codex 額度分析縮圖與口語、依需要下載本機語音功能，並搜尋及下載有來源與授權資料的配樂。原片與音訊留在本機。</span></label>
        <button className="button primary" disabled={busy || loading || connectionError || !sourceReady || !consent || !intent.trim() || !durationValid || skills.length === 0} onClick={() => void start()}>{busy ? '正在啟動…' : '開始製作'}</button>
      </>}
    </div></details>}
    {status?.result && status.result.run_id !== status.latest_run?.run_id && <details className="production-output">
      <summary>上一次完成的版本</summary>
      <p>這版已保留供比較，仍需依實際觀看結果驗收。</p>
      {status.result.preview_url && <video controls preload="metadata" src={status.result.preview_url} aria-label="上一次完成版本" />}
      {status.result.export_url && <a href={status.result.export_url} download>下載這版可編輯時間軸</a>}
    </details>}
  </section>
}
