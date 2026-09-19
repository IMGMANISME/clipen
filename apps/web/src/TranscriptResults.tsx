import { useEffect, useState } from 'react'
import { getAnalysisResults, getTranscriptSegments } from './api'
import type {
  AnalysisResultItem,
  AnalysisResultsPage,
  AnalysisRun,
  TranscriptPage,
} from './api'

const statusCopy: Record<AnalysisResultItem['status'], string> = {
  pending: '尚未處理',
  prepared: '等待辨識',
  transcribed: '有逐字稿',
  needs_review: '需要核對',
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

type TranscriptView = {
  state: 'loading' | 'ready' | 'failed'
  page?: TranscriptPage
  message?: string
  loadingMore?: boolean
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

function confidenceLabel(confidence: string | null): string | null {
  if (confidence === null) return null
  const value = Number(confidence)
  return Number.isFinite(value) && value >= 0 && value <= 1
    ? `信心 ${Math.round(value * 100)}%`
    : null
}

export function TranscriptResults({
  projectId,
  run,
  refreshToken = 0,
}: {
  projectId: string
  run: AnalysisRun
  refreshToken?: number
}) {
  const [offset, setOffset] = useState(0)
  const [results, setResults] = useState<ResultsView | null>(null)
  const [transcripts, setTranscripts] = useState<Record<string, TranscriptView>>({})
  const requestKey = `${run.run_id}:${offset}:${refreshToken}`
  const currentResults = results?.requestKey === requestKey ? results : null
  const page = currentResults?.state === 'ready' ? currentResults.page ?? null : null
  const loading = currentResults === null
  const error = currentResults?.state === 'failed' ? currentResults.message ?? '無法讀取逐字稿清單。' : null

  useEffect(() => {
    let mounted = true
    getAnalysisResults(projectId, run.run_id, offset)
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
  }, [offset, projectId, requestKey, run.run_id])

  function openTranscript(item: AnalysisResultItem, open: boolean) {
    if (!open || !['transcribed', 'needs_review'].includes(item.status)) return
    const key = itemKey(item)
    if (transcripts[key]) return
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
          <h3 id="transcript-results-title">逐字稿與問題清單</h3>
          <p>需要處理的素材排在最前面。展開成功項目即可核對原始時間與辨識文字。</p>
        </div>
        {page && <span>{page.total.toLocaleString()} 個項目</span>}
      </div>

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
              const hasTranscript = ['transcribed', 'needs_review'].includes(item.status)
              const sourceTime = item.source && item.preview_start_pts
                ? formatSourceTime(item.preview_start_pts, item.source.time_base)
                : null
              return (
                <details
                  className={`transcript-item status-${item.status}`}
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
                      {item.preview_text && <p>{item.preview_text}{item.preview_truncated ? '…' : ''}</p>}
                    </div>
                    <div className="transcript-item-meta">
                      {sourceTime && <span>{sourceTime} 起</span>}
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
                    {hasTranscript && transcript?.state === 'ready' && transcript.page && (
                      <>
                        <ol className="transcript-segments">
                          {transcript.page.segments.map((segment) => (
                            <li key={segment.ordinal}>
                              <time>{formatSourceTime(segment.start_pts, transcript.page!.time_base)}</time>
                              <p>{segment.text || '（空白片段）'}</p>
                              {confidenceLabel(segment.confidence) && <span>{confidenceLabel(segment.confidence)}</span>}
                            </li>
                          ))}
                        </ol>
                        {transcript.page.next_offset !== null && (
                          <button
                            className="button secondary"
                            onClick={() => loadMore(item)}
                            disabled={transcript.loadingMore}
                          >
                            {transcript.loadingMore ? '正在載入…' : '載入更多逐字稿'}
                          </button>
                        )}
                        {transcript.message && <p role="alert" className="transcript-issue">{transcript.message}</p>}
                      </>
                    )}
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
