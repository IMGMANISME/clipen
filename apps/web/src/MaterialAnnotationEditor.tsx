import { useMemo, useRef, useState } from 'react'
import { saveMaterialAnnotation } from './api'
import type { DayPart, MaterialAnnotationState, MaterialUsage, TranscriptPage } from './api'

const dayParts: Array<{ value: DayPart; label: string }> = [
  { value: 'dawn', label: '清晨' },
  { value: 'morning', label: '早上' },
  { value: 'noon', label: '中午' },
  { value: 'afternoon', label: '下午' },
  { value: 'evening', label: '傍晚' },
  { value: 'night', label: '晚上' },
  { value: 'late_night', label: '深夜' },
]

const usages: Array<{ value: MaterialUsage; label: string; help: string }> = [
  { value: 'normal', label: '交給 AI 判斷', help: '依剪輯計畫選用' },
  { value: 'must_use', label: '優先使用', help: '這段對成片很重要' },
  { value: 'ambient_only', label: '只取畫面', help: '不採用這段逐字稿' },
  { value: 'exclude', label: '不要使用', help: '排除這段素材' },
]

function parsePeople(value: string): string[] {
  return [...new Set(value.split(/[,，、\n]/).map((item) => item.trim()).filter(Boolean))]
}

function applySuggestion(initial: MaterialAnnotationState): MaterialAnnotationState {
  if (initial.material.revision !== 0) return initial
  return {
    ...initial,
    material: {
      ...initial.material,
      location: initial.suggestion.location,
      trip_day: initial.suggestion.trip_day,
      day_part: initial.suggestion.day_part,
    },
  }
}

