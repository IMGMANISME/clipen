import { PowerNotice } from './PowerNotice'
import { useEffect, useId, useRef, useState } from 'react'
import { convertResolveTitles, exportProduction, getResolveTimelineStatus, exportToResolve, getProductionWorkbench, refineProduction, renderProduction, restoreProduction } from './api'
import type { FilmPreview, FilmVersion, ProductionWorkbenchState, ResolveExport, ResolveTimelineStatus } from './api'
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
  const [resolved, setResolved] = useState<ResolveExport | null>(null)
  const [timeline, setTimeline] = useState<ResolveTimelineStatus | null>(null)
  const [titlesOpen, setTitlesOpen] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [historyId, setHistoryId] = useState('')
  const [copied, setCopied] = useState(false)
  const [versionPreview, setVersionPreview] = useState<FilmVersion | null>(null)
  const [previewError, setPreviewError] = useState('')
  const versionPlayer = useRef<HTMLVideoElement>(null)
  useEffect(() => { if (!visible) versionPlayer.current?.pause() }, [visible])
  useEffect(() => {
    let disposed = false
    void getProductionWorkbench(projectId, runId).then((next) => { if (!disposed) { setData(next); setExportUrl(''); setResolved(null) } }).catch((e) => { if (!disposed) setError(String(e)) })
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
  function exportResolve() {
    setExporting(true)
    void action(async () => { try { setResolved(await exportToResolve(csrf, projectId, runId)); setTimeline(null) } finally { setExporting(false) } },
      '已匯出到毛片資料夾，Finder 已打開匯出的資料夾。')
  }
  function convertTitles() {
    if (!resolved) return
    void action(async () => { await convertResolveTitles(csrf, projectId, runId, resolved.folder); setTimeline(await getResolveTimelineStatus(csrf, projectId, runId, resolved.folder)) },
      '已做好可改字的時間線，Finder 已打開它所在的資料夾。')
  }
  // While the steps are open, keep checking for the .drt saved in Resolve so the person sees it land.
  useEffect(() => {
    if (!resolved || !titlesOpen || !visible) return
    let disposed = false
    const check = () => void getResolveTimelineStatus(csrf, projectId, runId, resolved.folder).then((next) => { if (!disposed) setTimeline(next) }).catch(() => { /* Keep the last status. */ })
    check()
    const timer = window.setInterval(check, 3000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [resolved, titlesOpen, visible, csrf, projectId, runId])
  async function copy() {
    try { await navigator.clipboard.writeText(prompt); setCopied(true); window.setTimeout(() => setCopied(false), 2000) }
    catch { setError('無法複製到剪貼簿，請直接選取文字複製。') }
  }
  return <section className="production-workbench" aria-labelledby={headingId}>
    <h3 id={headingId} className="sr-only">審片並交給 AI 修改</h3>
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
      <button type="button" className="button secondary" disabled={!historyId || busy || active} onClick={() => void action(() => restoreProduction(csrf, projectId, runId, data!.revision.revision_id, historyId), '已還原。輸出這一版影片後就能審片。')}>還原選定版本</button>
    </details>
    <details className="workbench-export"><summary>到 DaVinci Resolve 自己剪</summary>
      <p>在 Clipen 裡，修改都是記注記交給 AI。要自己動手逐格剪，就匯出到 DaVinci Resolve：時間線直接使用原始素材，元件做成透明圖片疊在上層，字幕是可編輯的字幕軌，配樂也一起帶過去。匯出會放在毛片資料夾裡的「Clipen 匯出」，不會被當成新素材。</p>
      <button type="button" className="button secondary" disabled={busy || active || !data} onClick={exportResolve}>{exporting ? '匯出中…' : '匯出到 DaVinci Resolve'}</button>
      {exporting && <p className="prompt-draft-hint">有馬賽克或變速的片段需要先輸出，可能要一點時間。</p>}
      {resolved && <div className="resolve-result" role="status">
        <p>已匯出到「{resolved.folder}」。</p>
        <ol>
          <li>在 Resolve 選「檔案 &gt; 匯入 &gt; 時間線…」，選擇資料夾裡的 .fcpxml。</li>
          <li>如果 Resolve 說找不到素材（clips were not yet found），按「Locate」，選擇毛片資料夾。</li>
          {resolved.captions > 0 && <li>再選「檔案 &gt; 匯入 &gt; 字幕…」，選擇「字幕.srt」，拖到時間線的字幕軌。</li>}
        </ol>
        {resolved.editable_overlays > 0 && <details className="resolve-titles" onToggle={(event) => setTitlesOpen(event.currentTarget.open)}>
          <summary>想在 Resolve 改元件的字（需要時再做）</summary>
          <p>匯入後元件已經和 Clipen 一模一樣，只是以圖片呈現。要改字時，再做兩步，{resolved.editable_overlays} 個元件會一起換成可以改字的標題：</p>
          <ol>
            <li>在 Resolve 選「檔案 &gt; 匯出 &gt; 時間線…」，格式選「DaVinci Resolve Timeline Files (*.drt)」，按儲存。Clipen 會自動做好可改字的版本，並在 Finder 打開它。</li>
            <li>在 Resolve 選「檔案 &gt; 匯入 &gt; 時間線…」，選名稱有「（可改字）」的 .drt。選取元件後，在右邊檢查器改字。</li>
          </ol>
          <ResolveTimeline status={timeline} disabled={busy || active} onConvert={convertTitles} />
        </details>}
        {resolved.notes.length > 0 && <details><summary>匯出時要留意的地方（{resolved.notes.length}）</summary><ul>{resolved.notes.map((note) => <li key={note}>{note}</li>)}</ul></details>}
        <p className="prompt-draft-hint">完整步驟在資料夾裡的「說明.txt」。請保持資料夾完整，時間線會用到裡面的檔案。</p>
      </div>}
      <button type="button" className="button ghost" disabled={busy || active || !data} onClick={() => void action(async () => { const result = await exportProduction(csrf, projectId, runId); setExportUrl(result.download_url) }, 'Clipen 剪輯資料已準備好。')}>下載 Clipen 剪輯資料（JSON）</button>
      {exportUrl && <a href={exportUrl} download>下載剪輯資料</a>}
    </details>
    </div>
  </section>
}

const fileName = (path: string) => path.split('/').pop() ?? path
/** Whether Clipen has found the .drt saved in Resolve, and where it looks while it has not. */
function ResolveTimeline({ status, disabled, onConvert }: { status: ResolveTimelineStatus | null; disabled: boolean; onConvert: () => void }) {
  if (!status) return <p className="prompt-draft-hint" role="status">正在找 Resolve 存出的 .drt…</p>
  if (status.state === 'converted' && status.converted) return <p className="resolve-timeline is-ready" role="status">已做好「{fileName(status.converted)}」，放在這份匯出的資料夾裡。在 Resolve 匯入它就能改字。</p>
  if (status.state === 'found' && status.source) return <div className="resolve-timeline" role="status">
    <p>找到「{fileName(status.source)}」，還沒做成可改字的版本。Clipen 開著時通常幾秒內會自動完成；沒有的話按這裡：</p>
    <button type="button" className="button ghost" disabled={disabled} onClick={onConvert}>把元件換成可改字的標題</button>
  </div>
  return <div className="resolve-timeline" role="status">
    <p>還沒找到這份時間線的 .drt。存到下面任一個地方，Clipen 就會找到：</p>
    <ul>{status.folders.map((folder) => <li key={folder.path}>{folder.label}<small>{folder.path}</small></li>)}</ul>
  </div>
}
