import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { getProductionWorkbench, updateProductionReference } from './api'
import type { ComponentDraft, FilmPreview, ProductionWorkbenchState, RefinementReference, ReferenceState } from './api'
import { ComponentInspector, StillFrame } from './ComponentEditor'
import { componentDraft, openComponent } from './componentEdit'
import type { ComponentEdit } from './componentEdit'
import { readNoteDraft } from './reviewDraft'
import { ReviewTimeline } from './ReviewTimeline'
import { clips, clock, describeNote, kindLabel, noteKinds, parseClock, placeholders, selfExplained, targetsAt } from './reviewModel'
import type { NoteDraft, NoteForm as Form } from './reviewDraft'

type Props = {
  visible: boolean; projectId: string; csrf: string; runId: string; data: ProductionWorkbenchState; video: FilmPreview | null; disabled: boolean
  onChanged: (state: ReferenceState) => void; onDirty: (dirty: boolean) => void
  /** Save or remove a graphic; each resolves true once done. */
  onSaveComponent: (draft: ComponentDraft) => Promise<boolean>
  onRemoveComponent: (elementId: string) => Promise<boolean>
  /** Shown in place of the player, e.g. an older version being compared. */
  stage?: ReactNode
}

/** Watch the film, note what to change, and edit its graphics: player, side panel and timeline. */
export function ReviewNotes({ visible, projectId, csrf, runId, data, video, disabled, onChanged, onDirty, onSaveComponent, onRemoveComponent, stage }: Props) {
  const timeline = data.revision.timeline
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const state = data.references ?? { version: 0, items: [] }
  const player = useRef<HTMLVideoElement>(null)
  const draftKey = `clipen:review-note-draft:${projectId}:${runId}`
  const [draft, setDraft] = useState<NoteDraft | null>(() => {
    try { return readNoteDraft(localStorage.getItem(draftKey)) } catch { return null }
  })
  const form = draft?.revisionId === data.revision.revision_id ? draft.form : null
  const outdated = draft && !form ? draft.form : null
  const [now, setNow] = useState(form?.at ?? 0)
  const [selection, setSelection] = useState<ComponentEdit | null>(null)
  const editing = data.component_editing
  const settings = selection ? editing?.components[selection.id] : undefined
  function setForm(value: Form | null) {
    const next = value ? { revisionId: data.revision.revision_id, form: value } : null
    setDraft(next)
    try {
      if (next) localStorage.setItem(draftKey, JSON.stringify(next))
      else localStorage.removeItem(draftKey)
    } catch { /* The mounted form remains usable when storage is unavailable. */ }
  }
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const frame = Math.max(0, Math.min(timeline.duration_frames - 1, Math.round(now * fps)))
  const formFrame = form ? Math.round(form.at * fps) : frame
  const targets = targetsAt(timeline, formFrame)
  const shot = targets[0]
  const layers = (clips(timeline).find((clip) => clip.item_id === shot?.id)?.effects ?? []).filter((effect) => effect.effect_id === 'audio.layers').flatMap((effect) => (effect.parameters.layers ?? []) as Array<Record<string, unknown>>)
  useEffect(() => { onDirty(Boolean(busy || draft)) }, [busy, draft, onDirty])
  useEffect(() => { if (!visible) player.current?.pause() }, [visible])
  function seek(seconds: number) {
    setNow(seconds)
    if (player.current) player.current.currentTime = seconds
  }
  function mark() {
    player.current?.pause()
    const at = player.current ? player.current.currentTime : now
    setSelection(null); setNow(at); setError('')
    setForm({ referenceId: '', at, end: '', kind: outdated?.kind ?? 'change', target: '', note: outdated?.note ?? '', gain: outdated?.gain ?? -12, fill: outdated?.fill ?? true, layer: '' })
  }
  function select(id: string) {
    if (form) return
    const edit = openComponent(timeline, editing, id)
    if (!edit) { setError('這個元件不能直接編輯，請記一筆注記交給 AI。'); return }
    player.current?.pause()
    setError(''); setSelection(edit)
    seek((editing!.components[id].shot_start_frame + edit.start + edit.duration / 2) / fps)
  }
  function step(seconds: number) {
    // Stepping pauses so the mark lands where the user stopped; an open note follows the playhead.
    player.current?.pause()
    const last = (timeline.duration_frames - 1) / fps
    const base = player.current ? player.current.currentTime : now
    const next = Math.max(0, Math.min(last, Math.round((base + seconds) * fps) / fps))
    seek(next)
    if (form) setForm({ ...form, at: next })
  }
  useEffect(() => {
    if (!visible) return
    // "M" marks the current moment and the arrows nudge the playhead, like an editing app.
    function key(event: KeyboardEvent) {
      if (event.key === 'Escape' && selection) { setSelection(null); return }
      const field = (event.target as HTMLElement | null)?.closest('input, textarea, select, [contenteditable="true"]')
      const otherPlayer = (event.target as HTMLElement | null)?.closest('video')
      if (field || (otherPlayer && otherPlayer !== player.current) || disabled || busy || selection || event.metaKey || event.ctrlKey || event.altKey) return
      if (event.key.toLowerCase() === 'm' && !form) { event.preventDefault(); mark() }
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault()
        step((event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 1 / fps : 1))
      }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  })
  function edit(item: RefinementReference) {
    if (draft) return
    const range = item.current_range
    if (!range) { setError('這筆注記的畫面已被改動，請刪除後在新的位置重新記一筆。'); return }
    const at = (range.at_frame ?? range.start_frame) / fps
    setSelection(null); seek(at); setError('')
    setForm({ referenceId: item.reference_id, at, end: item.explicit_range ? clock((range.start_frame + range.duration_frames) / fps, true) : '', kind: item.kind, target: item.element_id ?? '', note: item.note, gain: item.source_gain_db ?? -12, fill: item.fill_music ?? true, layer: item.audio_layer_id ?? '' })
  }
  async function mutate(change: { reference: RefinementReference } | { delete_id: string }) {
    if (busy || disabled) return
    setBusy(true); setError('')
    try {
      onChanged(await updateProductionReference(csrf, projectId, runId, data.revision.revision_id, state.version, change))
      if ('reference' in change) setForm(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : '無法儲存注記。')
      try { const latest = await getProductionWorkbench(projectId, runId); if (latest.references) onChanged(latest.references) } catch { /* Keep the draft on failure. */ }
    } finally { setBusy(false) }
  }
  async function finish(work: () => Promise<boolean>) {
    if (!selection || busy) return
    setBusy(true); setError('')
    try { if (await work()) setSelection(null) }
    finally { setBusy(false) }
  }
  const endSeconds = form?.end.trim() ? parseClock(form.end) : null
  const target = targets.find((entry) => entry.id === form?.target)
  let problem = ''
  if (form) {
    if (!shot) problem = '這個時間點沒有畫面，請換個位置。'
    else if (endSeconds !== null && !(endSeconds > form.at)) problem = '結束時間要晚於標記的時間，例如 2:55。'
    else if (!selfExplained.includes(form.kind) && !form.note.trim()) problem = '寫下要怎麼改。'
  }
  function save() {
    if (!form || !shot || problem) return
    // A plain mark covers the shot or element pointed at; an end time narrows it.
    let start: number, end: number
    if (endSeconds !== null) { start = formFrame; end = Math.min(shot.end, Math.round(endSeconds * fps)) }
    else if (target?.text) { start = Math.max(shot.start, target.start); end = Math.min(shot.end, target.end) }
    else { start = shot.start; end = shot.end }
    void mutate({ reference: {
      reference_id: form.referenceId || crypto.randomUUID(), kind: form.kind, item_id: shot.id,
      start_offset_frames: start - shot.start, duration_frames: Math.max(1, end - start), note: form.note.trim(),
      at_offset_frames: formFrame - shot.start, explicit_range: endSeconds !== null, ...(target?.text ? { element_id: target.id } : {}),
      ...(form.kind === 'audio' ? { source_gain_db: form.gain, fill_music: form.fill, ...(form.layer ? { audio_layer_id: form.layer } : {}) } : {}),
    } })
  }
  const notes = state.items.map((item) => ({ item, ...describeNote(item, timeline) })).sort((a, b) => (a.at ?? Infinity) - (b.at ?? Infinity))
  // While a graphic is open, show the frame in the middle of where it would appear.
  const editFrame = selection && settings ? Math.min(timeline.duration_frames - 1, settings.shot_start_frame + selection.start + Math.floor(selection.duration / 2)) : frame

  let panel: ReactNode
  if (selection && settings && editing) panel = <ComponentInspector edit={selection} settings={settings} editing={editing} fps={fps} busy={busy} disabled={disabled}
    onChange={setSelection} onCancel={() => setSelection(null)} onSave={() => void finish(() => onSaveComponent(componentDraft(selection)))}
    onRemove={() => void finish(() => onRemoveComponent(selection.id))} />
  else if (form) panel = <fieldset className="rv-note-form" disabled={disabled || busy}>
    <div className="rv-panel-head"><h4>{form.referenceId ? '修改注記' : '記一筆'}</h4><span className="rv-time">{clock(form.at, true)}</span>
      <button type="button" className="rv-icon" aria-label="取消" onClick={() => setForm(null)}>×</button></div>
    <div className="review-chips" role="radiogroup" aria-label="要怎麼處理">{noteKinds.map(([value, label]) => <button type="button" role="radio" aria-checked={form.kind === value} key={value} className={`review-chip is-${value}`} onClick={() => setForm({ ...form, kind: value })}>{label}</button>)}</div>
    {targets.length > 1 && <div className="review-chips" role="radiogroup" aria-label="指的是">{targets.map((entry, index) => <button type="button" role="radio" key={entry.id} aria-checked={index === 0 ? !target?.text : form.target === entry.id} className="review-chip" onClick={() => setForm({ ...form, target: index === 0 ? '' : entry.id })}>{entry.label}</button>)}</div>}
    <textarea aria-label="注記說明" rows={3} maxLength={2000} value={form.note} placeholder={placeholders[form.kind]} onChange={(e) => setForm({ ...form, note: e.target.value })} autoFocus
      onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !problem) save() }} />
    {form.kind === 'audio' && <>
      {layers.length > 0 && <label>哪個聲音<select value={form.layer} onChange={(e) => setForm({ ...form, layer: e.target.value })}><option value="">這段素材的原音</option>{layers.map((layer) => <option key={String(layer.layer_id)} value={String(layer.layer_id)}>{data.audio_assets.find((asset) => asset.asset_id === layer.asset_id)?.name ?? '配樂／音效'}</option>)}</select></label>}
      <label>降低音量 {form.gain} dB<input aria-label="希望降低音量" type="range" min={-60} max={0} step={1} value={form.gain} onChange={(e) => setForm({ ...form, gain: Number(e.target.value) })} /></label>
      <label className="consent-check"><input type="checkbox" checked={form.fill} onChange={(e) => setForm({ ...form, fill: e.target.checked })} />讓配樂補滿這段</label>
    </>}
    <details className="rv-range" open={Boolean(form.end)}><summary>只改其中一段</summary>
      <div className="rv-row"><input aria-label="結束時間" value={form.end} placeholder="到 2:55" onChange={(e) => setForm({ ...form, end: e.target.value })} />
        {video && <button type="button" className="button ghost" onClick={() => setForm({ ...form, end: clock(player.current?.currentTime ?? now, true) })}>到目前時間</button>}</div>
    </details>
    {problem && form.note && <p className="rv-hint">{problem}</p>}
    <div className="rv-row"><button type="button" className="button primary" disabled={Boolean(problem)} onClick={save}>{busy ? '儲存中…' : '記下'}</button>
      <button type="button" className="button ghost" onClick={() => setForm(null)}>取消</button></div>
  </fieldset>
  else panel = <>
    <div className="rv-panel-head"><h4>注記</h4>{notes.length > 0 && <span className="rv-count">{notes.length}</span>}
      <button type="button" className="button secondary rv-mark" disabled={disabled || busy} onClick={mark}>記一筆<kbd>M</kbd></button></div>
    {outdated && <div className="rv-outdated" role="status"><p>上一版有一筆沒記下的注記：「{outdated.note}」</p>
      <button type="button" className="button ghost" onClick={() => setForm(null)}>不要了</button></div>}
    {notes.length ? <ol className="rv-notes">{notes.map(({ item, at, time, target: label }) => <li key={item.reference_id} className={`is-${item.kind}`}>
      <button type="button" className="rv-note-jump" disabled={at === null} onClick={() => { if (at !== null) seek(at) }} aria-label={`跳到 ${time}`}>
        <span className="rv-time">{time}</span><span className={`review-kind is-${item.kind}`}>{kindLabel[item.kind]}</span>
        <span className="rv-note-target">{label}</span>
        {item.note && <span className="rv-note-text">{item.note}</span>}
        {(item.current_range?.element_present === false || !item.current_range) && <small>畫面已經改過，AI 會依原始素材找回位置。</small>}
      </button>
      <div className="rv-note-actions"><button type="button" className="rv-text-button" disabled={disabled || busy || Boolean(draft)} onClick={() => edit(item)}>修改</button>
        <button type="button" className="rv-text-button" disabled={disabled || busy} onClick={() => void mutate({ delete_id: item.reference_id })}>刪除</button></div>
    </li>)}</ol>
      : <div className="rv-empty"><p>看到要改的地方，按 <kbd>M</kbd> 記一筆。</p><p>想改畫面上的字，點時間線上紫色的元件。</p></div>}
  </>

  return <section className="rv-studio" aria-label="審片">
    <div className="rv-stage">
      {stage ?? (selection || !video
        ? <StillFrame projectId={projectId} csrf={csrf} runId={runId} revisionId={data.revision.revision_id} timeline={timeline} frame={editFrame}
          component={selection ? componentDraft(selection) : undefined} onMove={selection ? (x, y) => setSelection({ ...selection, x, y }) : undefined} />
        : <video ref={player} controls preload="metadata" src={video.media_url} aria-label="審片用影片" onLoadedMetadata={(e) => { e.currentTarget.currentTime = now }} onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)} onSeeked={(e) => setNow(e.currentTarget.currentTime)} />)}
    </div>
    <aside className="rv-panel">
      {error && <p role="alert" className="rv-error">{error}</p>}
      {panel}
    </aside>
    <ReviewTimeline timeline={timeline} now={now} video={video?.media_url ?? null} selected={selection?.id ?? null} onSelect={form ? undefined : select}
      onSeek={(seconds) => { seek(seconds); if (form) setForm({ ...form, at: seconds }) }}
      notes={notes.map(({ item, at, target: label }) => ({ id: item.reference_id, at, label: `${kindLabel[item.kind]} ${label}`, kind: item.kind }))} />
  </section>
}
