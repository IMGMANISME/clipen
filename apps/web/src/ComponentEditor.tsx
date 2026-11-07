import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent } from 'react'
import { getEditingFrame } from './api'
import type { ComponentDraft, ComponentEditing, ComponentSettings, EditAgentTimeline } from './api'
import { X, Y, clamp, componentChanged } from './componentEdit'
import type { ComponentEdit, Values } from './componentEdit'
import { clock, parseClock } from './reviewModel'

const colorNames: Array<['accent' | 'paper' | 'ink', string]> = [['accent', '主色'], ['paper', '亮色'], ['ink', '暗色']]

/** A text field that commits a parsed value on Enter or leaving it, so typing is never clamped mid-word. */
function Field({ label, value, format, parse, onCommit, disabled, suffix }: { label: string; value: number; format: (value: number) => string; parse: (text: string) => number; onCommit: (value: number) => void; disabled: boolean; suffix?: string }) {
  const [text, setText] = useState<string | null>(null)
  const commit = () => { if (text === null) return; const parsed = parse(text); if (Number.isFinite(parsed)) onCommit(parsed); setText(null) }
  return <label>{label}<span className="rv-field">
    <input value={text ?? format(value)} disabled={disabled} inputMode="decimal" onChange={(e) => setText(e.target.value)} onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commit() } if (e.key === 'Escape') setText(null) }} />{suffix && <em>{suffix}</em>}</span></label>
}

type InspectorProps = {
  edit: ComponentEdit; settings: ComponentSettings; editing: ComponentEditing; fps: number; busy: boolean; disabled: boolean
  onChange: (edit: ComponentEdit) => void; onSave: () => void; onCancel: () => void; onRemove: () => void
}
/** Everything about one graphic: wording, look, typeface, timing; its place is dragged on the picture. */
export function ComponentInspector({ edit, settings, editing, fps, busy, disabled, onChange, onSave, onCancel, onRemove }: InspectorProps) {
  const changed = componentChanged(edit)
  const valid = Boolean(edit.text.trim())
  const shotStart = settings.shot_start_frame, shotEnd = settings.shot_start_frame + settings.shot_duration_frames
  const update = (next: Partial<Values>) => onChange({ ...edit, ...next })
  const enter = (e: KeyboardEvent<HTMLInputElement>) => { if (e.key === 'Enter' && changed && valid) onSave() }
  const moved = edit.x !== edit.saved.x || edit.y !== edit.saved.y
  const palette = editing.visual_styles.find((style) => style.id === edit.visual)?.colors ?? { accent: '#e9b44c', paper: '#ffffff', ink: '#111111' }
  return <div className="rv-inspector">
    <div className="rv-panel-head"><h4>{settings.name}</h4><span className="rv-time">{clock((shotStart + edit.start) / fps)}</span>
      <button type="button" className="rv-icon" aria-label="關閉" onClick={onCancel}>×</button></div>
    <label>主要文字<span className={`rv-counter${edit.text.trim().length > settings.max_characters ? ' is-over' : ''}`}>{edit.text.trim().length}/{settings.max_characters}</span>
      <input value={edit.text} maxLength={48} autoFocus disabled={disabled || busy} onChange={(e) => update({ text: e.target.value })} onKeyDown={enter} /></label>
    <label>補充文字<input value={edit.detail} maxLength={48} placeholder="可不填" disabled={disabled || busy} onChange={(e) => update({ detail: e.target.value })} onKeyDown={enter} /></label>
    <div className="rv-pair">
      {editing.visual_styles.length > 1 && <label>樣式<select value={edit.visual} disabled={disabled || busy} onChange={(e) => update({ visual: e.target.value })}>
        {editing.visual_styles.map((style) => <option key={style.id} value={style.id}>{style.name}</option>)}</select></label>}
      {editing.font_styles.length > 1 && <label>字體<select value={edit.font} disabled={disabled || busy} onChange={(e) => update({ font: e.target.value })}>
        {editing.font_styles.map((font) => <option key={font.id} value={font.id}>{font.name}</option>)}</select></label>}
    </div>
    <div className="rv-look">
      <label className="rv-scale">大小<span className="rv-counter">{Math.round(edit.scale * 100)}%</span>
        <input type="range" min={60} max={160} step={5} value={Math.round(edit.scale * 100)} disabled={disabled || busy} aria-label="元件大小"
          onChange={(e) => update({ scale: Number(e.target.value) / 100 })} /></label>
      <div className="rv-colors" role="group" aria-label="顏色">
        {colorNames.map(([name, label]) => <label key={name} className="rv-swatch" title={label}>
          <input type="color" value={edit.colors[name] ?? palette[name]} disabled={disabled || busy} aria-label={label}
            onChange={(e) => update({ colors: { ...edit.colors, [name]: e.target.value } })} />
          <span style={{ background: edit.colors[name] ?? palette[name] }} />{label}</label>)}
        {(Object.keys(edit.colors).length > 0 || edit.scale !== 1) && <button type="button" className="rv-link" onClick={() => update({ colors: {}, scale: 1 })}>恢復樣式原樣</button>}
      </div>
    </div>
    <div className="rv-pair">
      <Field label="出現" value={(shotStart + edit.start) / fps} format={(value) => clock(value, true)} parse={parseClock} disabled={disabled || busy}
        onCommit={(seconds) => update({ start: Math.max(0, Math.min(settings.shot_duration_frames - edit.duration, Math.round(seconds * fps) - shotStart)) })} />
      <Field label="長度" suffix="秒" value={edit.duration / fps} format={(value) => value.toFixed(1)} parse={Number} disabled={disabled || busy}
        onCommit={(seconds) => update({ duration: Math.max(6, Math.min(settings.shot_duration_frames - edit.start, Math.round(seconds * fps))) })} />
    </div>
    <p className="rv-hint">在畫面上拖曳元件就能移動，方向鍵可以微調。這段鏡頭是 {clock(shotStart / fps, true)}–{clock(shotEnd / fps, true)}，元件會留在裡面。
      {moved && <button type="button" className="rv-link" onClick={() => update({ x: edit.saved.x, y: edit.saved.y })}>回到原位</button>}</p>
    <div className="rv-row">
      <button type="button" className="button primary" disabled={disabled || busy || !changed || !valid} onClick={onSave}>{busy ? '儲存中…' : '儲存'}</button>
      <button type="button" className="button ghost" onClick={onCancel}>取消</button>
      <button type="button" className="rv-text-button is-danger" disabled={disabled || busy} onClick={onRemove}>刪除元件</button>
    </div>
  </div>
}

