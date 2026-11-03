import { PowerNotice } from './PowerNotice'
import { useEffect, useRef, useState } from 'react'
import { cancelEditAgent, getEditAgent, getProductionSkills, resumeProduction, startProduction } from './api'
import type { EditAgentStatus, ProductionSkill } from './api'
import { ProductionStatusPanel } from './ProductionStatusPanel'
import { ProductionMusicPicker } from './ProductionMusicPicker'
import { ProductionWorkbench } from './ProductionWorkbench'
import { PromptHistory } from './PromptHistory'
import { SkillPicker } from './SkillPicker'
import { StudioIcon } from './StudioIcon'
import type { StudioIconName } from './StudioIcon'
import { usePromptDraft } from './usePromptDraft'
import { useProvider } from './providerContext'
import { clock } from './reviewModel'

type Props = { projectId: string; csrf: string; brief: string; sourceReady: boolean }

export function ProductionSection({ projectId, csrf, brief, sourceReady }: Props) {
  const provider = useProvider()
  const [skills, setSkills] = useState<ProductionSkill[]>([])
  const [musicId, setMusicId] = useState('')
  const [skillId, setSkillId] = useState('travel-program')
  const [visualStyleId, setVisualStyleId] = useState('tpl-travel-vlog')
  const [templateId, setTemplateId] = useState('travel-vlog')
  const [fontStyleId, setFontStyleId] = useState('variety-tw')
  const [targetMinutes, setTargetMinutes] = useState('10')
  const targetDuration = Math.round(Number(targetMinutes) * 60)
  const durationValid = targetMinutes.trim() !== '' && Number.isFinite(targetDuration) && targetDuration >= 30 && targetDuration <= 1200
  const [intent, setIntent] = usePromptDraft(projectId, 'production', brief || '以人物對話與實際體驗串起一段旅遊節目，有清楚故事、節目字幕、配樂、適合的轉場、節目主標、動態大字與有設計感的畫面內動畫。保留人物與現場，不使用獨立字卡代替敘事。')
  const [status, setStatus] = useState<EditAgentStatus | null>(null)
  const [consent, setConsent] = useState(false)
  const [briefOpen, setBriefOpen] = useState(false)
  const [view, setView] = useState<'overview' | 'edit' | 'new'>('overview')
  const [showDraft, setShowDraft] = useState(false)
  const overviewPlayer = useRef<HTMLVideoElement>(null)
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
  // Show the last finished film by default; a newer, unfinished draft is opt-in and labelled.
  const finished = production?.finished_preview ?? null
  const draft = production?.draft_preview ?? null
  const viewingDraft = Boolean(draft && (showDraft || !finished))
  const fallback = latestPreview?.state === 'succeeded' ? latestPreview : production?.last_successful_preview ?? latestPreview
  const preview = viewingDraft ? draft : finished ?? fallback
  const shownVersion = viewingDraft ? draft?.version : preview === finished ? finished?.version : null
  const showingPreviousPreview = !viewingDraft && preview?.state === 'succeeded' && preview.render_id !== latestPreview?.render_id && !draft
  const timeline = production?.draft.timeline
  const components = timeline?.tracks.filter((track) => track.track_id === 'program-components' || track.track_id.startsWith('program-components-')).reduce((total, track) => total + track.items.length, 0) ?? 0
  const captions = timeline?.tracks.find((track) => track.track_id === 'agent-subtitles')?.items.length ?? 0
  const visualStyles = skills.find((skill) => skill.skill_id === skillId)?.visual_styles ?? []
  const fontStyles = skills.find((skill) => skill.skill_id === skillId)?.font_styles ?? []
  const displayedFont = active ? production?.skill.selected_font_style_id ?? 'default' : fontStyleId
  const selectedFont = fontStyles.find((font) => font.font_style_id === displayedFont)
  const displayedStyle = active ? production?.skill.selected_visual_style_id ?? 'variety' : visualStyleId
  const duration = timeline ? timeline.duration_frames * timeline.frame_rate.den / timeline.frame_rate.num : 0
  useEffect(() => { if (view !== 'overview') overviewPlayer.current?.pause() }, [view])

  function reuseSettings() {
    if (!production) return
    const style = production.skill.selected_visual_style_id ?? 'tpl-travel-vlog'
    const owner = skills.find((s) => s.skill_id === production.skill.skill_id) ?? skills.find((s) => s.visual_styles?.some((v) => v.visual_style_id === style))
    if (!owner) { setError('目前影片的樣板已無法用於新製作，請選擇其他樣板。'); return }
    setSkillId(owner.skill_id); setVisualStyleId(style)
    setTemplateId(owner.templates?.find((t) => t.visual_style_id === style)?.template_id ?? '')
    setFontStyleId(production.skill.selected_font_style_id ?? 'default')
    setTargetMinutes(String(Number(((production.intent.target_duration_seconds ?? 60) / 60).toFixed(3))))
    setIntent(production.intent.brief); setMusicId(production.intent.music_choice?.asset_id ?? ''); setConsent(false)
  }

  return <section className="production-workspace" aria-labelledby="production-heading" onPlayCapture={(event) => {
    event.currentTarget.querySelectorAll('video').forEach((video) => { if (video !== event.target) video.pause() })
  }}>
    <h2 id="production-heading" className="sr-only">AI 節目製作</h2>
    {production && <nav className="studio-view-nav" aria-label="製作工作區">{([
      ['overview', '影片總覽', 'play'], ['edit', '審片修改', 'edit'], ['new', '新片設定', 'plus'],
    ] as const).map(([id, label, icon]) => <button key={id} type="button" aria-pressed={view === id} onClick={() => { setView(id); if (id === 'new') setBriefOpen(true) }}><StudioIcon name={icon as StudioIconName} />{label}</button>)}</nav>}
    {error && <p className="inline-message error" role="alert">{error}</p>}
    {loading && <p role="status">正在讀取製作工作…</p>}
    {connectionError && <div className="production-connection-notice" role="status"><strong>暫時讀不到最新製作狀態</strong>
      <p>以下保留上次讀取的資料；這不代表 AI 已停止。正在自動重新連線。</p>
      <button type="button" className="button secondary" onClick={() => setRefreshKey((value) => value + 1)}>立即重試連線</button>
    </div>}
    {status?.production && (view === 'overview' || active || ['failed', 'interrupted'].includes(run?.state ?? '')) && <ProductionStatusPanel status={status} busy={busy}
      onView={() => setView('overview')} onRefine={timeline && timeline.duration_frames > 0 ? () => setView('edit') : undefined} onResume={() => void resume()} onCancel={() => void cancel()} onRefresh={() => setRefreshKey((value) => value + 1)}
      onEdit={() => {
        setConsent(false); setBriefOpen(true); setView('new')
        window.requestAnimationFrame(() => document.getElementById('production-intent')?.focus())
      }} />}
    {production && <div className="production-output studio-screen" id="production-output" tabIndex={-1} hidden={view !== 'overview'}>
      <div className="screen-heading"><h3>你的影片</h3><span className="studio-version">{shownVersion ? `版本 ${shownVersion.number}` : viewingDraft ? '未完成的草稿' : '目前草稿'} · {Math.floor(duration / 60)}:{String(Math.round(duration % 60)).padStart(2, '0')}</span></div>
      {preview?.state === 'succeeded' ? <>
        {viewingDraft && <div className="film-draft-notice" role="status"><strong>未完成的草稿{draft?.version?.kind === 'ai_partial' ? `：${draft.version.label}` : ''}</strong>
          <p>{active ? 'AI 還在製作，這是製作途中的畫面，還沒做完交付檢查。' : 'AI 還沒做完這一輪的交付檢查，畫面可能只改了一部分。'}</p>
          {finished && <button type="button" className="button secondary" onClick={() => setShowDraft(false)}>回到完成的版本</button>}</div>}
        {!viewingDraft && draft && <div className="film-draft-offer"><span>{active ? 'AI 製作中，有一份還沒完成的草稿。' : `有一份比這版新、但還沒完成的草稿${draft.version?.kind === 'ai_partial' ? `（${draft.version.label}）` : ''}。`}</span>
          <button type="button" className="button ghost" onClick={() => setShowDraft(true)}>看未完成的草稿</button></div>}
        {showingPreviousPreview && <p className="inline-message" role="status">{latestPreview && ['queued', 'running'].includes(latestPreview.state) ? '新預覽正在輸出，先保留上一份成功影片。' : '這次預覽未完成，上一份成功影片仍可播放與下載。'}</p>}
        {preview.stale && <p className="inline-message">這是先前修訂的預覽，目前草稿尚有更新。</p>}
        <video ref={overviewPlayer} key={preview.render_id} controls preload="metadata" src={preview.media_url} aria-label="影片預覽" />
        <div className="screen-actions"><span><span className="screen-ready-dot" />{viewingDraft ? '未完成的草稿' : preview.stale || showingPreviousPreview ? '先前版本' : finished ? `完成的版本${finished.version ? `：${finished.version.label}` : ''}` : '目前預覽'}</span><a className="button secondary" href={preview.media_url} download><StudioIcon name="download" />下載這版影片</a><button type="button" className="button primary" onClick={() => setView('edit')}><StudioIcon name="edit" />審片並記下修改</button></div>
      </> : <p>{preview && ['queued', 'running'].includes(preview.state) ? '正在合成預覽…' : preview?.state === 'failed' ? '這次預覽輸出失敗，草稿仍保留。' : 'AI 尚未產生可觀看預覽。'}</p>}
      <details className="production-film-info"><summary>影片資訊與配樂來源</summary>
        <p>{production.skill.name} · {production.skill.version}</p>
        <p>{duration.toFixed(1)} 秒 · {captions} 段字幕 · {components} 個畫面元件</p>
        <p>{production.draft.summary}</p>
      {production.production_contents?.music.map((music, index) => <div className="finishing-music-credit" key={`${music.name}-${index}`}>
        <strong>{music.role === 'music' ? '配樂' : '聲音素材'}：{music.name}</strong>
        <p>音源使用 {clock(music.source_offset_seconds ?? 0, true)}–{clock((music.source_offset_seconds ?? 0) + music.duration_seconds, true)}{music.source_duration_seconds != null ? `／原曲 ${clock(music.source_duration_seconds, true)}` : ''}</p>
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
    {production && run && timeline && timeline.duration_frames > 0 && <div hidden={view !== 'edit'}><ProductionWorkbench key={run.run_id} visible={view === 'edit'} runProvider={run.provider} projectId={projectId} csrf={csrf} runId={run.run_id} revisionId={production.draft.revision_id} previews={[latestPreview, draft, finished, production.last_successful_preview]} active={active} onChanged={() => setRefreshKey((value) => value + 1)} musicId={musicId} onMusicChange={setMusicId} /></div>}
    {!loading && <details className="production-editor" hidden={Boolean(production) && view !== 'new'} open={!production || briefOpen}
      onToggle={(event) => setBriefOpen(event.currentTarget.open)}>
      <summary>{production ? active ? '本次製作需求' : '製作下一支影片' : '設定你的影片'}</summary>
      <div className="production-brief">
      {production && !active && <div className="inline-message"><p>這裡設定的是下一支新影片，請確認樣板、片長與需求。</p><button type="button" className="button secondary" disabled={busy} onClick={reuseSettings}>沿用目前影片的設定</button></div>}
      {active && production ? <p className="skill-picker-active">本次使用的剪輯樣板：<strong>{production.skill.name}</strong></p>
        : skills.length > 0 && <SkillPicker skills={skills} skillId={skillId} templateId={templateId} disabled={busy} onChoose={(choice) => {
          setSkillId(choice.skillId); setTemplateId(choice.templateId); setVisualStyleId(choice.visualStyleId); setFontStyleId(choice.fontStyleId)
          if (choice.defaultDurationSeconds && choice.defaultDurationSeconds % 60 === 0) setTargetMinutes(String(choice.defaultDurationSeconds / 60))
        }} />}
      <label htmlFor="production-duration">預計片長（分鐘）</label>
      <input id="production-duration" type="number" min={0.5} max={20} step="any" inputMode="decimal"
        value={active ? Number(production?.intent.target_duration_seconds ?? 60) / 60 : targetMinutes}
        disabled={active || busy} aria-describedby="production-duration-hint" aria-invalid={!active && !durationValid}
        onChange={(event) => setTargetMinutes(event.target.value)} />
      <p id="production-duration-hint">可自訂 0.5–20 分鐘，支援小數。{durationValid ? `目標 ${clock(targetDuration)}，以秒為單位安排。` : ''}</p>
      {!active && !durationValid && <p className="inline-message error" role="alert">請輸入 0.5–20 分鐘。</p>}
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
        <p>{durationValid ? `製作約 ${clock(targetDuration)} 的影片；片長越長，分析、字幕與輸出需要的時間和 AI 額度越多。` : '設定片長後即可開始製作。'}</p>
        <PowerNotice />
        <label className="consent-check"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
          <span>同意使用 {provider.name} 額度分析縮圖與口語、依需要下載本機語音功能，並搜尋及下載有來源與授權資料的配樂。原片與音訊留在本機。</span></label>
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
