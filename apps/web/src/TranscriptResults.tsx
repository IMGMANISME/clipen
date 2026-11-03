import { useEffect, useState } from 'react'
import { getAnalysisResults, getMaterialAnnotation, getTranscriptSegments } from './api'
import { MaterialAnnotationEditor } from './MaterialAnnotationEditor'
import type {
  AnalysisResultItem,
  AnalysisResultsPage,
  AnalysisRun,
  MaterialAnnotationState,
  TranscriptPage,
} from './api'

const statusCopy: Record<AnalysisResultItem['status'], string> = {
  pending: '尚未處理',
  prepared: '等待辨識',
  transcribed: '有逐字稿',
  needs_review: '辨識不確定',
  no_audio: '沒有音軌',
  no_speech: '未偵測到人聲',
  failed: '需要處理',
}

const issueCopy: Record<string, string> = {
  root_changed: '外接磁碟重新掛載後的素材位置無法確認，這個項目沒有進行辨識。',
  root_unavailable: '目前無法讀取素材磁碟，這個項目沒有進行辨識。',
  source_changed: '分析期間素材有變動，這份結果未被採用。',
  audio_decode_failed: '無法從這個素材讀取音訊。',
  audio_decode_timeout: '音訊準備時間過長，已安全停止這個項目。',
  audio_output_missing: '沒有取得可供辨識的音訊。',
  audio_artifact_too_large: '音訊衍生成果超過安全大小限制。',
  audio_time_mapping_invalid: '無法把音訊時間可靠地對回原始素材。',
  normalized_audio_invalid: '轉換後的音訊格式無法驗證。',
  ffmpeg_unavailable: '找不到 FFmpeg，無法準備音訊。',
  ffmpeg_unsupported: '目前的 FFmpeg 版本不支援這個素材。',
  ffmpeg_output_too_large: '音訊轉換輸出超過安全大小限制。',
  asr_failed: '本機語音辨識沒有完成這個項目。',
  asr_timeout: '語音辨識時間過長，已安全停止這個項目。',
  asr_result_invalid: '語音辨識結果格式無法驗證。',
  asr_result_too_large: '逐字稿結果超過安全大小限制。',
  asr_output_too_large: '語音辨識輸出超過安全大小限制。',
  asr_model_changed: '辨識期間模型內容有變動，結果未被採用。',
  asr_model_unavailable: '本機語音辨識模型暫時無法使用。',
  asr_runtime_unavailable: '本機語音辨識環境暫時無法使用。',
  asr_runtime_unsupported: '目前的語音辨識環境不受支援。',
  transcript_low_confidence: '原始收音與有限的增強比對沒有得到穩定結果，或無法確認人聲是否屬於拍攝主體。這份逐字稿不會被當成可信口語，請搭配畫面與原音確認。',
  transcript_quality_unverified: '這是舊版產生的逐字稿，品質尚未驗證，請先核對。',
  preparation_item_internal_error: '處理這個項目時發生未預期的本機錯誤。',
}

const usageCopy = {
  normal: '一般素材',
  must_use: '優先使用',
  exclude: '不要使用',
  ambient_only: '只取畫面／環境音',
} as const

const userStateCopy: Record<AnalysisResultItem['user_state'], string> = {
  pending: '尚未處理',
  reviewed: '已看過',
  confirmed: '已核對',
  priority: '優先使用',
  excluded: '不要使用',
}

type TranscriptView = {
  state: 'loading' | 'ready' | 'failed'
  page?: TranscriptPage
  message?: string
  loadingMore?: boolean
}

type AnnotationView = {
  state: 'loading' | 'ready' | 'failed'
  annotation?: MaterialAnnotationState
  message?: string
}

type ResultsView = {
  requestKey: string
  state: 'ready' | 'failed'
  page?: AnalysisResultsPage
  message?: string
}

function itemKey(item: AnalysisResultItem): string {
  return `${item.version_id}:${item.stream_index}`
}

