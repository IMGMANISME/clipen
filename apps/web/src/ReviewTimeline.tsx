import { useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import type { EditAgentTimeline } from './api'
import { lanes } from './componentEdit'
import { clips, clock } from './reviewModel'

type Span = { id: string; start: number; end: number; label: string; selectable?: boolean; tone?: string; kind?: string; unchecked?: boolean }
type Place = { id: string; start: number; end: number; lane: number }
type Note = { id: string; at: number | null; label: string; kind: string }
type Props = { timeline: EditAgentTimeline; now: number; notes: Note[]; video: string | null; onSeek: (seconds: number) => void; selected?: string | null; onSelect?: (id: string) => void
  /** The graphic being edited, where it would sit now. */
  draft?: Place | null
  /** Dragging a graphic, stretching its ends or moving it to another lane; absent when graphics cannot be moved. */
  onPlace?: (id: string, start: number, duration: number, lane: number) => void
  /** Each graphic's look, so graphics of one kind share a colour on the timeline. */
  looks?: Record<string, { name: string; index: number }>
  /** Texts and graphics the AI never looked at in a rendered still of this version. */
  unreviewed?: string[] }

const HEAD = 72
const EDGE = 7  // pixels at each end of a graphic that stretch it rather than move it
const SNAP = 8  // pixels within which an edge snaps to a cut or the playhead
// One colour per look on the timeline; graphics without a look keep the plain component colour.
const TONES = ['#8e5a9e', '#3f7fa8', '#b0683a', '#4f8a5b', '#a64d63', '#7a6f2e', '#5a62b0', '#2e8a86']
const overlaps = (a: { start: number; end: number }, b: { start: number; end: number }) => a.start < b.end && b.start < a.end
const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300]

/** Small stills of each shot, cut from the review film itself so nothing extra is rendered. */
function useThumbnails(source: string | null, times: number[]) {
  const [thumbs, setThumbs] = useState<Record<number, string>>({})
  const key = times.join(',')
  useEffect(() => {
    if (!source || !times.length) return
    let cancelled = false
    const video = document.createElement('video')
    video.muted = true; video.preload = 'auto'; video.crossOrigin = 'anonymous'; video.src = source
    const canvas = document.createElement('canvas'); canvas.width = 112; canvas.height = 63
    const pen = canvas.getContext('2d')
    let index = 0
    const found: Record<number, string> = {}
    function next() {
      if (cancelled || index >= times.length) return
      video.currentTime = Math.min(times[index], Math.max(0, (video.duration || times[index]) - .05))
    }
    video.addEventListener('loadeddata', next)
    video.addEventListener('seeked', () => {
      if (cancelled || !pen) return
      try { pen.drawImage(video, 0, 0, canvas.width, canvas.height); found[index] = canvas.toDataURL('image/jpeg', .7) } catch { /* A frame that cannot be read stays blank. */ }
      index += 1
      // Publish in batches so the strip fills in without re-rendering on every still.
      if (index % 8 === 0 || index >= times.length) setThumbs({ ...found })
      next()
    })
    return () => { cancelled = true; video.removeAttribute('src'); video.load() }
  }, [source, key]) // eslint-disable-line react-hooks/exhaustive-deps
  return thumbs
}

