import { useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import type { EditAgentTimeline } from './api'
import { clips, clock } from './reviewModel'

type Span = { id: string; start: number; end: number; label: string }
type Note = { id: string; at: number | null; label: string; kind: string }
type Props = { timeline: EditAgentTimeline; now: number; notes: Note[]; video: string | null; onSeek: (seconds: number) => void }

const HEAD = 72
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
export function ReviewTimeline({ timeline, now, notes, video, onSeek }: Props) {
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const total = Math.max(1, timeline.duration_frames)
  const seconds = total / fps
  const scroller = useRef<HTMLDivElement>(null)
  const [viewport, setViewport] = useState(800)
  const [zoom, setZoom] = useState(0)  // 0 fits the whole film; each step doubles the scale
  const dragging = useRef(false)
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
  const text = (subtitles: boolean): Span[] => timeline.tracks
    .filter((track) => (track.kind === 'title' || track.kind === 'graphic') && (track.track_id === 'agent-subtitles') === subtitles)
    .flatMap((track) => track.items).map((item) => ({ id: item.item_id, start: item.start_frame, end: item.start_frame + item.duration_frames, label: (item.text ?? '').replace(/\s+/g, ' ') }))
  const music: Span[] = clips(timeline).flatMap((clip) => (clip.effects ?? []).filter((effect) => effect.effect_id === 'audio.layers')
    .flatMap((effect) => (effect.parameters.layers ?? []) as Array<Record<string, unknown>>)
    .map((layer, index) => {
      const start = clip.start_frame + Number(layer.start_frame ?? 0)
      return { id: `${clip.item_id}-${index}`, start, end: start + Number(layer.duration_frames ?? 0), label: '配樂' }
    }))
  const components = text(false), captions = text(true)
  const tracks: Array<{ id: string; name: string; label: string; spans: Span[]; kind: string }> = [
    ...(components.length ? [{ id: 'v3', name: 'V3', label: '元件', spans: components, kind: 'component' }] : []),
    ...(captions.length ? [{ id: 'v2', name: 'V2', label: '字幕', spans: captions, kind: 'caption' }] : []),
    { id: 'v1', name: 'V1', label: '畫面', spans: shots, kind: 'shot' },
    ...(music.length ? [{ id: 'a1', name: 'A1', label: '配樂', spans: music, kind: 'music' }] : []),
  ]
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
  return <div className="nle">
    <div className="nle-toolbar">
      <span className="nle-clock">{clock(now, true)}</span>
      <span className="nle-duration">/ {clock(seconds)}</span>
      <label className="nle-zoom">縮放<input type="range" min={0} max={6} step={1} value={zoom} onChange={(event) => setZoom(Number(event.target.value))} aria-label="時間軸縮放" /></label>
      <button type="button" className="button ghost" onClick={() => setZoom(0)} disabled={zoom === 0}>整部片</button>
    </div>
    <div className="nle-scroll" ref={scroller}>
      <div className="nle-canvas" style={{ width: width + HEAD }}
        onPointerDown={(event) => { if ((event.target as HTMLElement).closest('.nle-heads, .nle-marker')) return; dragging.current = true; event.currentTarget.setPointerCapture(event.pointerId); scrub(event) }}
        onPointerMove={(event) => { if (dragging.current) scrub(event) }}
        onPointerUp={() => { dragging.current = false }}>
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
          <div className="nle-lane" style={{ width }}>
            {track.spans.map((span, index) => <div key={span.id} className="nle-clip" style={{ left: x(span.start), width: Math.max(2, x(span.end) - x(span.start)), ...(track.kind === 'shot' && thumbs[index] ? { backgroundImage: `url(${thumbs[index]})` } : {}) }}
              title={`${clock(span.start / fps)}–${clock(span.end / fps)} ${span.label}`}>
              {x(span.end) - x(span.start) > 44 && <span>{span.label}</span>}
            </div>)}
          </div>
        </div>)}
        <span className="nle-playhead" style={{ left: HEAD + now * perSecond }} aria-hidden="true" />
      </div>
    </div>
  </div>
}
