import { useEffect, useRef, useState } from 'react'
import { getEditingFrame } from './api'
import type { EditAgentTimeline, EditAgentStatus } from './api'

type Preview = NonNullable<EditAgentStatus['production']>['preview']
type Props = { projectId: string; csrf: string; runId: string; revisionId: string; timeline: EditAgentTimeline; selected?: { item_id: string; start_frame: number; duration_frames: number; kind: string }; override?: Record<string, unknown>; canMove: boolean; onMove: (x: number, y: number) => void; preview: Preview; referenceOnly?: boolean }
export function VisualEditorPreview({ projectId, csrf, runId, revisionId, timeline, selected, override, canMove, onMove, preview, referenceOnly = false }: Props) {
  const fps = timeline.frame_rate.num / timeline.frame_rate.den
  const [frame, setFrame] = useState(() => selected ? selected.start_frame + Math.floor((selected.duration_frames - 1) / 2) : 0)
  const [image, setImage] = useState('')
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [playing, setPlaying] = useState(false)
  const video = useRef<HTMLVideoElement>(null)
  const draft = JSON.stringify(override)
  const validFrame = Math.max(0, Math.min(timeline.duration_frames - 1, frame))
  useEffect(() => {
    const abort = new AbortController()
    const timer = window.setTimeout(() => {
      setBusy(true); setError('')
      void getEditingFrame(csrf, projectId, runId, revisionId, validFrame, draft ? JSON.parse(draft) as Record<string, unknown> : undefined, abort.signal)
        .then((value) => { if (!abort.signal.aborted) { setImage(value.image_data_url); setBusy(false) } })
        .catch((e) => { if (!abort.signal.aborted) { setError(e instanceof Error ? e.message : '暫時無法讀取編輯畫面。'); setBusy(false) } })
    }, 350)
    return () => { window.clearTimeout(timer); abort.abort() }
  }, [csrf, projectId, runId, revisionId, validFrame, draft])
  function move(event: React.PointerEvent<HTMLDivElement>) {
    if (!canMove || playing) return
    const bounds = event.currentTarget.getBoundingClientRect()
    onMove(Math.max(0, Math.min(.95, (event.clientX - bounds.left) / bounds.width)), Math.max(0, Math.min(.9, (event.clientY - bounds.top) / bounds.height)))
  }
  function seek(value: number) {
    setFrame(value)
    if (video.current) video.current.currentTime = value / fps
  }
  return <div className="workbench-live-preview">
    <div className="workbench-preview-heading"><strong>{referenceOnly ? '參考畫面' : '編輯畫面'}</strong><span>{(validFrame / fps).toFixed(2)} 秒</span></div>
    <div className={`workbench-preview-canvas ${canMove && !playing ? 'is-movable' : ''}`} style={{ aspectRatio: `${timeline.canvas.width}/${timeline.canvas.height}` }}
      onPointerDown={(e) => { if (canMove && !playing) { e.currentTarget.setPointerCapture(e.pointerId); move(e) } }}
      onPointerMove={(e) => { if (e.buttons === 1) move(e) }}>
      {playing && preview?.state === 'succeeded' ? <video ref={video} controls src={preview.media_url} onLoadedMetadata={(e) => { e.currentTarget.currentTime = validFrame / fps }} onSeeked={(e) => setFrame(Math.round(e.currentTarget.currentTime * fps))} aria-label="目前影片完整預覽" />
        : image ? <img src={image} alt={`目前版本 ${(validFrame / fps).toFixed(2)} 秒的畫面與文字元件`} draggable={false} /> : <div className="workbench-frame-placeholder">{busy ? '正在取得這個時間點的畫面…' : '這個時間點尚無可用畫面。'}</div>}
      {!playing && busy && image && <span className="workbench-frame-loading" role="status">更新畫面中…</span>}
      {!playing && canMove && <span className="workbench-position-marker" style={{ left: `${Number(override?.x ?? .1) * 100}%`, top: `${Number(override?.y ?? .1) * 100}%` }} aria-hidden="true" />}
    </div>
    {error && <p className="inline-message error" role="alert">{error}</p>}
    <label>查看時間<input type="range" min={0} max={Math.max(0, timeline.duration_frames - 1)} step={1} value={validFrame} onChange={(e) => seek(Number(e.target.value))} /></label>
    <p>{referenceOnly ? '對照畫面確認要標記的片段；下方可播放影片檢查聲音。' : canMove && !playing ? '拖曳畫面可移動選定元件；也可用右側位置滑桿微調。' : '選擇鏡頭、字幕或元件，會自動定位到它出現的時間。'}</p>
    <p className="workbench-preview-note">{referenceOnly ? '目前顯示已儲存的影片；參考註記會在送出 AI 修訂後套用。' : '編輯畫面會預覽文字與位置，尚未儲存的調整也會顯示；轉場、動態與聲音請播放完整影片確認。'}</p>
    {preview?.state === 'succeeded' && <button type="button" className="button secondary" onClick={() => setPlaying((value) => !value)}>{playing ? '返回即時編輯畫面' : '播放完整影片預覽'}</button>}
    {playing && <p>完整影片只包含已儲存並輸出的內容。{referenceOnly ? '參考註記尚未套用。' : '未儲存的文字與位置請返回編輯畫面查看。'}</p>}
    {playing && preview?.stale && <p role="status">這是較早版本的影片，尚未套用目前修改。儲存後按「更新影片預覽」。</p>}
  </div>
}
