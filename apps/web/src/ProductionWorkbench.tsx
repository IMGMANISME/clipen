import { PowerNotice } from './PowerNotice'
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { convertResolveTitles, exportProduction, getResolveTimelineStatus, exportToResolve, getProductionWorkbench, refineProduction, renderProduction, restoreProduction, setComponentText } from './api'
import type { FilmPreview, FilmVersion, ProductionWorkbenchState, ResolveExport, ResolveTimelineStatus } from './api'
import { ProductionMusicPicker } from './ProductionMusicPicker'
import { usePromptDraft } from './usePromptDraft'
import { ReviewNotes } from './ReviewNotes'
import { clock, composePrompt } from './reviewModel'
import { providerName, useProvider } from './providerContext'

type Props = { visible: boolean; runProvider?: string; projectId: string; csrf: string; runId: string; revisionId: string; active: boolean; onChanged: () => void; musicId: string; previews: Array<FilmPreview | null | undefined>; onMusicChange: (id: string) => void }
const MAX_INSTRUCTION = 8000
/** Review the film: watch, note, reword graphics, and hand the notes to the AI. Everything else waits in menus. */
export function ProductionWorkbench({ visible, projectId, csrf, runId, revisionId, active, onChanged, musicId, onMusicChange, previews, runProvider }: Props) {
  const provider = useProvider()
  // A revision may hand the draft to the AI Clipen was started with.
  const switching = Boolean(runProvider && provider.id && runProvider !== provider.id)
  const [data, setData] = useState<ProductionWorkbenchState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [instruction, setInstruction] = usePromptDraft(projectId, 'refinement', '', runId)
  const [consent, setConsent] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [noteOpen, setNoteOpen] = useState(false)
  const [resolved, setResolved] = useState<ResolveExport | null>(null)
  const [timeline, setTimeline] = useState<ResolveTimelineStatus | null>(null)
  const [exporting, setExporting] = useState(false)
  const [compare, setCompare] = useState<FilmVersion | null>(null)
  useEffect(() => {
    let disposed = false
    void getProductionWorkbench(projectId, runId).then((next) => { if (!disposed) { setData(next); setResolved(null); setCompare(null) } }).catch((e) => { if (!disposed) setError(String(e)) })
    return () => { disposed = true }
  }, [projectId, runId, revisionId])
  useEffect(() => { if (!message) return; const timer = window.setTimeout(() => setMessage(''), 4000); return () => window.clearTimeout(timer) }, [message])
  // Note times are positions in the current version, so review only a film rendered from it.
  const revision = data?.revision.revision_id
  const video = previews.find((preview) => preview?.state === 'succeeded' && preview.revision_id === revision) ?? null
  const rendering = previews.some((preview) => preview && preview.revision_id === revision && ['queued', 'running'].includes(preview.state))
  const notes = data?.references?.items ?? []
  const prompt = data ? composePrompt(instruction, notes, data.revision.timeline) : ''
  const current = data?.history.find((version) => version.revision_id === revision)
  const seconds = data ? data.revision.timeline.duration_frames * data.revision.timeline.frame_rate.den / data.revision.timeline.frame_rate.num : 0
  async function action(work: () => Promise<unknown>, success: string): Promise<boolean> {
    if (busy || active || !data) return false
    setBusy(true); setError(''); setMessage('')
    let ok = false
    try { await work(); setMessage(success); ok = true }
    catch (e) { setError(e instanceof Error ? e.message : '這次操作沒有完成。') }
    finally {
      try { setData(await getProductionWorkbench(projectId, runId)) } catch { /* Keep the last visible state. */ }
      onChanged(); setBusy(false)
    }
    return ok
  }
  function send() {
    if (!data) return
    // The notes travel as structured data too, so a very long list can fall back to the overall request.
    const text = prompt.length <= MAX_INSTRUCTION ? prompt : `${instruction.trim() || '請依審片注記修改目前影片，其他部分保留。'}\n\n（共 ${notes.length} 筆審片注記，詳見附帶的注記資料。）`.slice(0, MAX_INSTRUCTION)
    setConfirming(false)
    void action(async () => { await refineProduction(csrf, projectId, runId, data.revision.revision_id, text, musicId || undefined, data.references?.version ?? 0); setInstruction(''); setConsent(false) }, 'AI 開始修改了。')
  }
  function render() {
    if (data) void action(() => renderProduction(csrf, projectId, runId, data.revision.revision_id), '開始輸出這一版，好了就能播放。')
  }
  function exportResolve() {
    setExporting(true)
    void action(async () => { try { setResolved(await exportToResolve(csrf, projectId, runId)); setTimeline(null) } finally { setExporting(false) } }, '已匯出，Finder 打開了匯出的資料夾。')
  }
  function convertTitles() {
    if (!resolved) return
    void action(async () => { await convertResolveTitles(csrf, projectId, runId, resolved.folder); setTimeline(await getResolveTimelineStatus(csrf, projectId, runId, resolved.folder)) }, '可改字的時間線做好了。')
  }
  function downloadData() {
    void action(async () => {
      const result = await exportProduction(csrf, projectId, runId)
      const link = document.createElement('a'); link.href = result.download_url; link.download = ''; link.click()
    }, '剪輯資料開始下載。')
  }
  // While the Resolve steps are open, keep checking for the .drt saved in Resolve so the person sees it land.
  useEffect(() => {
    if (!resolved || !resolved.editable_overlays || !visible) return
    let disposed = false
    const check = () => void getResolveTimelineStatus(csrf, projectId, runId, resolved.folder).then((next) => { if (!disposed) setTimeline(next) }).catch(() => { /* Keep the last status. */ })
    check()
    const timer = window.setInterval(check, 3000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [resolved, visible, csrf, projectId, runId])

  const comparing = compare?.preview_url ? <div className="rv-compare">
    <video key={compare.preview_url} controls autoPlay preload="metadata" src={compare.preview_url} aria-label={`版本 ${compare.number} 預覽`} />
    <div className="rv-compare-bar"><span>正在看<strong>版本 {compare.number}</strong>，目前的版本不受影響</span>
      <button type="button" className="button secondary" disabled={busy || active} onClick={() => { if (data) void action(() => restoreProduction(csrf, projectId, runId, data.revision.revision_id, compare.revision_id), `已還原成版本 ${compare.number}。`).then((ok) => { if (ok) setCompare(null) }) }}>還原這一版</button>
      <button type="button" className="button ghost" onClick={() => setCompare(null)}>回到目前</button></div>
  </div> : undefined

  return <section className="rv" aria-label="審片修改">
    <header className="rv-bar">
      <div className="rv-title"><h3>審片</h3>{data && <span>{current ? `版本 ${current.number}` : '目前版本'} · {clock(seconds)}</span>}</div>
      <div className="rv-tools">
        {data && !video && <button type="button" className="button primary" disabled={busy || active || rendering} onClick={render}>{rendering ? '輸出中…' : '輸出這一版'}</button>}
        <Menu label="版本" disabled={!data}>{(close) => <ol className="rv-versions">{data?.history.map((version) => {
          const isCurrent = version.revision_id === revision
          return <li key={version.revision_id} className={isCurrent ? 'is-current' : ''}>
            <div><strong>版本 {version.number}</strong>{isCurrent && <em>目前</em>}
              <span>{version.label} · {new Date(version.created_at).toLocaleString('zh-TW', { hour12: false, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}{version.duration_seconds != null ? ` · ${clock(version.duration_seconds)}` : ''}</span></div>
            {!isCurrent && <div className="rv-row">
              {version.preview_url && <button type="button" className="rv-text-button" onClick={() => { setCompare(version); close() }}>看看</button>}
              <button type="button" className="rv-text-button" disabled={busy || active} onClick={() => { close(); if (data) void action(() => restoreProduction(csrf, projectId, runId, data.revision.revision_id, version.revision_id), `已還原成版本 ${version.number}。`) }}>還原</button>
            </div>}
          </li>
        })}</ol>}</Menu>
        <Menu label={exporting ? '匯出中…' : '匯出'} disabled={!data || exporting}>{(close) => <div className="rv-menu-list">
          <button type="button" disabled={busy || active} onClick={() => { close(); exportResolve() }}><strong>DaVinci Resolve</strong><span>自己動手細剪，素材、元件和配樂一起帶過去</span></button>
          {video && <a href={video.media_url} download onClick={close}><strong>下載這一版影片</strong><span>MP4</span></a>}
          <button type="button" disabled={busy || active} onClick={() => { close(); downloadData() }}><strong>剪輯資料</strong><span>JSON，給開發或備份用</span></button>
        </div>}</Menu>
      </div>
    </header>
    {error && <p className="rv-alert" role="alert">{error}<button type="button" className="rv-icon" aria-label="關閉" onClick={() => setError('')}>×</button></p>}
    {message && <p className="rv-toast" role="status">{message}</p>}
    {data && <ReviewNotes visible={visible} projectId={projectId} csrf={csrf} runId={runId} data={data} video={video} disabled={busy || active} stage={comparing}
      onChanged={(references) => setData((value) => value ? { ...value, references } : value)} onDirty={setNoteOpen}
      onSaveComponent={(elementId, text, detail) => action(() => setComponentText(csrf, projectId, runId, data.revision.revision_id, elementId, text, detail), '改好了。輸出這一版就能在影片裡看到。')} />}
    {data && <footer className="rv-composer">
      {active ? <p className="rv-working"><span className="rv-pulse" />AI 正在修改這支片，好了會自動更新。</p> : <>
        <textarea aria-label="這一版還要怎麼改？" rows={1} value={instruction} maxLength={MAX_INSTRUCTION} onChange={(e) => setInstruction(e.target.value)}
          placeholder={notes.length ? `還有其他想法嗎？（可不填，${notes.length} 筆注記會一起交給 AI）` : '這一版還要怎麼改？例如：節奏再快一點，早餐段落縮短'} />
        <Menu label="配樂" disabled={busy} up>{() => <div className="rv-music">
          <ProductionMusicPicker projectId={projectId} csrf={csrf} value={musicId} onChange={onMusicChange} disabled={busy} defaultLabel="沿用目前配樂" /></div>}</Menu>
        <button type="button" className="button primary" disabled={busy || noteOpen || !prompt} title={noteOpen ? '先記下或取消正在寫的注記' : undefined} onClick={() => setConfirming(true)}>交給 AI 修改</button>
      </>}
    </footer>}
    <Dialog open={confirming} label="交給 AI 修改" onClose={() => setConfirming(false)}>
      <h3>交給 {provider.name} 修改</h3>
      <p>{notes.length ? `${notes.length} 筆注記${instruction.trim() ? '和你的說明' : ''}` : '你的說明'}會交給 {provider.name}，從{current ? `版本 ${current.number} ` : '目前版本'}繼續改。現在這版會保留，隨時可以還原。</p>
      {switching && <p className="rv-note">這支片原本由 {providerName(runProvider)} 製作，這次改由 {provider.name} 接手。</p>}
      <PowerNotice />
      <label className="consent-check"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />同意使用 {provider.name} 的額度</label>
      <div className="rv-row rv-dialog-actions"><button type="button" className="button ghost" onClick={() => setConfirming(false)}>取消</button>
        <button type="button" className="button primary" disabled={!consent || busy} onClick={send}>開始修改</button></div>
    </Dialog>
    <Dialog open={Boolean(resolved)} label="到 DaVinci Resolve 繼續剪" onClose={() => setResolved(null)}>
      {resolved && <>
        <h3>到 DaVinci Resolve 繼續剪</h3>
        <p className="rv-path">{resolved.folder}</p>
        <ol className="rv-steps">
          <li>在 Resolve 選「檔案 › 匯入 › 時間線…」，選資料夾裡的 .fcpxml。</li>
          <li>如果說找不到素材，按「Locate」，選毛片資料夾。</li>
          {resolved.captions > 0 && <li>再選「檔案 › 匯入 › 字幕…」，選「字幕.srt」，拖到字幕軌。</li>}
        </ol>
        {resolved.editable_overlays > 0 && <details className="rv-resolve-titles"><summary>也想在 Resolve 改元件的字</summary>
          <ol className="rv-steps">
            <li>在 Resolve 選「檔案 › 匯出 › 時間線…」，存成 .drt。</li>
            <li>Clipen 會做好一份「（可改字）」的 .drt，匯入它就能改字。</li>
          </ol>
          <ResolveTimeline status={timeline} disabled={busy || active} onConvert={convertTitles} />
        </details>}
        {resolved.notes.length > 0 && <details><summary>要留意的地方（{resolved.notes.length}）</summary><ul>{resolved.notes.map((note) => <li key={note}>{note}</li>)}</ul></details>}
        <div className="rv-row rv-dialog-actions"><button type="button" className="button primary" onClick={() => setResolved(null)}>好</button></div>
      </>}
    </Dialog>
  </section>
}

/** A button that opens a small panel, closed by clicking outside or pressing Escape. */
function Menu({ label, disabled, up, children }: { label: string; disabled?: boolean; up?: boolean; children: (close: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const away = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away); document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('mousedown', away); document.removeEventListener('keydown', escape) }
  }, [open])
  return <div className="rv-menu" ref={root}>
    <button type="button" className="button secondary" aria-expanded={open} disabled={disabled} onClick={() => setOpen(!open)}>{label}<span className="rv-caret" aria-hidden="true" /></button>
    {open && <div className={`rv-menu-panel${up ? ' is-up' : ''}`}>{children(() => setOpen(false))}</div>}
  </div>
}

/** A modal dialog that keeps focus inside and closes on Escape or a click outside it. */
function Dialog({ open, label, onClose, children }: { open: boolean; label: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])
  return <dialog ref={ref} className="rv-dialog" aria-label={label} onClose={onClose} onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <div className="rv-dialog-body">{open && children}</div>
  </dialog>
}

const fileName = (path: string) => path.split('/').pop() ?? path
/** Whether Clipen has found the .drt saved in Resolve, and where it looks while it has not. */
function ResolveTimeline({ status, disabled, onConvert }: { status: ResolveTimelineStatus | null; disabled: boolean; onConvert: () => void }) {
  if (!status) return <p className="rv-hint" role="status">正在找 Resolve 存出的 .drt…</p>
  if (status.state === 'converted' && status.converted) return <p className="resolve-timeline is-ready" role="status">做好了：「{fileName(status.converted)}」，在匯出的資料夾裡。</p>
  if (status.state === 'found' && status.source) return <div className="resolve-timeline" role="status">
    <p>找到「{fileName(status.source)}」，正在轉成可改字的版本。沒自動完成的話按這裡：</p>
    <button type="button" className="button ghost" disabled={disabled} onClick={onConvert}>轉成可改字的版本</button>
  </div>
  return <div className="resolve-timeline" role="status">
    <p>還沒找到 .drt。存到下面任一個地方就行：</p>
    <ul>{status.folders.map((folder) => <li key={folder.path}>{folder.label}<small>{folder.path}</small></li>)}</ul>
  </div>
}