type StillProps = {
  projectId: string; csrf: string; runId: string; revisionId: string; timeline: EditAgentTimeline; frame: number
  component?: ComponentDraft; onMove?: (x: number, y: number) => void
}
/** A still of the current version, rendered on demand. With a component draft it shows the unsaved
 * graphic and a box around it that can be dragged, or nudged with the arrow keys. */
export function StillFrame({ projectId, csrf, runId, revisionId, timeline, frame, component, onMove }: StillProps) {
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const draftKey = component ? JSON.stringify(component) : ''
  const key = `${revisionId}:${frame}:${draftKey}`
  // The last answer stays on screen (dimmed) while the next one renders.
  const [shown, setShown] = useState<{ key: string; image: string; error: string; box: { x: number; y: number; width: number; height: number } | null; at: { x: number; y: number } | null }>({ key: '', image: '', error: '', box: null, at: null })
  const surface = useRef<HTMLDivElement>(null)
  const drag = useRef<{ x: number; y: number; clientX: number; clientY: number } | null>(null)
  useEffect(() => {
    const abort = new AbortController()
    const timer = window.setTimeout(() => {
      const draft = draftKey ? JSON.parse(draftKey) as ComponentDraft : undefined
      void getEditingFrame(csrf, projectId, runId, revisionId, frame, abort.signal, draft)
        .then((value) => { if (!abort.signal.aborted) setShown({ key, image: value.image_data_url, error: '', box: value.component_box ?? null, at: draft ? { x: draft.x ?? 0, y: draft.y ?? 0 } : null }) })
        .catch((e) => { if (!abort.signal.aborted) setShown((last) => ({ ...last, key, error: e instanceof Error ? e.message : '暫時無法讀取畫面。' })) })
    }, 300)
    return () => { window.clearTimeout(timer); abort.abort() }
  }, [csrf, projectId, runId, revisionId, frame, draftKey, key])
  const { image, error, box, at } = shown
  // The box follows the pointer at once; the picture catches up when the new still arrives.
  const place = box && at && component && onMove ? { left: box.x + (component.x ?? at.x) - at.x, top: box.y + (component.y ?? at.y) - at.y } : null
  function down(event: PointerEvent<HTMLDivElement>) {
    if (!component || !onMove) return
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { x: component.x ?? 0, y: component.y ?? 0, clientX: event.clientX, clientY: event.clientY }
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    const start = drag.current, rect = surface.current?.getBoundingClientRect()
    if (!start || !rect || !onMove) return
    onMove(clamp(start.x + (event.clientX - start.clientX) / rect.width, X), clamp(start.y + (event.clientY - start.clientY) / rect.height, Y))
  }
  function nudge(event: KeyboardEvent<HTMLDivElement>) {
    if (!component || !onMove) return
    const step = event.shiftKey ? .02 : .005
    const delta = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[event.key]
    if (!delta) return
    event.preventDefault(); event.stopPropagation()
    onMove(clamp((component.x ?? 0) + delta[0], X), clamp((component.y ?? 0) + delta[1], Y))
  }
  const ratio = timeline.canvas.width / timeline.canvas.height
  return <div className={`rv-still${shown.key !== key && image ? ' is-loading' : ''}`} ref={surface}
    style={{ aspectRatio: `${timeline.canvas.width}/${timeline.canvas.height}`, width: `min(100%, calc(max(220px, 100vh - 480px) * ${ratio}))` }}>
    {image ? <img src={image} alt={`${clock(frame / fps)} 的畫面`} draggable={false} /> : <span className="rv-still-wait">正在取得畫面…</span>}
    {place && box && <div className="rv-grab" role="slider" tabIndex={0} aria-label="元件位置，拖曳或用方向鍵移動" aria-valuetext={`水平 ${Math.round((component?.x ?? 0) * 100)}%，垂直 ${Math.round((component?.y ?? 0) * 100)}%`}
      style={{ left: `${place.left * 100}%`, top: `${place.top * 100}%`, width: `${box.width * 100}%`, height: `${box.height * 100}%` }}
      onPointerDown={down} onPointerMove={move} onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }} onKeyDown={nudge} />}
    {error && <p className="rv-still-error" role="alert">{error}</p>}
  </div>
}
