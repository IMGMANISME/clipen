import { PowerNotice } from './PowerNotice'
import { useEffect, useId, useRef, useState } from 'react'
import { editProduction, exportProduction, getProductionWorkbench, refineProduction, renderProduction } from './api'
import type { FilmPreview, FilmVersion, ProductionWorkbenchState } from './api'
import { ProductionMusicPicker } from './ProductionMusicPicker'
import { PromptHistory } from './PromptHistory'
import { usePromptDraft } from './usePromptDraft'
import { ReviewNotes } from './ReviewNotes'
import { clock, composePrompt, describeNote } from './reviewModel'
import { providerName, useProvider } from './providerContext'
import { overallInstruction } from './reviewDraft'

type Props = { visible: boolean; runProvider?: string; projectId: string; csrf: string; runId: string; revisionId: string; active: boolean; onChanged: () => void; musicId: string; previews: Array<FilmPreview | null | undefined>; onMusicChange: (id: string) => void }
const MAX_INSTRUCTION = 8000
/** Review the film, note what to change, and hand the notes to the AI. Fine manual editing belongs in an editing app. */
export function ProductionWorkbench({ visible, projectId, csrf, runId, revisionId, active, onChanged, musicId, onMusicChange, previews, runProvider }: Props) {
  const provider = useProvider()
  // A revision may hand the draft to the AI Clipen was started with.
  const switching = Boolean(runProvider && provider.id && runProvider !== provider.id)
  const headingId = useId()
  const [data, setData] = useState<ProductionWorkbenchState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [instruction, setInstruction] = usePromptDraft(projectId, 'refinement', '', runId)
  const [consent, setConsent] = useState(false)
  const [noteOpen, setNoteOpen] = useState(false)
  const [exportUrl, setExportUrl] = useState('')
  const [historyId, setHistoryId] = useState('')
  const [copied, setCopied] = useState(false)
  const [versionPreview, setVersionPreview] = useState<FilmVersion | null>(null)
  const [previewError, setPreviewError] = useState('')
  const versionPlayer = useRef<HTMLVideoElement>(null)
  useEffect(() => { if (!visible) versionPlayer.current?.pause() }, [visible])
  useEffect(() => {
    let disposed = false
    void getProductionWorkbench(projectId, runId).then((next) => { if (!disposed) { setData(next); setExportUrl('') } }).catch((e) => { if (!disposed) setError(String(e)) })
    return () => { disposed = true }
  }, [projectId, runId, revisionId])
  // Note times are positions in the current version, so review only a film rendered from it.
  const video = previews.find((preview) => preview?.state === 'succeeded' && preview.revision_id === data?.revision.revision_id) ?? null
  const notes = data?.references?.items ?? []
  const prompt = data ? composePrompt(instruction, notes, data.revision.timeline) : ''
  async function action(work: () => Promise<unknown>, success: string) {
    if (busy || active || !data) return
    setBusy(true); setError(''); setMessage('')
    try { await work(); setMessage(success) }
    catch (e) { setError(e instanceof Error ? e.message : '這次操作未完成。') }
    finally {
      try { setData(await getProductionWorkbench(projectId, runId)) } catch { /* Keep the last visible state. */ }
      onChanged(); setBusy(false)
    }
  }
  function send() {
    if (!data) return
    // The notes travel as structured data too, so a very long list can fall back to the overall request.
    const text = prompt.length <= MAX_INSTRUCTION ? prompt : `${instruction.trim() || '請依審片注記修改目前影片，其他部分保留。'}\n\n（共 ${notes.length} 筆審片注記，詳見附帶的注記資料。）`.slice(0, MAX_INSTRUCTION)
    void action(async () => { await refineProduction(csrf, projectId, runId, data.revision.revision_id, text, musicId || undefined, data.references?.version ?? 0); setInstruction(''); setConsent(false) }, 'AI 已開始依你的注記修改。')
  }
  async function copy() {
    try { await navigator.clipboard.writeText(prompt); setCopied(true); window.setTimeout(() => setCopied(false), 2000) }
    catch { setError('無法複製到剪貼簿，請直接選取文字複製。') }
  }
  return <section className="production-workbench" aria-labelledby={headingId}>
    <div className="workbench-heading"><div><span className="studio-section-number">02 / REVIEW</span><h3 id={headingId}>邊看邊記，交給 AI 修改。</h3>
      <p>看片時把要改、要拿掉、覺得可以的地方記下來，最後連同整體方向一起交給 AI。</p></div></div>
    <div className="workbench-content">
    {active && <p role="status">AI 正在修改這支影片，完成或停止後即可繼續記注記。</p>}
    {error && <p className="inline-message error" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {data && !video && <div className="review-stale" role="status"><p>目前這一版還沒有可播放的影片，先用下方畫面逐格查看；也可以先輸出影片再審片。</p>
      <button type="button" className="button secondary" disabled={busy || active} onClick={() => void action(() => renderProduction(csrf, projectId, runId, data.revision.revision_id), '已開始輸出這一版影片，完成後就能播放審片。')}>輸出這一版影片</button></div>}
    {data && <ReviewNotes visible={visible} projectId={projectId} csrf={csrf} runId={runId} data={data} video={video} disabled={busy || active} onChanged={(references) => setData((current) => current ? { ...current, references } : current)} onDirty={setNoteOpen} />}
    <fieldset disabled={busy || active || !data}>
      <legend>交給 AI 修改</legend>
      <label>這一版要改什麼？<textarea aria-label="這一版要改什麼？" value={instruction} maxLength={MAX_INSTRUCTION} onChange={(e) => setInstruction(e.target.value)} placeholder="整體方向，例如：節奏再快一點，早餐段落縮短；其他照注記改。" rows={3} /></label>
      <PromptHistory projectId={projectId} csrf={csrf} kind="refinement" text={instruction} onUse={(value) => setInstruction(overallInstruction(value))} disabled={busy || active} />
      {prompt && <details className="review-prompt"><summary>AI 會收到的改片內容{notes.length ? `（${notes.length} 筆注記）` : ''}</summary>
        <pre>{prompt}</pre>
        {prompt.length > MAX_INSTRUCTION && <p className="prompt-draft-hint">注記太多，文字只會送出整體方向；每筆注記仍會完整交給 AI。</p>}
        <button type="button" className="button ghost" onClick={() => void copy()}>{copied ? '已複製' : '複製這段文字'}</button></details>}
      <ProductionMusicPicker projectId={projectId} csrf={csrf} value={musicId} onChange={onMusicChange} disabled={busy || active} defaultLabel="沿用目前配樂" />
      {noteOpen && <p role="status">還有一筆注記沒記下，請先按「記下這筆」或取消。</p>}
      {switching && <p className="workbench-provider-switch" role="note">這支片原本由 {providerName(runProvider)} 製作。這次修改會改由 {provider.name} 接手，從目前的版本繼續，使用 {provider.name} 的額度。</p>}
      <PowerNotice />
      <label className="consent-check"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />同意使用 {provider.name} 額度修改目前影片。</label>
      <button type="button" className="button primary" disabled={!consent || noteOpen || !prompt} onClick={send}>交給 AI 修改</button>
    </fieldset>
    {data?.sent_notes && <details className="review-sent"><summary>上一次交給 AI 的修改</summary>
      {data.sent_notes.instruction && <pre>{data.sent_notes.instruction}</pre>}
      {!data.sent_notes.instruction?.includes('審片注記') && data.sent_notes.items.length > 0 && <ol>{data.sent_notes.items.map((item) => <li key={item.reference_id}>{describeNote(item, data.revision.timeline).line}</li>)}</ol>}
    </details>}
    <details className="workbench-versions-panel" onToggle={(event) => { if (!event.currentTarget.open) versionPlayer.current?.pause() }}><summary>版本紀錄與還原</summary>
      <p className="workbench-versions-note">每次 AI 製作或修改結束、每次還原，各算一個版本。</p>
      {data?.history.length ? <ol className="workbench-versions">{data.history.map((version) => {
        const current = version.revision_id === data.revision.revision_id
        return <li key={version.revision_id} className={`is-${version.kind} ${current ? 'is-current' : ''}`}>
          <label><input type="radio" name="workbench-version" value={version.revision_id} checked={historyId === version.revision_id} disabled={current} onChange={() => setHistoryId(version.revision_id)} />
            <span><strong>版本 {version.number}　{version.label}</strong>{current && <em>目前</em>}
              <small>{new Date(version.created_at).toLocaleString('zh-TW', { hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}{version.duration_seconds != null ? ` · ${clock(version.duration_seconds)}` : ''}{version.detail ? `　「${version.detail.length > 40 ? `${version.detail.slice(0, 40)}…` : version.detail}」` : ''}</small></span></label>
          {version.preview_url ? <button type="button" className="button ghost" onClick={() => { setVersionPreview(version); setPreviewError('') }}>預覽版本 {version.number}</button> : <small>這版尚無可播放的輸出</small>}
        </li>
      })}</ol> : <p>還沒有完成的版本。</p>}
      {versionPreview?.preview_url && <div className="version-preview"><p>正在比較版本 {versionPreview.number}，目前影片不會被還原。</p><video key={versionPreview.preview_url} ref={versionPlayer} controls preload="metadata" src={versionPreview.preview_url} aria-label={`版本 ${versionPreview.number} 預覽`} onError={() => setPreviewError('這版的預覽檔暫時無法讀取。')} />{previewError && <p role="status">{previewError}</p>}<button type="button" className="button ghost" onClick={() => setVersionPreview(null)}>關閉版本預覽</button></div>}
      <button type="button" className="button secondary" disabled={!historyId || busy || active} onClick={() => void action(() => editProduction(csrf, projectId, runId, data!.revision.revision_id, 'restore_revision', { revision_id: historyId, summary: '使用者還原影片版本' }), '已還原。輸出這一版影片後就能審片。')}>還原選定版本</button>
    </details>
    <details className="workbench-export"><summary>匯出剪輯資料</summary>
      <p>要逐格精修，之後會支援匯出到 DaVinci Resolve。目前可以下載 Clipen 的剪輯資料（文字、效果、時間軸與來源）。</p>
      <button type="button" className="button secondary" disabled={busy || active || !data} onClick={() => void action(async () => { const result = await exportProduction(csrf, projectId, runId); setExportUrl(result.download_url) }, '剪輯資料已準備好。')}>準備剪輯資料</button>
      {exportUrl && <a href={exportUrl} download>下載剪輯資料</a>}
    </details>
    </div>
  </section>
}