function formatSourceTime(pts: string, timeBase: { num: number; den: number }): string {
  try {
    const milliseconds = BigInt(pts) * BigInt(timeBase.num) * 1000n / BigInt(timeBase.den)
    const negative = milliseconds < 0n
    let remaining = negative ? -milliseconds : milliseconds
    const hours = remaining / 3_600_000n
    remaining %= 3_600_000n
    const minutes = remaining / 60_000n
    remaining %= 60_000n
    const seconds = remaining / 1000n
    const millis = remaining % 1000n
    const main = hours > 0n
      ? `${hours}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
      : `${minutes}:${seconds.toString().padStart(2, '0')}`
    return `${negative ? '−' : ''}${main}.${millis.toString().padStart(3, '0')}`
  } catch {
    return '時間未知'
  }
}

export function TranscriptResults({
  csrf,
  projectId,
  run,
  refreshToken = 0,
}: {
  csrf: string
  projectId: string
  run: AnalysisRun
  refreshToken?: number
}) {
  const [offset, setOffset] = useState(0)
  const [tripDay, setTripDay] = useState<number | null>(null)
  const [reviewState, setReviewState] = useState<'all' | 'pending' | 'reviewed'>('all')
  const [listRevision, setListRevision] = useState(0)
  const [results, setResults] = useState<ResultsView | null>(null)
  const [transcripts, setTranscripts] = useState<Record<string, TranscriptView>>({})
  const [annotations, setAnnotations] = useState<Record<string, AnnotationView>>({})
  const requestKey = `${run.run_id}:${offset}:${tripDay ?? 'all'}:${reviewState}:${refreshToken}:${listRevision}`
  const currentResults = results?.requestKey === requestKey ? results : null
  const page = currentResults?.state === 'ready' ? currentResults.page ?? null : null
  const loading = currentResults === null
  const error = currentResults?.state === 'failed' ? currentResults.message ?? '無法讀取逐字稿清單。' : null

  useEffect(() => {
    let mounted = true
    getAnalysisResults(projectId, run.run_id, offset, { tripDay, reviewState })
      .then((result) => {
        if (mounted) setResults({ requestKey, state: 'ready', page: result })
      })
      .catch((caught) => {
        if (mounted) {
          setResults({
            requestKey,
            state: 'failed',
            message: caught instanceof Error ? caught.message : '無法讀取逐字稿清單。',
          })
        }
      })
    return () => { mounted = false }
  }, [offset, projectId, requestKey, reviewState, run.run_id, tripDay])

  function openTranscript(item: AnalysisResultItem, open: boolean) {
    if (!open) return
    const key = itemKey(item)
    if (!annotations[key]) {
      setAnnotations((current) => ({ ...current, [key]: { state: 'loading' } }))
      getMaterialAnnotation(projectId, item.version_id, item.stream_index)
        .then((annotation) => setAnnotations((current) => ({ ...current, [key]: { state: 'ready', annotation } })))
        .catch((caught) => setAnnotations((current) => ({ ...current, [key]: {
          state: 'failed',
          message: caught instanceof Error ? caught.message : '無法讀取素材資訊。',
        } })))
    }
    if (!['transcribed', 'needs_review'].includes(item.status) || transcripts[key]) return
    setTranscripts((current) => ({ ...current, [key]: { state: 'loading' } }))
    getTranscriptSegments(projectId, run.run_id, item.version_id, item.stream_index)
      .then((result) => {
        setTranscripts((current) => ({
          ...current,
          [key]: { state: 'ready', page: result },
        }))
      })
      .catch((caught) => {
        setTranscripts((current) => ({
          ...current,
          [key]: {
            state: 'failed',
            message: caught instanceof Error ? caught.message : '無法讀取這份逐字稿。',
          },
        }))
      })
  }

  function loadMore(item: AnalysisResultItem) {
    const key = itemKey(item)
    const current = transcripts[key]
    if (current?.state !== 'ready' || !current.page || current.page.next_offset === null) return
    setTranscripts((all) => ({ ...all, [key]: { ...current, loadingMore: true } }))
    getTranscriptSegments(
      projectId,
      run.run_id,
      item.version_id,
      item.stream_index,
      current.page.next_offset,
    )
      .then((next) => {
        setTranscripts((all) => ({
          ...all,
          [key]: {
            state: 'ready',
            page: {
              ...next,
              offset: 0,
              segments: [...current.page!.segments, ...next.segments],
            },
          },
        }))
      })
      .catch((caught) => {
        setTranscripts((all) => ({
          ...all,
          [key]: {
            ...current,
            loadingMore: false,
            message: caught instanceof Error ? caught.message : '無法讀取更多逐字稿。',
          },
        }))
      })
  }

  const pageNumber = page ? Math.floor(page.offset / page.limit) + 1 : 1
  const pageCount = page ? Math.max(1, Math.ceil(page.total / page.limit)) : 1

  return (
    <section className="transcript-results" aria-labelledby="transcript-results-title">
      <div className="transcript-results-heading">
        <div>
          <h3 id="transcript-results-title">挑重要素材，補充剪輯線索</h3>
          <p>不必消除所有「辨識不確定」。只處理你想放進成片、或 AI 容易誤解的片段。</p>
        </div>
        {page && <span>共 {page.total.toLocaleString()} 支素材</span>}
      </div>

      <ol className="review-guide" aria-label="素材核對方式">
        <li><span>1</span><div><strong>展開重要片段</strong><small>先聽原音、看完整毛片</small></div></li>
        <li><span>2</span><div><strong>只補確定資訊</strong><small>人物、地點與旅行時間都可留白</small></div></li>
        <li><span>3</span><div><strong>保存後繼續</strong><small>其餘素材交由 AI 自行判斷</small></div></li>
      </ol>

      {page && (
        <div className="review-filters" aria-label="篩選素材">
          <label>
            <span>旅行日</span>
            <select value={tripDay ?? ''} onChange={(event) => { setOffset(0); setTripDay(event.target.value ? Number(event.target.value) : null) }}>
              <option value="">全部日期</option>
              {(page.filters?.trip_days ?? []).map((item) => <option key={item.day} value={item.day}>第 {item.day} 天（{item.count}）</option>)}
            </select>
          </label>
          <label>
            <span>處理狀態</span>
            <select value={reviewState} onChange={(event) => { setOffset(0); setReviewState(event.target.value as 'all' | 'pending' | 'reviewed') }}>
              <option value="all">全部狀態</option>
              <option value="pending">尚未處理</option>
              <option value="reviewed">已處理</option>
            </select>
          </label>
          <span>符合條件：{page.total.toLocaleString()} 支</span>
        </div>
      )}

      {loading && <p className="transcript-list-state" role="status">正在讀取分析結果…</p>}
      {error && <p className="transcript-list-state has-error" role="alert">{error}</p>}

      {page && !loading && (
        <>
          <div className="transcript-list">
            {page.items.length === 0 && (
              <p className="transcript-list-state">這次分析沒有可顯示的項目。</p>
            )}
            {page.items.map((item) => {
              const key = itemKey(item)
              const transcript = transcripts[key]
              const annotation = annotations[key]
              const hasTranscript = ['transcribed', 'needs_review'].includes(item.status)
              const userState = item.user_state ?? 'pending'
              const sourceTime = item.source && item.preview_start_pts
                ? formatSourceTime(item.preview_start_pts, item.source.time_base)
                : null
              return (
                <details
                  className={`transcript-item status-${item.status} user-${userState}`}
                  key={key}
                  onToggle={(event) => openTranscript(item, event.currentTarget.open)}
                >
                  <summary>
                    <div className="transcript-item-main">
                      <strong>{item.display_name}</strong>
                      <span>
                        {item.role === 'source' ? '毛片' : '參考作品'}
                        {item.stream_index >= 0 ? ` · 音軌 ${item.stream_index}` : ''}
                      </span>
                      {annotation?.state === 'ready' && annotation.annotation && (
                        <div className="annotation-summary">
                          {annotation.annotation.material.usage !== 'normal' && <span>{usageCopy[annotation.annotation.material.usage]}</span>}
                          {annotation.annotation.material.trip_day && <span>第 {annotation.annotation.material.trip_day} 天</span>}
                          {annotation.annotation.material.location && <span>{annotation.annotation.material.location}</span>}
                          {annotation.annotation.material.people.map((person) => <span key={person}>{person}</span>)}
                          {annotation.annotation.transcript?.discarded && <span>逐字稿已移除</span>}
                          {annotation.annotation.transcript?.confirmed && <span>逐字稿已核對</span>}
                        </div>
                      )}
                      {item.preview_text && <p>{item.preview_text}{item.preview_truncated ? '…' : ''}</p>}
                    </div>
                    <div className="transcript-item-meta">
                      {sourceTime && <span>{sourceTime} 起</span>}
                      {item.trip_day && <span className="trip-day-badge">第 {item.trip_day} 天</span>}
                      {item.transcript_discarded && <span className="user-state state-discarded">逐字稿已移除</span>}
                      <span className={`user-state state-${userState}`}>{userStateCopy[userState]}</span>
                      <span className="transcript-status">{statusCopy[item.status]}</span>
                    </div>
                  </summary>

                  <div className="transcript-item-body">
                    {item.status === 'failed' && (
                      <p className="transcript-issue">{item.issue_code ? issueCopy[item.issue_code] ?? '這個項目沒有產生可靠逐字稿，可以重新分析後再確認。' : '這個項目沒有產生可靠逐字稿。'}</p>
                    )}
                    {item.status === 'no_speech' && <p>這段有音軌，但本機辨識沒有找到可轉錄的人聲。</p>}
                    {item.status === 'no_audio' && <p>這個素材沒有可供語音辨識的音軌。</p>}
                    {item.status === 'needs_review' && (
                      <p className="transcript-issue">{item.issue_code ? issueCopy[item.issue_code] ?? issueCopy.transcript_low_confidence : issueCopy.transcript_low_confidence}</p>
                    )}
                    {hasTranscript && transcript?.state === 'loading' && <p role="status">正在載入逐字稿…</p>}
                    {hasTranscript && transcript?.state === 'failed' && <p role="alert" className="transcript-issue">{transcript.message}</p>}
                    {annotation?.state === 'loading' && <p role="status">正在載入素材資訊…</p>}
                    {annotation?.state === 'failed' && <p role="alert" className="transcript-issue">{annotation.message}</p>}
                    {annotation?.state === 'ready' && annotation.annotation && (!hasTranscript || transcript?.state === 'ready') && (
                      <MaterialAnnotationEditor
                        key={`${annotation.annotation.material.revision}:${annotation.annotation.transcript?.revision ?? 0}`}
                        csrf={csrf}
                        projectId={projectId}
                        initial={annotation.annotation}
                        transcript={transcript?.page}
                        mediaKind={item.kind}
                        formatTime={(pts) => formatSourceTime(pts, transcript?.page?.time_base ?? { num: 1, den: 1 })}
                        onSaved={(saved) => {
                          setAnnotations((current) => ({ ...current, [key]: { state: 'ready', annotation: saved } }))
                          setListRevision((current) => current + 1)
                        }}
                      />
                    )}
                    {hasTranscript && transcript?.state === 'ready' && transcript.page?.next_offset !== null && (
                      <button className="button secondary load-transcript" onClick={() => loadMore(item)} disabled={transcript.loadingMore}>
                        {transcript.loadingMore ? '正在載入…' : '載入更多逐字稿'}
                      </button>
                    )}
                    {transcript?.message && <p role="alert" className="transcript-issue">{transcript.message}</p>}
                  </div>
                </details>
              )
            })}
          </div>

          <nav className="transcript-pagination" aria-label="逐字稿結果分頁">
            <button
              className="button secondary"
              disabled={page.offset === 0 || loading}
              onClick={() => setOffset(Math.max(0, page.offset - page.limit))}
            >上一頁</button>
            <span>第 {pageNumber} / {pageCount} 頁</span>
            <button
              className="button secondary"
              disabled={page.next_offset === null || loading}
              onClick={() => page.next_offset !== null && setOffset(page.next_offset)}
            >下一頁</button>
          </nav>
        </>
      )}
    </section>
  )
}