export function MaterialAnnotationEditor({
  csrf,
  projectId,
  initial,
  transcript,
  mediaKind,
  formatTime,
  onSaved,
}: {
  csrf: string
  projectId: string
  initial: MaterialAnnotationState
  transcript?: TranscriptPage
  mediaKind: 'video' | 'audio'
  formatTime: (pts: string) => string
  onSaved: (annotation: MaterialAnnotationState) => void
}) {
  const base = useMemo(() => applySuggestion(initial), [initial])
  const [annotation, setAnnotation] = useState(base)
  const [people, setPeople] = useState(initial.material.people.join('、'))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [playbackError, setPlaybackError] = useState(false)
  const player = useRef<HTMLMediaElement | null>(null)
  const corrected = useMemo(
    () => new Map(annotation.transcript?.corrections.map((item) => [item.ordinal, item.text]) ?? []),
    [annotation.transcript?.corrections],
  )
  const pending = useMemo(() => ({
    ...annotation,
    material: { ...annotation.material, people: parsePeople(people) },
  }), [annotation, people])
  const dirty = JSON.stringify(pending) !== JSON.stringify(base)
  const neverReviewed = initial.material.revision === 0 && (initial.transcript?.revision ?? 0) === 0
  const currentDayPart = dayParts.find((part) => part.value === annotation.material.day_part)?.label
  const contextSummary = [
    annotation.material.trip_day ? `第 ${annotation.material.trip_day} 天` : null,
    currentDayPart ?? null,
    annotation.material.location,
    ...parsePeople(people).slice(0, 2),
  ].filter(Boolean).join(' · ')

  function updateMaterial<K extends keyof MaterialAnnotationState['material']>(
    key: K,
    value: MaterialAnnotationState['material'][K],
  ) {
    setSaved(false)
    setAnnotation((current) => ({
      ...current,
      material: { ...current.material, [key]: value },
    }))
  }

  function updateTranscript(ordinal: number, original: string, text: string) {
    if (!annotation.transcript) return
    const corrections = annotation.transcript.corrections.filter((item) => item.ordinal !== ordinal)
    if (text !== original) corrections.push({ ordinal, text })
    corrections.sort((left, right) => left.ordinal - right.ordinal)
    setSaved(false)
    setAnnotation((current) => ({
      ...current,
      transcript: current.transcript && { ...current.transcript, corrections, confirmed: false },
    }))
  }

  async function persist(candidate: MaterialAnnotationState) {
    setSaving(true)
    setError(null)
    try {
      const result = await saveMaterialAnnotation(csrf, projectId, candidate)
      setAnnotation(result)
      setPeople(result.material.people.join('、'))
      setSaved(true)
      onSaved(result)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '無法保存素材資訊。')
    } finally {
      setSaving(false)
    }
  }

  async function save() {
    await persist(pending)
  }

  async function setTranscriptDiscarded(discarded: boolean) {
    if (!annotation.transcript) return
    if (discarded && !window.confirm('確定要移除這份逐字稿？\n\n素材與原始辨識紀錄不會被刪除，之後仍可恢復；移除後 AI 不會使用這份文字。')) return
    const candidate: MaterialAnnotationState = {
      ...pending,
      transcript: {
        ...annotation.transcript,
        discarded,
        confirmed: discarded ? false : annotation.transcript.confirmed,
      },
    }
    setAnnotation(candidate)
    setSaved(false)
    await persist(candidate)
  }

  function seek(pts: string) {
    if (!player.current || !transcript) return
    const ticks = Number(BigInt(pts) - BigInt(transcript.source_start_pts))
    player.current.currentTime = Math.max(0, ticks * transcript.time_base.num / transcript.time_base.den)
    void player.current.play()
  }

  return (
    <div className="annotation-editor">
      <div className="annotation-workbench">
        <div className="material-player">
          {mediaKind === 'video'
            ? <video ref={(node) => { player.current = node }} controls preload="metadata" onError={() => setPlaybackError(true)} src={`/api/projects/${projectId}/media/${initial.material.version_id}`}>
                目前的瀏覽器無法直接播放這個素材。
              </video>
            : <audio ref={(node) => { player.current = node }} controls preload="metadata" onError={() => setPlaybackError(true)} src={`/api/projects/${projectId}/media/${initial.material.version_id}`}>
                目前的瀏覽器無法直接播放這個素材。
              </audio>}
          <p>{playbackError ? '無法直接播放。請確認外接硬碟仍連接；若素材編碼不受瀏覽器支援，後續可建立本機預覽檔。' : '本機唯讀播放，不會上傳或建立副本。'}</p>
        </div>
        <div className="annotation-decision">
          <div className="annotation-intro">
            <div><strong>整理這段素材</strong><p>先決定怎麼使用，再補充必要線索。</p></div>
            <div className="annotation-origin">
              {initial.material.revision === 0 && initial.suggestion.capture_time && <span>拍攝於 {initial.suggestion.capture_time.replace('T', ' ').slice(5, 16)}</span>}
            </div>
          </div>

          <fieldset className="annotation-usage">
            <legend>這段要怎麼用？</legend>
            <div className="usage-options">
              {usages.map((usage) => (
                <label className={annotation.material.usage === usage.value ? 'is-selected' : ''} key={usage.value}>
                  <input
                    type="radio"
                    name={`usage-${initial.material.version_id}`}
                    value={usage.value}
                    checked={annotation.material.usage === usage.value}
                    onChange={() => updateMaterial('usage', usage.value)}
                  />
                  <span><strong>{usage.label}</strong><small>{usage.help}</small></span>
                </label>
              ))}
            </div>
          </fieldset>
        </div>
      </div>

      <details className="annotation-context-disclosure">
        <summary>
          <div>
            <strong>場景資訊與備註</strong>
            <span>{contextSummary || '沒有補充資訊'}</span>
          </div>
          <span className="disclosure-action"><span className="when-closed">選填 · 展開</span><span className="when-open">收合</span></span>
        </summary>
        <div className="annotation-context">
            <div className="annotation-grid compact">
              <label className="annotation-field">
                <span>人物</span>
                <input value={people} maxLength={800} placeholder="例如：我、朋友" onChange={(event) => { setPeople(event.target.value); setSaved(false) }} />
              </label>
              <label className="annotation-field">
                <span>地點 {initial.material.revision === 0 && initial.suggestion.location && <small className="auto-label">自動</small>}</span>
                <input value={annotation.material.location ?? ''} maxLength={160} placeholder="未指定" onChange={(event) => updateMaterial('location', event.target.value || null)} />
              </label>
              <label className="annotation-field">
                <span>旅行日 {initial.material.revision === 0 && initial.suggestion.capture_time && <small className="auto-label">自動</small>}</span>
                <div className="day-input"><span>第</span><input aria-label="旅行第幾天" type="number" min="1" max="365" value={annotation.material.trip_day ?? ''} placeholder="—" onChange={(event) => updateMaterial('trip_day', event.target.value ? Number(event.target.value) : null)} /><span>天</span></div>
              </label>
              <label className="annotation-field">
                <span>時段 {initial.material.revision === 0 && initial.suggestion.capture_time && <small className="auto-label">自動</small>}</span>
                <select value={annotation.material.day_part ?? ''} onChange={(event) => updateMaterial('day_part', (event.target.value || null) as DayPart | null)}>
                  <option value="">未指定</option>
                  {dayParts.map((part) => <option key={part.value} value={part.value}>{part.label}</option>)}
                </select>
              </label>
            </div>

          <label className="annotation-field annotation-note">
            <span>給剪輯 AI 的備註 <small>選填</small></span>
            <textarea rows={2} maxLength={2000} value={annotation.material.note} placeholder="例如：進店前的轉場，希望保留笑聲" onChange={(event) => updateMaterial('note', event.target.value)} />
            <small>{annotation.material.note.length.toLocaleString()} / 2,000</small>
          </label>
        </div>
      </details>

      {transcript && annotation.transcript && (
        <div className="transcript-review">
          {annotation.transcript.stale && (
            <p className="annotation-stale" role="status">逐字稿已重新分析；舊修正沒有自動套用，請依目前文字重新核對。</p>
          )}
          <div className="transcript-review-heading">
            <div>
              <strong>{annotation.transcript.discarded ? '逐字稿已移除' : '核對逐字稿'}</strong>
              <p>{annotation.transcript.discarded ? 'AI 不會使用這份文字；素材仍可依畫面內容參與剪輯。' : '只改錯字；時間碼仍以原始分析為準。'}</p>
            </div>
            <div className="transcript-heading-actions">
              {!annotation.transcript.discarded && <label className="confirm-transcript">
                <input type="checkbox" disabled={transcript.next_offset !== null} checked={annotation.transcript.confirmed} onChange={(event) => {
                  setSaved(false)
                  setAnnotation((current) => ({ ...current, transcript: current.transcript && { ...current.transcript, confirmed: event.target.checked } }))
                }} />
                <span>{transcript.next_offset === null ? '我已核對全部文字' : '載入全部文字後可確認'}</span>
              </label>}
              <button
                className={`button transcript-discard ${annotation.transcript.discarded ? 'restore' : ''}`}
                type="button"
                disabled={saving}
                onClick={() => void setTranscriptDiscarded(!annotation.transcript?.discarded)}
              >
                {annotation.transcript.discarded ? '恢復逐字稿' : '移除逐字稿'}
              </button>
            </div>
          </div>
          {annotation.transcript.discarded ? (
            <div className="transcript-discarded-message">
              <strong>已從剪輯計畫的文字線索中移除</strong>
              <p>適合背景談話、路人聲音或與作品無關的收音。原始影片與辨識資料都還在。</p>
            </div>
          ) : <ol className="transcript-segments editable">
            {transcript.segments.map((segment) => {
              const edited = corrected.get(segment.ordinal)
              const removed = edited === ''
              return (
                <li className={removed ? 'is-removed' : ''} key={segment.ordinal}>
                  <button className="transcript-seek" type="button" onClick={() => seek(segment.start_pts)}>{formatTime(segment.start_pts)}</button>
                  {removed
                    ? <div className="transcript-segment-removed"><strong>這段逐字稿已移除</strong><span>不會提供給剪輯 AI</span></div>
                    : <textarea
                        rows={2}
                        aria-label={`逐字稿片段 ${segment.ordinal + 1}`}
                        value={edited ?? segment.text}
                        onChange={(event) => updateTranscript(segment.ordinal, segment.text, event.target.value)}
                      />}
                  <div className="transcript-segment-actions">
                    {!removed && <span className={corrected.has(segment.ordinal) ? 'corrected' : ''}>{corrected.has(segment.ordinal) ? '已修正' : '原始辨識'}</span>}
                    <button
                      type="button"
                      className={`transcript-segment-remove ${removed ? 'restore' : ''}`}
                      onClick={() => updateTranscript(segment.ordinal, segment.text, removed ? segment.text : '')}
                    >{removed ? '恢復這段' : '移除這段'}</button>
                  </div>
                </li>
              )
            })}
          </ol>}
        </div>
      )}

      <div className={`annotation-actions ${dirty ? 'has-changes' : ''}`}>
        <div aria-live="polite">
          {error && <p className="form-error" role="alert">{error}</p>}
          {saved && <p className="save-success">已保存。AI 製作時會參考這些標註。</p>}
          {!error && !saved && dirty && <p className="unsaved-hint">有尚未保存的修改</p>}
        </div>
        {dirty || saving
          ? <button className="button primary" type="button" disabled={saving} onClick={() => void save()}>{saving ? '正在保存…' : '保存修改'}</button>
          : neverReviewed
            ? <button className="button secondary mark-reviewed" type="button" onClick={() => void save()}>標記為已看過</button>
          : <span className="annotation-current">✓ 目前設定已套用</span>}
      </div>
    </div>
  )
}
