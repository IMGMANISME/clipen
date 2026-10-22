import { useEffect, useId, useState } from 'react'
import { editProduction, exportProduction, getProductionWorkbench, refineProduction, renderProduction } from './api'
import type { EditAgentStatus, EditAgentTimelineItem, ProductionWorkbenchState } from './api'
import { ProductionMusicPicker } from './ProductionMusicPicker'
import { PromptHistory } from './PromptHistory'
import { usePromptDraft } from './usePromptDraft'
import { StudioIcon } from './StudioIcon'
import type { StudioIconName } from './StudioIcon'
import { RefinementReferences } from './RefinementReferences'
import { VisualEditorPreview } from './VisualEditorPreview'

type Props = { mode?: 'all' | 'manual' | 'ai'; onRefine?: () => void; projectId: string; csrf: string; runId: string; revisionId: string; active: boolean; onChanged: () => void; musicId: string; preview: NonNullable<EditAgentStatus['production']>['preview']; onMusicChange: (id: string) => void }
const toolLabels: Record<string, string> = { edit_text: '文字與時間', configure_text_style: '文字外觀', remove_item: '刪除元素', trim_clip: '裁切片段', reorder_clips: '重排片段', split_clip: '分割片段', replace_clip_source: '替換來源', insert_clip: '插入片段', set_source_audio: '原音', set_color_grade: '調色', set_mosaic: '馬賽克', set_transition: '轉場', set_fade: '淡入淡出', remove_clip_effect: '移除效果', place_audio: '音樂與音效', remove_audio: '移除聲音', add_program_component: '新增節目元件', update_program_component: '修改節目元件', remove_program_component: '移除節目元件', restyle_program_components: '更換元件風格與字體', restore_revision: '還原版本' }
export function ProductionWorkbench({ projectId, csrf, runId, revisionId, active, onChanged, musicId, onMusicChange, preview, mode = 'all', onRefine }: Props) {
  const contentId = useId()
  const [collapsedModes, setCollapsedModes] = useState<string[]>([])
  const expanded = !collapsedModes.includes(mode)
  const [data, setData] = useState<ProductionWorkbenchState | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [instruction, setInstruction] = usePromptDraft(projectId, 'refinement')
  const [consent, setConsent] = useState(false)
  const [referenceDirty, setReferenceDirty] = useState(false)
  const [referenceTarget, setReferenceTarget] = useState<{ itemId: string; nonce: number }>()
  const [text, setText] = useState('')
  const [start, setStart] = useState(0)
  const [length, setLength] = useState(1)
  const [trim, setTrim] = useState(0)
  const [x, setX] = useState(0.1)
  const [y, setY] = useState(0.1)
  const [visualStyle, setVisualStyle] = useState('editorial')
  const [fontStyle, setFontStyle] = useState('handwriting')
  const [tool, setTool] = useState('set_transition')
  const [args, setArgs] = useState('{}')
  const [exportUrl, setExportUrl] = useState('')
  const [historyId, setHistoryId] = useState('')
  const [tab, setTab] = useState('video')
  const items = data?.revision.timeline.tracks.flatMap((track) => track.items.map((item) => ({ ...item, track: track.track_id, category: track.kind === 'video' ? 'video' : track.track_id === 'agent-subtitles' ? 'subtitles' : 'graphics' }))) ?? []
  const selected = items.find((item) => item.item_id === selectedId)
  const fps = data ? data.revision.timeline.frame_rate.num / data.revision.timeline.frame_rate.den : 30
  const profile = selected?.style_id ? data?.styles[selected.style_id] : undefined
  useEffect(() => {
    let disposed = false
    void getProductionWorkbench(projectId, runId).then((next) => { if (!disposed) { setData(next); setExportUrl('') } }).catch((e) => { if (!disposed) setError(String(e)) })
    return () => { disposed = true }
  }, [projectId, runId, revisionId])
  function selectElement(id: string) {
    setSelectedId(id)
    const item = items.find((entry) => entry.item_id === id)
    if (!item) return
    const style = item.style_id ? data?.styles[item.style_id] : undefined
    setText(item.text ?? ''); setStart(item.start_frame / fps); setLength(item.duration_frames / fps); setTrim(0)
    setX(Number(style?.x ?? 0.1)); setY(Number(style?.y ?? 0.1))
    setVisualStyle(String(style?.visual_style_id ?? 'editorial')); setFontStyle(String(style?.font_style_id ?? 'handwriting'))
  }
  async function action(work: () => Promise<unknown>, success: string) {
    if (busy || active || !data) return
    setBusy(true); setError(''); setMessage('')
    let completed = false
    try { await work(); setMessage(success); completed = true }
    catch (e) { setError(e instanceof Error ? e.message : '這次修改未完成。') }
    finally {
      try {
        const next = await getProductionWorkbench(projectId, runId); setData(next)
        if (completed) {
          const item = next.revision.timeline.tracks.flatMap((track) => track.items).find((entry) => entry.item_id === selectedId)
          if (item) {
            const style = item.style_id ? next.styles[item.style_id] : undefined
            setText(item.text ?? ''); setStart(item.start_frame / fps); setLength(item.duration_frames / fps); setTrim(0)
            setX(Number(style?.x ?? .1)); setY(Number(style?.y ?? .1))
          } else setSelectedId('')
        }
      } catch { /* Preserve the last visible draft. */ }
      onChanged(); setBusy(false)
    }
  }
  async function edit(name: string, values: Record<string, unknown>) {
    if (!data) return
    return editProduction(csrf, projectId, runId, data.revision.revision_id, name, values)
  }
  function saveElement() {
    if (!selected) return
    const span = { start_frame: Math.round(start * fps), duration_frames: Math.round(length * fps) }
    if (selected.kind === 'media') {
      void action(() => edit('trim_clip', { item_id: selectedId, start_offset_frames: Math.round(trim * fps), duration_frames: span.duration_frames, summary: '使用者裁切片段' }), '片段已更新；請重新輸出預覽。')
    } else if (profile?.kind === 'program_component') {
      const anchor = items.find((i) => i.item_id === profile.anchor_item_id)
      const [main, ...detail] = text.split('\n')
      void action(() => edit('update_program_component', { element_id: selectedId, item_id: profile.anchor_item_id, component_id: profile.component_id, start_offset_frames: span.start_frame - (anchor?.start_frame ?? 0), duration_frames: span.duration_frames, text: main, ...(detail.length ? { detail: detail.join(' ') } : {}), x, y, ...(profile.target ? { target: profile.target } : {}), evidence: '使用者直接編輯的節目文字與位置。', summary: '使用者修改節目元件' }), '元件已更新；請重新輸出預覽。')
    } else {
      void action(() => edit('edit_text', { item_id: selectedId, ...span, text, style_id: 'title.caption', summary: '使用者修改文字與顯示時間' }), '文字已更新；請重新輸出預覽。')
    }
  }
  function chooseAdvanced(name: string) {
    setTool(name)
    const spec = data?.tools.find((entry) => entry.name === name)
    const template: Record<string, unknown> = {}
    for (const key of spec?.inputSchema.required ?? []) template[key] = key === 'item_id' || key === 'element_id' ? selectedId : key === 'summary' ? '使用者進階修改' : null
    setArgs(JSON.stringify(template, null, 2))
  }
  async function reorder(direction: number) {
    const videos = items.filter((i) => i.category === 'video').map((i) => i.item_id); const at = videos.indexOf(selectedId); const next = at + direction
    if (at < 0 || next < 0 || next >= videos.length) return
    ;[videos[at], videos[next]] = [videos[next], videos[at]]
    await edit('reorder_clips', { item_ids: videos, summary: '使用者調整鏡頭順序' })
  }
  function soundRows() {
    return items.flatMap((item) => (item.effects ?? []).filter((effect) => effect.effect_id === 'audio.layers').flatMap((effect) => ((effect.parameters.layers ?? []) as Array<Record<string, unknown>>).map((layer) => ({ item, layer }))))
  }
  return <section className="production-workbench" aria-labelledby="workbench-heading">
    <div className={`workbench-heading ${expanded ? '' : 'is-collapsed'}`}><div><span className="studio-section-number">{mode === 'ai' ? '03 / REFINE' : '02 / EDIT'}</span><h3 id="workbench-heading"><button type="button" className="workbench-collapse" aria-expanded={expanded} aria-controls={contentId} onClick={() => setCollapsedModes((current) => expanded ? [...current, mode] : current.filter((value) => value !== mode))}><span>{mode === 'ai' ? '讓好作品，再往前一步。' : '繼續編輯這支影片'}</span><span className="workbench-collapse-hint">{expanded ? '收合' : '展開'}<span className={expanded ? 'collapse-chevron is-open' : 'collapse-chevron'} aria-hidden="true" /></span></button></h3><p>{mode === 'ai' ? '補充你知道的細節，讓 AI 在這一版上繼續打磨。' : '看著畫面調整每個細節。修改會另存版本，不使用 AI 額度。'}</p></div></div>
    <div id={contentId} className="workbench-content" hidden={!expanded}>
    {active && <p role="status">AI 正在修改這份影片，完成或停止後即可手動編輯。</p>}
    {error && <p className="inline-message error" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    <fieldset hidden={mode === 'ai'} disabled={busy || active || !data}>
      <legend>手動調整</legend>
      <div className="workbench-tabs" role="group" aria-label="編輯內容">
        {[['video', '畫面剪輯', 'film'], ['subtitles', '字幕', 'text'], ['graphics', '節目元件', 'layers'], ['audio', '音樂與音效', 'audio']].map(([id, label, icon]) => <button key={id} type="button" className={`button ${tab === id ? 'primary' : 'ghost'}`} aria-pressed={tab === id} onClick={() => setTab(id)}><StudioIcon name={icon as StudioIconName} />{label}</button>)}
      </div>
      {tab !== 'audio' ? <div className="workbench-visual-editor">
        {data && <VisualEditorPreview key={`${selectedId}:${data.revision.revision_id}`} projectId={projectId} csrf={csrf} runId={runId} revisionId={data.revision.revision_id} timeline={data.revision.timeline} selected={selected}
          override={selected?.kind === 'text' ? { item_id: selectedId, text, ...(profile?.kind === 'program_component' ? { x, y } : {}) } : undefined}
          canMove={profile?.kind === 'program_component' && !busy && !active} onMove={(nextX, nextY) => { setX(nextX); setY(nextY) }} preview={preview} />}
        <div className="workbench-editor-grid">
        <div className="workbench-items" role="group" aria-label="時間軸元素">{items.filter((item) => item.category === tab).map((item, n) => <button type="button" key={item.item_id} aria-pressed={selectedId === item.item_id} onClick={() => selectElement(item.item_id)}><strong>{item.text || `鏡頭 ${n + 1}`}</strong><span>{(item.start_frame / fps).toFixed(1)}–{((item.start_frame + item.duration_frames) / fps).toFixed(1)} 秒</span></button>)}</div>
        {selected && selected.category === tab ? <div className="workbench-fields">
          {selected.kind !== 'media' && <label>文字<textarea aria-label="文字" value={text} onChange={(e) => setText(e.target.value)} rows={3} /></label>}
          {selected.kind !== 'media' ? <label>開始時間（秒）<input type="number" min={0} step={1 / fps} value={start} onChange={(e) => setStart(Number(e.target.value))} /></label> : <label>從片段開頭裁掉（秒）<input type="number" min={0} step={1 / fps} value={trim} onChange={(e) => setTrim(Number(e.target.value))} /></label>}
          <label>持續時間（秒）<input type="number" min={1 / fps} step={1 / fps} value={length} onChange={(e) => setLength(Number(e.target.value))} /></label>
          {profile?.kind === 'program_component' && <><label>水平位置<input type="range" min={0} max={1} step={0.01} value={x} onChange={(e) => setX(Number(e.target.value))} /></label><label>垂直位置<input type="range" min={0} max={1} step={0.01} value={y} onChange={(e) => setY(Number(e.target.value))} /></label></>}
          {profile?.kind === 'program_component' && <>
            <label>元件風格<select value={visualStyle} onChange={(e) => setVisualStyle(e.target.value)}>{(data?.visual_styles ?? [{ visual_style_id: 'variety', name: '活潑綜藝' }, { visual_style_id: 'editorial', name: '旅誌編輯部' }, { visual_style_id: 'cinema', name: '簡約影誌' }]).map((style) => <option value={style.visual_style_id} key={style.visual_style_id}>{style.name}</option>)}</select></label>
            <label>元件字體<select value={fontStyle} onChange={(e) => setFontStyle(e.target.value)}>{[['default', '跟隨風格'], ['japanese', '日系綜藝'], ['street', '潮流雜誌'], ['travel', '質感旅遊'], ['handwriting', '落雁手寫'], ['calligraphy', '筆意楷書']].map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
            <button type="button" className="button secondary" onClick={() => void action(() => edit('restyle_program_components', { visual_style_id: visualStyle, font_style_id: fontStyle, element_ids: [selectedId], summary: '使用者更換元件設計與字體' }), '這個元件的風格已更新。')}>套用這個元件的風格</button>
          </>}
          <button type="button" className="button ghost" onClick={() => {
            const video = items.find((item) => item.category === 'video' && item.start_frame <= selected.start_frame && item.start_frame + item.duration_frames > selected.start_frame)
            if (video) { setCollapsedModes((current) => current.filter((value) => value !== 'ai' && value !== 'all')); setReferenceTarget((previous) => ({ itemId: video.item_id, nonce: (previous?.nonce ?? 0) + 1 })); onRefine?.(); window.requestAnimationFrame(() => document.getElementById('refinement-references')?.scrollIntoView({ behavior: 'smooth', block: 'start' })) }
          }}>標記此片段供 AI 參考</button>
          <button type="button" className="button secondary" onClick={saveElement}>儲存這個元素</button>
          {selected.kind === 'media' && <div className="workbench-tabs"><button className="button ghost" type="button" onClick={() => void action(() => reorder(-1), '順序已更新。')}>往前移</button><button className="button ghost" type="button" onClick={() => void action(() => reorder(1), '順序已更新。')}>往後移</button></div>}
          <button type="button" className="button ghost" onClick={() => void action(() => edit('remove_item', { item_id: selectedId, summary: '使用者刪除元素' }), '已刪除，可從版本紀錄還原。')}>刪除這個元素</button>
        </div> : <p>選擇一個元素開始編輯。</p>}
      </div></div> : <div className="workbench-sounds">
        {soundRows().map(({ item, layer }) => <AudioLayerEditor key={`${String(layer.layer_id)}-${data?.revision.revision_id}`} fps={fps} item={item} layer={layer} assets={data?.audio_assets ?? []} onSave={(values) => void action(() => edit('place_audio', { item_id: item.item_id, ...values, summary: '使用者調整混音' }), '聲音安排已更新。')} onRemove={() => void action(() => edit('remove_audio', { layer_id: layer.layer_id, summary: '使用者移除音訊' }), '音訊已移除。')} />)}
        {!soundRows().length && <p>這版尚未加入獨立音樂或音效。可在下方指定配樂後請 AI 安排，或使用進階調整加入聲音。</p>}
      </div>}
      <details className="workbench-advanced"><summary>進階調整：效果、樣式與精確參數</summary>
        <label>編輯工具<select value={tool} onChange={(e) => chooseAdvanced(e.target.value)}>{data?.tools.map((entry) => <option value={entry.name} key={entry.name}>{toolLabels[entry.name] ?? entry.name}</option>)}</select></label>
        <p>{data?.tools.find((entry) => entry.name === tool)?.description}</p>
        <label>參數<textarea rows={8} value={args} onChange={(e) => setArgs(e.target.value)} spellCheck={false} /></label>
        <details><summary>參數格式</summary><pre>{JSON.stringify(data?.tools.find((entry) => entry.name === tool)?.inputSchema, null, 2)}</pre></details>
        <button type="button" className="button secondary" onClick={() => void action(() => edit(tool, JSON.parse(args) as Record<string, unknown>), '修改已儲存。')}>儲存進階調整</button>
      </details>
      <div className="workbench-tabs"><button type="button" className="button primary" onClick={() => void action(() => renderProduction(csrf, projectId, runId, data!.revision.revision_id), '已開始輸出目前版本，完成後會更新上方預覽。')}>更新影片預覽</button><button type="button" className="button secondary" onClick={() => void action(async () => { const result = await exportProduction(csrf, projectId, runId); setExportUrl(result.download_url) }, '剪輯資料已準備好。')}>匯出所有可編輯圖層</button></div>
      {exportUrl && <a href={exportUrl} download>下載原生剪輯資料（含文字、效果、時間軸及來源）</a>}
      <details><summary>版本紀錄與還原</summary><label>選擇版本<select value={historyId} onChange={(e) => setHistoryId(e.target.value)}><option value="">選擇版本</option>{data?.history.map((revision) => <option key={revision.revision_id} value={revision.revision_id}>版本 {revision.ordinal} · {revision.summary}</option>)}</select></label><button type="button" className="button secondary" disabled={!historyId} onClick={() => void action(() => edit('restore_revision', { revision_id: historyId, summary: '使用者還原影片版本' }), '已還原，請更新預覽。')}>還原選定版本</button></details>
    </fieldset>
    <fieldset hidden={mode === 'manual'} disabled={busy || active || !data}>
      <legend>請 AI 繼續打磨</legend>
      {data && <RefinementReferences projectId={projectId} csrf={csrf} runId={runId} data={data} preview={preview} disabled={busy || active} suggested={referenceTarget} onChanged={(references) => setData((current) => current ? { ...current, references } : current)} onDirty={setReferenceDirty} />}
      <label>這一版要改什麼？<textarea aria-label="這一版要改什麼？" value={instruction} maxLength={8000} onChange={(e) => setInstruction(e.target.value)} placeholder="例如：保留目前剪輯，縮短早餐段落；修正 02:15 的字幕，其他部分保留。" rows={4} /></label>
      <PromptHistory projectId={projectId} csrf={csrf} kind="refinement" text={instruction} onUse={setInstruction} disabled={busy || active} />
      <ProductionMusicPicker projectId={projectId} csrf={csrf} value={musicId} onChange={onMusicChange} disabled={busy || active} defaultLabel="沿用目前配樂" />
      {referenceDirty && <p role="status">還有尚未儲存的參考註記，請先儲存或取消這筆編輯。</p>}
      <label className="consent-check"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />同意使用 Codex 額度修改目前影片。</label>
      <button type="button" className="button primary" disabled={!consent || referenceDirty || (!instruction.trim() && !data?.references?.items.length)} onClick={() => void action(async () => { await refineProduction(csrf, projectId, runId, data!.revision.revision_id, instruction.trim() || '請依已儲存的片段參考註記修訂目前影片，保留其他內容。', musicId || undefined, data!.references?.version ?? 0); setInstruction(''); setConsent(false) }, 'AI 已開始依目前版本修改。')}>修改目前影片</button>
    </fieldset>
    </div>
  </section>
}
function AudioLayerEditor({ item, layer, assets, onSave, onRemove, fps }: { fps: number; item: EditAgentTimelineItem; layer: Record<string, unknown>; assets: ProductionWorkbenchState['audio_assets']; onSave: (values: Record<string, unknown>) => void; onRemove: () => void }) {
  const [values, setValues] = useState(layer)
  return <div className="workbench-fields"><strong>{assets.find((asset) => asset.asset_id === layer.asset_id)?.name ?? '聲音片段'}</strong>
    <label>音訊來源<select value={String(values.asset_id)} onChange={(e) => setValues({ ...values, asset_id: e.target.value })}>{assets.map((asset) => <option key={asset.asset_id} value={asset.asset_id}>{asset.name}</option>)}</select></label>
    {([['gain_db', '音量（dB）'], ['source_offset_seconds', '音樂起點（秒）'], ['start_frame', '開始位置（相對鏡頭，秒）'], ['duration_frames', '持續時間（秒）'], ['fade_seconds', '淡入淡出（秒）']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" step={key.includes('frame') ? 1 / fps : 0.1} value={Number(values[key] ?? 0) / (key.includes('frame') ? fps : 1)} onChange={(e) => setValues({ ...values, [key]: key.includes('frame') ? Math.round(Number(e.target.value) * fps) : Number(e.target.value) })} /></label>)}
    <label className="consent-check"><input type="checkbox" checked={Boolean(values.duck)} onChange={(e) => setValues({ ...values, duck: e.target.checked })} />對話時降低音樂</label>
    <div className="workbench-tabs"><button className="button secondary" type="button" onClick={() => onSave(values)}>儲存聲音</button><button className="button ghost" type="button" onClick={onRemove}>移除聲音</button></div><small>錨定鏡頭：{item.item_id}</small>
  </div>
}
