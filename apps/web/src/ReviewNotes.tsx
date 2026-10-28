import { useEffect, useId, useRef, useState } from 'react'
import { getEditingFrame, getProductionWorkbench, updateProductionReference } from './api'
import type { EditAgentTimeline, FilmPreview, ProductionWorkbenchState, RefinementReference, ReferenceState } from './api'

type Kind = RefinementReference['kind']
export const noteKinds: Array<[Kind, string]> = [['change', '要修改'], ['remove', '拿掉'], ['keep', '這樣可以'], ['highlight', '加強爆點'], ['location', '地點補充'], ['audio', '聲音'], ['note', '其他']]
const kindLabel = Object.fromEntries(noteKinds) as Record<Kind, string>
// Only these say enough without a sentence.
const selfExplained: Kind[] = ['remove', 'keep']
const placeholders: Record<Kind, string> = {
  change: '例如：這段字幕改成「第一次吃到」，晚一秒出現。', remove: '可以不填；或補充原因，例如：直式畫面，和前後接不起來。', keep: '可以不填；例如：這段文字和時間都很好。',
  highlight: '例如：第一口之後的反應是笑點，請留鋪陳再強調。', location: '例如：這裡是銀座的某某店，不是淺草。', audio: '例如：風聲太大，壓低原音，讓配樂接上；保留說話內容。', note: '補充只有你知道的拍攝背景或想法。',
}

type Target = { id: string; label: string; start: number; end: number; text?: boolean }
type Form = { referenceId: string; at: number; end: string; kind: Kind; target: string; note: string; gain: number; fill: boolean; layer: string }

export function clock(seconds: number, precise = false) {
  const minutes = Math.floor(seconds / 60), rest = seconds - minutes * 60
  return `${minutes}:${precise ? rest.toFixed(1).padStart(4, '0') : String(Math.floor(rest)).padStart(2, '0')}`
}
function parseClock(value: string) {
  const parts = value.trim().split(':')
  if (!parts.length || parts.length > 2 || parts.some((part) => !/^\d+(\.\d+)?$/.test(part))) return NaN
  return parts.length === 2 ? Number(parts[0]) * 60 + Number(parts[1]) : Number(parts[0])
}
function clips(timeline: EditAgentTimeline) { return timeline.tracks.filter((track) => track.kind === 'video').flatMap((track) => track.items) }
function trackName(trackId: string | undefined) { return trackId === 'agent-subtitles' ? '字幕' : trackId?.startsWith('program-components') ? '元件' : '文字' }
/** Everything on screen at `frame`: the shot first, then captions and components. */
export function targetsAt(timeline: EditAgentTimeline, frame: number): Target[] {
  const shots = clips(timeline)
  const shot = shots.findIndex((item) => item.start_frame <= frame && frame < item.start_frame + item.duration_frames)
  const result: Target[] = shot < 0 ? [] : [{ id: shots[shot].item_id, label: `鏡頭 ${shot + 1}（畫面）`, start: shots[shot].start_frame, end: shots[shot].start_frame + shots[shot].duration_frames }]
  for (const track of timeline.tracks) {
    if (track.kind === 'video' || track.kind === 'audio') continue
    for (const item of track.items) {
      if (!item.text || item.start_frame > frame || frame >= item.start_frame + item.duration_frames) continue
      const text = item.text.replace(/\s+/g, ' ')
      result.push({ id: item.item_id, label: `${trackName(track.track_id)}「${text.length > 24 ? `${text.slice(0, 24)}…` : text}」`, start: item.start_frame, end: item.start_frame + item.duration_frames, text: true })
    }
  }
  return result
}
/** How a note reads to a person, and to the AI in the request text. */
export function describeNote(item: RefinementReference, timeline: EditAgentTimeline) {
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const range = item.current_range
  const shot = clips(timeline).findIndex((clip) => clip.item_id === range?.item_id)
  const at = range ? (range.at_frame ?? range.start_frame) / fps : null
  const time = at === null ? '位置已變動' : item.explicit_range && range ? `${clock(at)}–${clock((range.start_frame + range.duration_frames) / fps)}` : clock(at)
  const target = item.element_id && item.element_text ? `${trackName(item.element_track)}「${item.element_text.replace(/\s+/g, ' ')}」` : shot >= 0 ? `鏡頭 ${shot + 1}` : '這個畫面'
  return { at, time, target, line: `${time} ${kindLabel[item.kind]}：${target}。${item.note}${item.kind === 'audio' && item.source_gain_db !== undefined ? `（原音 ${item.source_gain_db} dB${item.fill_music ? '，配樂補滿' : ''}）` : ''}` }
}
export function composePrompt(instruction: string, items: RefinementReference[], timeline: EditAgentTimeline) {
  const lines = [...items].map((item) => describeNote(item, timeline)).sort((a, b) => (a.at ?? Infinity) - (b.at ?? Infinity)).map((note, index) => `${index + 1}. ${note.line}`)
  const overall = instruction.trim() || (lines.length ? '請依下面的審片注記修改目前影片，其他部分保留。' : '')
  return lines.length ? `${overall}\n\n審片注記（時間是目前這一版影片的位置）：\n${lines.join('\n')}` : overall
}