/** An editing-app timeline under the review player: ruler, tracks, clips, note markers and a playhead. */
export function ReviewTimeline({ timeline, now, notes, video, onSeek, selected = null, onSelect, draft = null, onPlace, looks = {}, unreviewed = [] }: Props) {
  const unchecked = new Set(unreviewed)
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const total = Math.max(1, timeline.duration_frames)
  const seconds = total / fps
  const scroller = useRef<HTMLDivElement>(null)
  const [viewport, setViewport] = useState(800)
  const [zoom, setZoom] = useState(0)  // 0 fits the whole film; each step doubles the scale
  const dragging = useRef(false)
  const grab = useRef<{ id: string; mode: 'move' | 'left' | 'right'; x: number; y: number; row: number; from: Place; moved: boolean } | null>(null)
  const [moving, setMoving] = useState(false)
  useEffect(() => {
    const element = scroller.current
    if (!element) return
    const observer = new ResizeObserver(() => setViewport(element.clientWidth - HEAD))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const perSecond = (Math.max(200, viewport) / seconds) * 2 ** zoom
  const width = seconds * perSecond
  const x = (frame: number) => (frame / fps) * perSecond

  const shots: Span[] = clips(timeline).map((item, index) => ({ id: item.item_id, start: item.start_frame, end: item.start_frame + item.duration_frames, label: `鏡頭 ${index + 1}` }))
  const label = (text?: string) => (text ?? '').replace(/\s+/g, ' ')
  const look = (id: string) => looks[id] ? { tone: TONES[looks[id].index % TONES.length], kind: looks[id].name } : {}
  const captions: Span[] = timeline.tracks.filter((track) => (track.kind === 'title' || track.kind === 'graphic') && track.track_id === 'agent-subtitles')
    .flatMap((track) => track.items.map((item) => ({ id: item.item_id, start: item.start_frame, end: item.start_frame + item.duration_frames, label: label(item.text), unchecked: unchecked.has(item.item_id) })))
  // Graphics sit on lanes; a higher lane is drawn on top. The one being edited shows where it would go.
  const graphics: Span[][] = lanes(timeline).map((track) => track.items.filter((item) => item.item_id !== draft?.id)
    .map((item) => ({ id: item.item_id, start: item.start_frame, end: item.start_frame + item.duration_frames, label: label(item.text), selectable: Boolean(onSelect), unchecked: unchecked.has(item.item_id), ...look(item.item_id) })))
  if (draft) {
    const item = lanes(timeline).flatMap((track) => track.items).find((entry) => entry.item_id === draft.id)
    while (graphics.length <= draft.lane) graphics.push([])
    graphics[draft.lane].push({ id: draft.id, start: draft.start, end: draft.end, label: label(item?.text), selectable: true, ...look(draft.id) })
  }
  if (moving) graphics.push([])  // room to drop a graphic on a new lane of its own
  const free = (lane: number, place: Place) => !(graphics[lane] ?? []).some((span) => span.id !== place.id && overlaps(span, place))
  const music: Span[] = clips(timeline).flatMap((clip) => (clip.effects ?? []).filter((effect) => effect.effect_id === 'audio.layers')
    .flatMap((effect) => (effect.parameters.layers ?? []) as Array<Record<string, unknown>>)
    .map((layer, index) => {
      const start = clip.start_frame + Number(layer.start_frame ?? 0)
      return { id: `${clip.item_id}-${index}`, start, end: start + Number(layer.duration_frames ?? 0), label: '配樂' }
    }))
  const lower = captions.length ? 3 : 2
  const tracks: Array<{ id: string; name: string; label: string; spans: Span[]; kind: string; lane?: number }> = [
    ...graphics.map((spans, lane) => ({ id: `g${lane}`, name: `V${lower + lane}`, label: graphics.length > 1 ? `元件 ${lane + 1}` : '元件', spans, kind: 'component', lane })).reverse(),
    ...(captions.length ? [{ id: 'v2', name: 'V2', label: '字幕', spans: captions, kind: 'caption' }] : []),
    { id: 'v1', name: 'V1', label: '畫面', spans: shots, kind: 'shot' },
    ...(music.length ? [{ id: 'a1', name: 'A1', label: '配樂', spans: music, kind: 'music' }] : []),
  ]
  // Where the AI never looked, in film order, so a person can step through them.
  const unseen = [...graphics.flat(), ...captions].filter((span) => span.unchecked).sort((a, b) => a.start - b.start)
  function nextUnseen() {
    const after = unseen.find((span) => span.start > Math.round(now * fps)) ?? unseen[0]
    if (after) onSeek(after.start / fps)
  }
  const thumbs = useThumbnails(video, shots.map((shot) => (shot.start + Math.min(fps, (shot.end - shot.start) / 2)) / fps))

  // Ruler ticks: the smallest step that leaves room for a label.
  const step = STEPS.find((value) => value * perSecond >= 64) ?? 600
  const ticks = Array.from({ length: Math.floor(seconds / step) + 1 }, (_, index) => index * step)

  // Keep the playhead in view when the film plays or jumps past the visible part of a zoomed timeline.
  useEffect(() => {
    const element = scroller.current
    if (!element || dragging.current) return
    const head = now * perSecond
    if (head < element.scrollLeft || head > element.scrollLeft + element.clientWidth - HEAD - 40) element.scrollLeft = Math.max(0, head - 80)
  }, [now, perSecond])

  function scrub(event: PointerEvent<HTMLDivElement>) {
    const element = scroller.current
    if (!element) return
    const left = element.getBoundingClientRect().left + HEAD - element.scrollLeft
    const value = Math.min(seconds - 1 / fps, Math.max(0, (event.clientX - left) / perSecond))
    onSeek(Math.round(value * fps) / fps)
  }
  /** Pressing a graphic: a click opens it; dragging moves it in time or to another lane, or stretches an end. */
  function hold(event: PointerEvent<HTMLDivElement>, pick: HTMLElement) {
    const id = pick.dataset.pick ?? ''
    const lane = graphics.findIndex((spans) => spans.some((span) => span.id === id))
    const span = graphics[lane]?.find((entry) => entry.id === id)
    if (!span) return
    const box = pick.getBoundingClientRect(), offset = event.clientX - box.left
    const mode = !onPlace || box.width < EDGE * 3 ? 'move' : offset < EDGE ? 'left' : offset > box.width - EDGE ? 'right' : 'move'
    grab.current = { id, mode, x: event.clientX, y: event.clientY, row: pick.closest<HTMLElement>('.nle-track')?.offsetHeight ?? 30, from: { id, start: span.start, end: span.end, lane }, moved: false }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  /** Cuts, the playhead and the film's ends pull a dragged edge to them. */
  function snap(frame: number) {
    const marks = [0, total, Math.round(now * fps), ...shots.flatMap((shot) => [shot.start, shot.end])]
    const near = marks.reduce((best, mark) => Math.abs(mark - frame) < Math.abs(best - frame) ? mark : best, frame)
    return Math.abs(x(near) - x(frame)) <= SNAP ? near : frame
  }
  function drag(event: PointerEvent<HTMLDivElement>) {
    const held = grab.current
    if (!held || !onPlace) return
    const dx = event.clientX - held.x, dy = event.clientY - held.y
    if (!held.moved && Math.abs(dx) < 4 && Math.abs(dy) < 4) return
    if (!held.moved) { held.moved = true; setMoving(true) }
    const frames = Math.round((dx / perSecond) * fps), { from } = held
    let start = from.start, end = from.end, lane = from.lane
    if (held.mode === 'left') start = Math.max(0, Math.min(end - 6, snap(from.start + frames)))
    else if (held.mode === 'right') end = Math.min(total, Math.max(start + 6, snap(from.end + frames)))
    else {
      const length = from.end - from.start
      // Whichever end is nearer a cut or the playhead pulls the graphic to it.
      const raw = from.start + frames, head = snap(raw) - raw, tail = snap(raw + length) - raw - length
      start = raw + (head && (!tail || Math.abs(head) <= Math.abs(tail)) ? head : tail)
      start = Math.max(0, Math.min(total - length, start)); end = start + length
      // Upward is a higher lane; one that is busy at that time is passed over for the next free one.
      const wanted = Math.max(0, Math.min(graphics.length - 1, from.lane - Math.round(dy / held.row)))
      const place = { id: held.id, start, end, lane: wanted }
      lane = free(wanted, place) ? wanted : [...graphics.keys()].find((n) => n >= wanted && free(n, place)) ?? graphics.length
    }
    onPlace(held.id, start, end - start, lane)
  }
  function release() {
    const held = grab.current
    grab.current = null; dragging.current = false
    if (!held) return
    if (held.moved) setMoving(false)
    else onSelect?.(held.id)
  }
  return <div className="nle">
    <div className="nle-toolbar">
      <span className="nle-clock">{clock(now, true)}</span>
      <span className="nle-duration">/ {clock(seconds)}</span>
      {unseen.length > 0 && <span className="nle-unseen" title="AI 這一版只看過部分畫面；虛線框的字幕與元件它沒有看過，建議先檢查。">
        <span className="nle-unseen-key" aria-hidden="true" />AI 沒看過 {unseen.length} 處
        <button type="button" className="button ghost" onClick={nextUnseen}>跳到下一處</button></span>}
      <label className="nle-zoom">縮放<input type="range" min={0} max={6} step={1} value={zoom} onChange={(event) => setZoom(Number(event.target.value))} aria-label="時間軸縮放" /></label>
      <button type="button" className="button ghost" onClick={() => setZoom(0)} disabled={zoom === 0}>整部片</button>
    </div>
    <div className="nle-scroll" ref={scroller}>
      <div className="nle-canvas" style={{ width: width + HEAD }}
        onPointerDown={(event) => {
          if ((event.target as HTMLElement).closest('.nle-heads, .nle-marker')) return
          // A graphic is picked for editing, or dragged, rather than scrubbed through.
          const pick = (event.target as HTMLElement).closest<HTMLElement>('[data-pick]')
          if (pick && onSelect) { hold(event, pick); return }
          dragging.current = true; event.currentTarget.setPointerCapture(event.pointerId); scrub(event) }}
        onPointerMove={(event) => { if (grab.current) drag(event); else if (dragging.current) scrub(event) }}
        onPointerUp={release} onPointerCancel={release}>
        <div className="nle-ruler">
          <div className="nle-corner" />
          <div className="nle-ruler-track" style={{ width }}>
            {ticks.map((tick) => <span key={tick} className="nle-tick" style={{ left: tick * perSecond }}>{clock(tick)}</span>)}
            {notes.filter((note) => note.at !== null).map((note) => <button type="button" key={note.id} className={`nle-marker is-${note.kind}`} style={{ left: (note.at ?? 0) * perSecond }}
              title={`${clock(note.at ?? 0)} ${note.label}`} aria-label={`跳到注記 ${clock(note.at ?? 0)}：${note.label}`} onClick={() => onSeek(note.at ?? 0)} />)}
          </div>
        </div>
        {tracks.map((track) => <div className={`nle-track is-${track.kind}`} key={track.id}>
          <div className="nle-heads"><strong>{track.name}</strong><span>{track.label}</span></div>
          <div className={`nle-lane${track.lane !== undefined && moving && !track.spans.length ? ' is-new' : ''}`} style={{ width }}>
            {track.spans.map((span, index) => <div key={span.id} className={`nle-clip${span.selectable ? ' is-pickable' : ''}${span.selectable && onPlace ? ' is-movable' : ''}${span.id === selected ? ' is-selected' : ''}${span.unchecked ? ' is-unseen' : ''}`} data-pick={span.selectable ? span.id : undefined} style={{ left: x(span.start), width: Math.max(2, x(span.end) - x(span.start)), ...(track.kind === 'shot' && thumbs[index] ? { backgroundImage: `url(${thumbs[index]})` } : {}), ...(span.tone ? { backgroundColor: span.tone } : {}) }}
              title={`${clock(span.start / fps)}–${clock(span.end / fps)} ${span.kind ? `［${span.kind}］` : ''}${span.label}${span.unchecked ? '（AI 沒看過）' : ''}${span.selectable ? (onPlace ? '（點一下編輯，拖曳改時間或換軌，拉兩端改長度）' : '（點一下編輯）') : ''}`}>
              {x(span.end) - x(span.start) > 44 && <span>{span.label}</span>}
            </div>)}
          </div>
        </div>)}
        <span className="nle-playhead" style={{ left: HEAD + now * perSecond }} aria-hidden="true" />
      </div>
    </div>
  </div>
}