type Props = { projectId: string; csrf: string; runId: string; data: ProductionWorkbenchState; video: FilmPreview | null; disabled: boolean; onChanged: (state: ReferenceState) => void; onDirty: (dirty: boolean) => void }
export function ReviewNotes({ projectId, csrf, runId, data, video, disabled, onChanged, onDirty }: Props) {
  const id = useId()
  const timeline = data.revision.timeline
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const state = data.references ?? { version: 0, items: [] }
  const player = useRef<HTMLVideoElement>(null)
  const stage = useRef<HTMLDivElement>(null)
  const [now, setNow] = useState(0)
  const [form, setForm] = useState<Form | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const frame = Math.max(0, Math.min(timeline.duration_frames - 1, Math.round(now * fps)))
  const formFrame = form ? Math.round(form.at * fps) : frame
  const targets = targetsAt(timeline, formFrame)
  const shot = targets[0]
  const layers = (clips(timeline).find((clip) => clip.item_id === shot?.id)?.effects ?? []).filter((effect) => effect.effect_id === 'audio.layers').flatMap((effect) => (effect.parameters.layers ?? []) as Array<Record<string, unknown>>)
  useEffect(() => { onDirty(Boolean(busy || form)) }, [busy, form, onDirty])
  function seek(seconds: number) {
    setNow(seconds)
    if (player.current) player.current.currentTime = seconds
  }
  function mark() {
    player.current?.pause()
    const at = player.current ? player.current.currentTime : now
    setNow(at); setError(''); setMessage('')
    setForm({ referenceId: '', at, end: '', kind: 'change', target: '', note: '', gain: -12, fill: true, layer: '' })
  }
  useEffect(() => {
    // "M" marks the current moment, like a marker key in an editing app.
    function key(event: KeyboardEvent) {
      const field = (event.target as HTMLElement | null)?.closest('input, textarea, select, [contenteditable="true"]')
      if (event.key.toLowerCase() === 'm' && !event.metaKey && !event.ctrlKey && !event.altKey && !field && !form && !disabled) { event.preventDefault(); mark() }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  })
  function edit(item: RefinementReference) {
    const range = item.current_range
    if (!range) { setError('這筆注記的畫面已被改動，請刪除後在新的位置重新記一筆。'); return }
    const at = (range.at_frame ?? range.start_frame) / fps
    seek(at); setError(''); setMessage('')
    setForm({ referenceId: item.reference_id, at, end: item.explicit_range ? clock((range.start_frame + range.duration_frames) / fps, true) : '', kind: item.kind, target: item.element_id ?? '', note: item.note, gain: item.source_gain_db ?? -12, fill: item.fill_music ?? true, layer: item.audio_layer_id ?? '' })
  }
  async function mutate(change: { reference: RefinementReference } | { delete_id: string }) {
    if (busy || disabled) return
    setBusy(true); setError(''); setMessage('')
    try {
      onChanged(await updateProductionReference(csrf, projectId, runId, data.revision.revision_id, state.version, change))
      setMessage('reference' in change ? '已記下。送出修改時會一起交給 AI。' : '已刪除這筆注記。')
      if ('reference' in change) setForm(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : '無法儲存注記。')
      try { const latest = await getProductionWorkbench(projectId, runId); if (latest.references) onChanged(latest.references) } catch { /* Keep the draft on failure. */ }
    } finally { setBusy(false) }
  }
  const endSeconds = form?.end.trim() ? parseClock(form.end) : null
  const target = targets.find((entry) => entry.id === form?.target)
  let problem = ''
  if (form) {
    if (!shot) problem = '這個時間點沒有畫面，請換個位置。'
    else if (endSeconds !== null && !(endSeconds > form.at)) problem = '結束時間要晚於標記的時間，格式例如 2:55。'
    else if (!selfExplained.includes(form.kind) && !form.note.trim()) problem = '請寫下要怎麼處理。'
  }
  function save() {
    if (!form || !shot || problem) return
    // A plain mark covers the shot or element pointed at; an end time narrows it.
    let start: number, end: number
    if (endSeconds !== null) { start = formFrame; end = Math.min(shot.end, Math.round(endSeconds * fps)) }
    else if (target?.text) { start = Math.max(shot.start, target.start); end = Math.min(shot.end, target.end) }
    else { start = shot.start; end = shot.end }
    if (endSeconds !== null && Math.round(endSeconds * fps) > shot.end) setMessage(`範圍已縮到這個鏡頭結束（${clock(shot.end / fps)}）；跨鏡頭請分開記。`)
    void mutate({ reference: {
      reference_id: form.referenceId || crypto.randomUUID(), kind: form.kind, item_id: shot.id,
      start_offset_frames: start - shot.start, duration_frames: Math.max(1, end - start), note: form.note.trim(),
      at_offset_frames: formFrame - shot.start, explicit_range: endSeconds !== null, ...(target?.text ? { element_id: target.id } : {}),
      ...(form.kind === 'audio' ? { source_gain_db: form.gain, fill_music: form.fill, ...(form.layer ? { audio_layer_id: form.layer } : {}) } : {}),
    } })
  }
  const notes = state.items.map((item) => ({ item, ...describeNote(item, timeline) })).sort((a, b) => (a.at ?? Infinity) - (b.at ?? Infinity))
  return <section className="review-notes" aria-labelledby={`${id}-heading`}>
    <div className="review-notes-heading"><h4 id={`${id}-heading`}>邊看邊記</h4><span>{notes.length ? `${notes.length} 筆注記` : '還沒有注記'}</span></div>
    <div className="review-player" ref={stage}>
      {video ? <video ref={player} controls preload="metadata" src={video.media_url} aria-label="審片用影片" onTimeUpdate={(e) => setNow(e.currentTarget.currentTime)} onSeeked={(e) => setNow(e.currentTarget.currentTime)} />
        : <FrameViewer projectId={projectId} csrf={csrf} runId={runId} revisionId={data.revision.revision_id} timeline={timeline} frame={frame} onSeek={(value) => setNow(value / fps)} />}
      <div className="review-mark-bar">
        <button type="button" className="button primary" disabled={disabled || busy || Boolean(form)} onClick={mark}>在 {clock(now)} 記一筆</button>
        <span className="prompt-draft-hint">看到要改的地方就按，或按鍵盤 M。影片會先暫停。</span>
      </div>
    </div>
    {error && <p role="alert" className="inline-message error">{error}</p>}{message && <p role="status">{message}</p>}
    {form && <fieldset className="review-note-form" disabled={disabled || busy}>
      <legend>{form.referenceId ? '修改注記' : `${clock(form.at, true)} 的注記`}</legend>
      <div className="review-chips" role="radiogroup" aria-label="要怎麼處理">{noteKinds.map(([value, label]) => <button type="button" role="radio" aria-checked={form.kind === value} key={value} className={`review-chip is-${value}`} onClick={() => setForm({ ...form, kind: value })}>{label}</button>)}</div>
      {targets.length > 1 && <div className="review-chips" role="radiogroup" aria-label="指的是">{targets.map((entry, index) => <button type="button" role="radio" key={entry.id} aria-checked={index === 0 ? !target?.text : form.target === entry.id} className="review-chip" onClick={() => setForm({ ...form, target: index === 0 ? '' : entry.id })}>{entry.label}</button>)}</div>}
      {targets.length === 1 && <p className="review-target">指的是：{shot.label}</p>}
      <label>說明<textarea aria-label="注記說明" rows={2} maxLength={2000} value={form.note} placeholder={placeholders[form.kind]} onChange={(e) => setForm({ ...form, note: e.target.value })} autoFocus /></label>
      {form.kind === 'audio' && <>
        {layers.length > 0 && <label>哪個聲音<select value={form.layer} onChange={(e) => setForm({ ...form, layer: e.target.value })}><option value="">這段素材的原音</option>{layers.map((layer) => <option key={String(layer.layer_id)} value={String(layer.layer_id)}>{data.audio_assets.find((asset) => asset.asset_id === layer.asset_id)?.name ?? '配樂／音效'}</option>)}</select></label>}
        <label>希望降低音量：{form.gain} dB<input aria-label="希望降低音量" type="range" min={-60} max={0} step={1} value={form.gain} onChange={(e) => setForm({ ...form, gain: Number(e.target.value) })} /></label>
        <label className="consent-check"><input type="checkbox" checked={form.fill} onChange={(e) => setForm({ ...form, fill: e.target.checked })} />讓配樂補滿這段，對話保持清楚</label>
      </>}
      <details className="review-range" open={Boolean(form.end)}><summary>只改其中一段</summary>
        <p>不填時，注記指整個{target?.text ? '字幕或元件' : '鏡頭'}。要指定一段，填結束時間。</p>
        <div className="workbench-tabs"><label>到<input aria-label="結束時間" value={form.end} placeholder="例如 2:55" onChange={(e) => setForm({ ...form, end: e.target.value })} /></label>
          {video && <button type="button" className="button ghost" onClick={() => setForm({ ...form, end: clock(player.current?.currentTime ?? now, true) })}>用影片目前時間</button>}</div>
      </details>
      {problem && <p className="prompt-draft-hint">{problem}</p>}
      <div className="workbench-tabs"><button type="button" className="button secondary" disabled={Boolean(problem)} onClick={save}>{busy ? '儲存中…' : '記下這筆'}</button><button type="button" className="button ghost" onClick={() => setForm(null)}>取消</button></div>
    </fieldset>}
    {notes.length > 0 && <ol className="review-note-list">{notes.map(({ item, at, time, target: label }) => <li key={item.reference_id} className={`is-${item.kind}`}>
      <button type="button" className="review-note-time" disabled={at === null} onClick={() => { if (at === null) return; seek(at); stage.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }} aria-label={`跳到 ${time}`}>{time}</button>
      <div><strong><span className={`review-kind is-${item.kind}`}>{kindLabel[item.kind]}</span>{label}</strong>
        {item.note && <p>{item.note}</p>}
        {item.current_range?.element_present === false && <small>這個字幕或元件已不在影片中。</small>}
        {!item.current_range && <small>這段畫面已被改動，AI 會依原始素材找回位置。</small>}
        {item.kind === 'location' && <small>地點補充會一直保留，之後的修改也會參考。</small>}</div>
      <div className="review-note-actions"><button type="button" className="button ghost" disabled={disabled || busy || Boolean(form)} onClick={() => edit(item)}>修改</button><button type="button" className="button ghost" disabled={disabled || busy} onClick={() => void mutate({ delete_id: item.reference_id })}>刪除</button></div>
    </li>)}</ol>}
    <p className="prompt-draft-hint">注記只存在 Clipen，不使用 AI 額度。送出修改後，這輪注記會交給 AI 並清空；地點補充會保留。</p>
  </section>
}

function FrameViewer({ projectId, csrf, runId, revisionId, timeline, frame, onSeek }: { projectId: string; csrf: string; runId: string; revisionId: string; timeline: EditAgentTimeline; frame: number; onSeek: (frame: number) => void }) {
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const [image, setImage] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    const abort = new AbortController()
    const timer = window.setTimeout(() => {
      void getEditingFrame(csrf, projectId, runId, revisionId, frame, undefined, abort.signal)
        .then((value) => { if (!abort.signal.aborted) { setImage(value.image_data_url); setError('') } })
        .catch((e) => { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '暫時無法讀取畫面。') })
    }, 300)
    return () => { window.clearTimeout(timer); abort.abort() }
  }, [csrf, projectId, runId, revisionId, frame])
  return <div className="review-frame">
    <div className="workbench-preview-canvas" style={{ aspectRatio: `${timeline.canvas.width}/${timeline.canvas.height}` }}>{image ? <img src={image} alt={`目前版本 ${clock(frame / fps)} 的畫面`} draggable={false} /> : <div className="workbench-frame-placeholder">正在取得畫面…</div>}</div>
    {error && <p className="inline-message error" role="alert">{error}</p>}
    <label>時間 {clock(frame / fps, true)}<input type="range" min={0} max={Math.max(0, timeline.duration_frames - 1)} step={1} value={frame} onChange={(e) => onSeek(Number(e.target.value))} /></label>
  </div>
}
