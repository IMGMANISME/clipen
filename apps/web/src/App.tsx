import { useEffect, useMemo, useState } from 'react'
import { EvidenceSection } from './EvidenceSection'
import { TranscriptResults } from './TranscriptResults'
import type { FormEvent } from 'react'
import {
  ApiError,
  connectLocalSession,
  createProject,
  cancelInventory,
  cancelAnalysis,
  getAnalysis,
  getInventory,
  getInventoryItems,
  getProject,
  getStatus,
  listProjects,
  removeDirectory,
  removeASR,
  resumeAnalysis,
  saveDirectory,
  selectDirectory,
  startInventory,
  startAnalysis,
  updateProjectDetails,
} from './api'
import type {
  DirectoryAvailability,
  DirectoryReference,
  DirectoryRole,
  InventoryItem,
  InventoryItemsPage,
  InventoryRun,
  InventorySummary,
  AnalysisRun,
  AnalysisSummary,
  Project,
  ProviderState,
  StatusPayload,
} from './api'
import './index.css'

type ApplicationState =
  | { kind: 'connecting' }
  | { kind: 'failed'; message: string; serverUnavailable: boolean }
  | { kind: 'ready'; status: StatusPayload; projects: Project[] }

const stages = ['素材', '分析', '計畫', '剪輯', '匯出']

const providerHelp: Record<Exclude<ProviderState, 'ready'>, string> = {
  login_required: '完成 Codex 登入後再重啟 Clipen。你仍可先建立作品與整理素材位置。',
  unsupported: '更新 Codex CLI 後再重啟 Clipen。本機作品資料不受影響。',
  confinement_failed: '安全隔離未通過，因此 Clipen 不會把任何作品資料交給 AI。',
  unavailable: '目前找不到 Codex CLI。你仍可先建立作品與整理素材位置。',
}

const availabilityCopy: Record<DirectoryAvailability, { label: string; help: string }> = {
  available: { label: '可使用', help: 'Clipen 只保存這個位置，不會修改裡面的檔案。' },
  offline: { label: '磁碟未連接', help: '重新連接外接磁碟後，Clipen 會再次確認這個位置。' },
  changed: { label: '位置已改變', help: '這個路徑現在指向不同資料夾，請重新選擇。' },
  permission_required: { label: '需要權限', help: '請重新選擇資料夾，讓 macOS 確認存取。' },
}

const inventoryErrorCopy: Record<string, string> = {
  controller_restarted: 'Clipen 上次關閉時檢查尚未完成，請重新檢查。',
  controller_stopped: 'Clipen 關閉時已安全停止檢查，請重新檢查。',
  probe_unavailable: '找不到 FFprobe。請先安裝 FFmpeg，再重新檢查。',
  probe_unsupported: '目前的 FFprobe 版本不支援這次檢查，請更新 FFmpeg。',
  inventory_input_changed: '檢查期間素材位置有變動，請重新檢查。',
  root_unavailable: '素材磁碟目前無法使用，請確認磁碟已連接。',
  root_changed: '素材位置已改變，請重新選擇資料夾。',
  discovery_timeout: '資料夾內容很多，這次檢查已安全停止。請整理後再試一次。',
  too_many_files: '資料夾中的檔案超過安全檢查上限，請縮小素材範圍。',
  tree_too_deep: '資料夾層級太深，請整理後再試一次。',
}

const itemIssueCopy: Record<string, string> = {
  entry_unreadable: '無法讀取這個項目',
  probe_unsupported: '不支援的媒體格式',
  probe_unreadable: '無法讀取媒體資訊',
  probe_timeout: '讀取媒體資訊逾時',
  probe_changed: '檢查時檔案有變動',
  probe_entry_unreadable: '無法讀取檔案',
  probe_entry_not_regular: '檔案位置已改變',
}

const analysisErrorCopy: Record<string, string> = {
  controller_restarted: 'Clipen 上次關閉時仍在分析。已保存的音訊與逐字稿切塊仍在，可以接續。',
  controller_stopped: 'Clipen 關閉時已安全停止分析，可以從 checkpoint 接續。',
  ffmpeg_unavailable: '找不到 FFmpeg，無法準備語音辨識需要的音訊。',
  asr_runtime_unavailable: '本機語音辨識執行環境目前無法使用。',
  asr_model_changed: '語音辨識模型內容已改變，為避免混用結果，這次分析已停止。',
  scope_changed: '素材檢查結果已改變，請重新確認分析範圍。',
  root_changed: '外接磁碟上的素材位置已改變，這次沒有執行語音辨識。請重新檢查素材後再試。',
  root_unavailable: '目前無法讀取素材磁碟，這次沒有執行語音辨識。',
  preparation_all_items_failed: '所有可分析素材都在語音辨識前失敗，沒有產生可用逐字稿。',
}

const analysisBlockedCopy: Record<string, string> = {
  inventory_required: '請先完成素材檢查。',
  preparation_empty: '目前沒有可進行口語分析的影音素材。',
  root_unavailable: '目前無法讀取素材磁碟，請確認外接硬碟已連接。',
  root_changed: '素材資料夾已改變，請先重新選擇並檢查素材。',
}

const asrSetupErrorCopy: Record<string, string> = {
  asr_runtime_create_failed: '無法建立隔離的執行環境，請確認磁碟空間後再試一次。',
  asr_runtime_install_failed: '語音辨識元件下載或安裝失敗，請確認網路後再試一次。',
  asr_disk_space_insufficient: '磁碟空間不足，請至少保留約 3.5 GB 可用空間後再安裝。',
  asr_disk_space_unavailable: '目前無法確認安裝磁碟的可用空間。',
  asr_runtime_install_timeout: '安裝時間超過安全上限，已停止這次設定。',
  asr_download_failed: '模型下載失敗，請確認網路後再試一次。',
  asr_download_verification_failed: '下載的模型未通過完整性驗證，Clipen 沒有啟用它。',
  asr_model_invalid: '模型檔案不完整，Clipen 沒有啟用它。',
  asr_receipt_invalid: '安裝紀錄不完整，請清除後重新安裝。',
  asr_setup_cleanup_failed: '未完成的暫存內容無法清除，請重新啟動 Clipen 再試一次。',
}

function formatBytes(value: number | null): string {
  if (value === null) return '大小未知'
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && size >= 1024; index += 1) {
    size /= 1024
    unit = units[index]
  }
  return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${unit}`
}

function projectFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('project')
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Clipen 無法完成這次操作。'
}

function formatUpdatedAt(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '最近更新'
  return new Intl.DateTimeFormat('zh-TW', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function ProviderNotice({ status }: { status: StatusPayload }) {
  if (status.provider.state === 'ready') return null
  return (
    <aside className="provider-notice" role="status">
      <span className="notice-symbol" aria-hidden="true">!</span>
      <div>
        <strong>{status.provider.message}</strong>
        <p>{providerHelp[status.provider.state]}</p>
      </div>
    </aside>
  )
}

function CreateProjectPanel({
  csrf,
  onCreated,
  onCancel,
}: {
  csrf: string
  onCreated: (project: Project) => void
  onCancel: () => void
}) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      onCreated(await createProject(csrf, name))
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="create-panel" aria-labelledby="create-title">
      <div>
        <p className="eyebrow">建立作品</p>
        <h2 id="create-title">先替這次剪輯取個名字</h2>
        <p>建立後再選擇毛片、參考作品並填寫剪輯需求。</p>
      </div>
      <form onSubmit={submit}>
        <label htmlFor="project-name">作品名稱</label>
        <input
          id="project-name"
          autoFocus
          maxLength={120}
          placeholder="例如：手沖咖啡教學"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="form-actions">
          <button type="button" className="button secondary" onClick={onCancel} disabled={busy}>取消</button>
          <button type="submit" className="button primary" disabled={!name.trim() || busy}>
            {busy ? '正在建立…' : '建立作品'}
          </button>
        </div>
      </form>
    </section>
  )
}

function ProjectHome({
  projects,
  csrf,
  onProjects,
  onOpen,
}: {
  projects: Project[]
  csrf: string
  onProjects: (projects: Project[]) => void
  onOpen: (project: Project) => void
}) {
  const [creating, setCreating] = useState(false)

  function created(project: Project) {
    onProjects([project, ...projects.filter((item) => item.project_id !== project.project_id)])
    onOpen(project)
  }

  return (
    <main className="home-main">
      <section className="home-heading">
        <div>
          <p className="eyebrow">本機剪輯工作台</p>
          <h1>你的作品</h1>
          <p>從毛片、參考作品與需求開始。Clipen 會先保存你選擇的位置，不會修改素材。</p>
        </div>
        {!creating && (
          <button className="button primary create-button" onClick={() => setCreating(true)}>
            <span aria-hidden="true">＋</span> 建立作品
          </button>
        )}
      </section>

      {creating && (
        <CreateProjectPanel
          csrf={csrf}
          onCreated={created}
          onCancel={() => setCreating(false)}
        />
      )}

      {!creating && projects.length === 0 && (
        <section className="empty-projects" aria-labelledby="empty-title">
          <div className="empty-mark" aria-hidden="true"><span /></div>
          <h2 id="empty-title">還沒有作品</h2>
          <p>建立第一個作品後，Clipen 會帶你完成素材、分析、計畫、剪輯與匯出。</p>
          <button className="button primary" onClick={() => setCreating(true)}>建立第一個作品</button>
        </section>
      )}

      {!creating && projects.length > 0 && (
        <section className="project-section" aria-labelledby="project-list-title">
          <div className="section-title-row">
            <h2 id="project-list-title">最近作品</h2>
            <span>{projects.length} 個作品</span>
          </div>
          <div className="project-grid">
            {projects.map((project) => {
              const source = project.directories.find((directory) => directory.role === 'source')
              return (
                <button className="project-card" key={project.project_id} onClick={() => onOpen(project)}>
                  <span className="project-stage">素材</span>
                  <strong>{project.name}</strong>
                  <span className="project-next">
                    {source ? '繼續整理作品需求' : '下一步：選擇毛片資料夾'}
                  </span>
                  <span className="project-time">{formatUpdatedAt(project.updated_at)}</span>
                </button>
              )
            })}
          </div>
        </section>
      )}
    </main>
  )
}

function DirectoryCard({
  role,
  reference,
  busy,
  onChoose,
  onRemove,
}: {
  role: DirectoryRole
  reference?: DirectoryReference
  busy: boolean
  onChoose: () => void
  onRemove: () => void
}) {
  const isSource = role === 'source'
  const copy = reference ? availabilityCopy[reference.availability] : null
  return (
    <article className={`directory-card ${reference ? 'has-directory' : ''}`}>
      <div className="directory-icon" aria-hidden="true"><span /></div>
      <div className="directory-content">
        <div className="directory-title-row">
          <div>
            <h3>{isSource ? '毛片資料夾' : '參考作品資料夾'}</h3>
            <p>{isSource ? '剪輯會使用的原始影片與聲音' : '用來理解風格，可稍後再加入'}</p>
          </div>
          {reference && <span className={`availability ${reference.availability}`}>{copy?.label}</span>}
        </div>
        {reference ? (
          <>
            <div className="selected-path">
              <strong>{reference.display_name}</strong>
              <span title={reference.display_path}>{reference.display_path}</span>
            </div>
            <p className="directory-help">{copy?.help}</p>
            <div className="directory-actions">
              <button className="text-button" onClick={onChoose} disabled={busy}>
                {busy ? '等待選擇…' : '重新選擇'}
              </button>
              <button className="text-button danger" onClick={onRemove} disabled={busy}>移除引用</button>
            </div>
          </>
        ) : (
          <button className="button secondary choose-button" onClick={onChoose} disabled={busy}>
            {busy ? '請在 macOS 視窗選擇…' : isSource ? '選擇毛片資料夾' : '選擇參考資料夾'}
          </button>
        )}
      </div>
    </article>
  )
}

function InventorySection({
  summary,
  page,
  loading,
  sourceReady,
  actionBusy,
  onStart,
  onCancel,
}: {
  summary: InventorySummary | null
  page: InventoryItemsPage | null
  loading: boolean
  sourceReady: boolean
  actionBusy: boolean
  onStart: () => void
  onCancel: (run: InventoryRun) => void
}) {
  const latest = summary?.latest ?? null
  const current = summary?.current ?? null
  const active = latest !== null && ['queued', 'running', 'cancelling'].includes(latest.state)
  const failed = latest !== null && ['failed', 'cancelled', 'interrupted'].includes(latest.state)
  const resultItems: InventoryItem[] = page?.items.filter((item) => item.status !== 'ignored') ?? []
  const canStart = sourceReady && !actionBusy && !active

  return (
    <section className="inventory-section" aria-labelledby="inventory-title">
      <div className="inventory-heading">
        <div>
          <p className="eyebrow">下一步</p>
          <h2 id="inventory-title">檢查素材是否可用</h2>
          <p>Clipen 會讀取媒體格式、長度與影音軌資訊。這次不會呼叫 AI，也不會修改或複製任何素材。</p>
        </div>
        {!active && (
          <button className="button primary" onClick={onStart} disabled={!canStart}>
            {actionBusy ? '正在準備…' : latest ? '重新檢查素材' : '檢查素材'}
          </button>
        )}
      </div>

      {!sourceReady && (
        <div className="inventory-empty muted-state">
          <strong>{current ? '重新確認毛片資料夾' : '先選擇可使用的毛片資料夾'}</strong>
          <p>{current
            ? '先前的檢查結果已暫時隱藏，重新選擇並檢查後才會用於後續分析。'
            : '完成上方的毛片位置設定後，就能開始檢查。'}</p>
        </div>
      )}

      {sourceReady && loading && !summary && (
        <div className="inventory-empty" role="status">
          <span className="small-spinner" aria-hidden="true" />
          <div><strong>正在讀取上次檢查狀態</strong><p>素材尚未被重新讀取。</p></div>
        </div>
      )}

      {sourceReady && !loading && !latest && (
        <div className="inventory-empty">
          <span className="inventory-mark" aria-hidden="true" />
          <div><strong>尚未檢查素材</strong><p>按下「檢查素材」後，這裡會即時顯示進度與需要處理的檔案。</p></div>
        </div>
      )}

      {latest && active && (
        <div className="inventory-progress" role="status" aria-live="polite">
          <div className="progress-main">
            <span className="small-spinner" aria-hidden="true" />
            <div>
              <strong>{latest.state === 'cancelling' ? '正在安全停止…' : '正在檢查素材…'}</strong>
              <p>已找到 {latest.counts.discovered.toLocaleString()} 個檔案，已讀取 {latest.counts.probed.toLocaleString()} 個媒體資訊。</p>
            </div>
          </div>
          <button
            className="button secondary"
            onClick={() => onCancel(latest)}
            disabled={actionBusy || latest.state === 'cancelling'}
          >
            {latest.state === 'cancelling' ? '停止中…' : '停止檢查'}
          </button>
        </div>
      )}

      {latest && failed && (
        <div className={`inventory-outcome ${latest.state === 'failed' ? 'has-error' : ''}`} role="status">
          <span className="outcome-symbol" aria-hidden="true">{latest.state === 'failed' ? '!' : '×'}</span>
          <div>
            <strong>{latest.state === 'failed' ? '這次檢查沒有完成' : latest.state === 'cancelled' ? '檢查已取消' : '檢查已安全中斷'}</strong>
            <p>{latest.error_code ? inventoryErrorCopy[latest.error_code] ?? 'Clipen 無法完成這次素材檢查，請重新試一次。' : '素材沒有被修改，你可以隨時重新檢查。'}</p>
            {current && current.run_id !== latest.run_id && <small>下方仍保留上次成功的檢查結果。</small>}
          </div>
        </div>
      )}

      {current && sourceReady && (
        <div className="inventory-results">
          <div className="result-title-row">
            <div>
              <strong>{current.counts.issues ? '素材已檢查，部分檔案需要注意' : '素材已準備好'}</strong>
              <p>{current.counts.discovered.toLocaleString()} 個檔案完成整理</p>
            </div>
            <span className="result-ready">檢查完成</span>
          </div>
          <div className="inventory-counts" aria-label="素材檢查摘要">
            <div><strong>{current.counts.usable.toLocaleString()}</strong><span>可使用媒體</span></div>
            <div className={current.counts.issues ? 'attention' : ''}><strong>{current.counts.issues.toLocaleString()}</strong><span>需要注意</span></div>
            <div><strong>{current.counts.ignored.toLocaleString()}</strong><span>略過的其他檔案</span></div>
          </div>

          {page && resultItems.length > 0 && (
            <div className="asset-list">
              <div className="asset-list-heading"><strong>媒體清單</strong><span>需要注意的項目會排在最前面</span></div>
              {resultItems.map((item) => {
                const duration = item.probe?.duration_seconds
                const codecs = [...new Set(item.streams.map((stream) => stream.codec))].join(' · ')
                return (
                  <div className={`asset-row ${item.status}`} key={item.asset_id}>
                    <span className={`asset-kind ${item.kind}`} aria-hidden="true">{item.kind === 'video' ? 'V' : item.kind === 'audio' ? 'A' : 'I'}</span>
                    <div className="asset-name"><strong>{item.display_name}</strong><span title={item.relative_path}>{item.relative_path}</span></div>
                    <div className="asset-detail">
                      {item.status === 'needs_attention'
                        ? <strong>{item.issue_code ? itemIssueCopy[item.issue_code] ?? '需要確認' : '需要確認'}</strong>
                        : <><span>{typeof duration === 'string' ? `${duration} 秒` : formatBytes(item.size_bytes)}</span>{codecs && <span>{codecs}</span>}</>}
                    </div>
                  </div>
                )
              })}
              {page.total > page.items.length && <p className="asset-limit">目前顯示前 {page.items.length.toLocaleString()} 個項目，共 {page.total.toLocaleString()} 個。</p>}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

function AnalysisSection({
  projectId,
  summary,
  loading,
  actionBusy,
  onStart,
  onCancel,
  onResume,
  onRemoveRuntime,
}: {
  projectId: string
  summary: AnalysisSummary | null
  loading: boolean
  actionBusy: boolean
  onStart: () => void
  onCancel: (run: AnalysisRun) => void
  onResume: (run: AnalysisRun) => void
  onRemoveRuntime: () => void
}) {
  const latest = summary?.latest ?? null
  const current = summary?.current ?? null
  const scope = summary?.scope ?? null
  const active = latest !== null && ['queued', 'running', 'cancelling'].includes(latest.state)
  const interrupted = latest?.state === 'interrupted'
  const canStart = Boolean(
    summary?.runtime.state === 'ready' && scope && !active && !interrupted && !actionBusy,
  )
  const progress = latest && latest.counts.total > 0
    ? Math.round(
      ((latest.counts.transcribed
        + latest.counts.no_audio
        + latest.counts.no_speech
        + latest.counts.issues) / latest.counts.total) * 100,
    )
    : 0
  const processed = latest
    ? latest.counts.transcribed + latest.counts.no_audio + latest.counts.no_speech + latest.counts.issues
    : 0
  const runtime = summary?.runtime
  return (
    <section className="analysis-section" aria-labelledby="analysis-title">
      <div className="analysis-heading">
        <div>
          <p className="eyebrow">02 · 分析</p>
          <h2 id="analysis-title">產生繁體中文逐字稿</h2>
          <p>Clipen 會把音訊切成短片段，再由本機 Qwen3-ASR 辨識。信心過低、只有背景聲或發生重複幻覺的片段不會硬生逐字稿；保留的結果會轉為繁體中文並對回毛片時間。</p>
        </div>
        {!active && !interrupted && (
          <button
            className="button primary"
            onClick={onStart}
            disabled={!canStart}
          >
            {actionBusy ? '正在準備…' : current ? '重新分析並取代結果' : '開始分析'}
          </button>
        )}
      </div>

      {loading && !summary && (
        <div className="analysis-card" role="status">
          <span className="small-spinner" aria-hidden="true" />
          <div><strong>正在讀取分析狀態</strong><p>尚未開始新的素材處理。</p></div>
        </div>
      )}

      {summary?.blocked_reason && (
        <div className="analysis-outcome has-error" role="alert">
          <span className="outcome-symbol" aria-hidden="true">!</span>
          <div>
            <strong>目前不能開始逐字稿分析</strong>
            <p>{analysisBlockedCopy[summary.blocked_reason] ?? '請先確認素材狀態。'}</p>
          </div>
        </div>
      )}

      {runtime?.state === 'setup_required' && runtime.notice && (
        <div className="analysis-card setup-required runtime-setup">
          <span className="outcome-symbol" aria-hidden="true">↓</span>
          <div className="runtime-setup-content">
            <strong>Clipen 完整設定尚未完成</strong>
            <p>請關閉 Clipen，回到 Terminal 執行 <code>clipen install</code>。設定會取得同意後一次完成語音辨識環境與模型下載。</p>
            <dl className="runtime-facts">
              <div><dt>下載</dt><dd>約 {formatBytes(runtime.notice.download_bytes)}</dd></div>
              <div><dt>安裝後</dt><dd>約 {formatBytes(runtime.notice.installed_bytes)}</dd></div>
              <div><dt>模型</dt><dd>{runtime.notice.model.name} · 固定版本 {runtime.notice.model.revision.slice(0, 8)}</dd></div>
              <div><dt>位置</dt><dd title={runtime.notice.install_location}>{runtime.notice.install_location}</dd></div>
            </dl>
            <p className="runtime-license">MLX Speech 本機程式採用 <a href={runtime.notice.license.software_url} target="_blank" rel="noreferrer">{runtime.notice.license.software} 授權</a>；模型採用 <a href={runtime.notice.license.model_url} target="_blank" rel="noreferrer">{runtime.notice.license.model} 授權</a>。{runtime.notice.license.notice}</p>
          </div>
        </div>
      )}

      {runtime?.state === 'removing' && (
        <div className="analysis-card" role="status">
          <span className="small-spinner" aria-hidden="true" />
          <div><strong>{runtime.message}</strong><p>只會移除 Clipen 管理的模型與執行環境。</p></div>
        </div>
      )}

      {runtime?.state === 'failed' && (
        <div className="analysis-outcome has-error runtime-failed" role="alert">
          <span className="outcome-symbol" aria-hidden="true">!</span>
          <div><strong>本機語音辨識設定沒有完成</strong><p>{runtime.error_code ? asrSetupErrorCopy[runtime.error_code] ?? runtime.message : runtime.message}</p></div>
          <button className="button secondary" onClick={onRemoveRuntime} disabled={actionBusy}>清除未完成安裝</button>
        </div>
      )}

      {summary?.runtime.state === 'ready' && scope && !latest && (
        <div className="analysis-card scope-preview">
          <div>
            <strong>分析範圍已準備好</strong>
            <p>{scope.counts.audio_streams} 條音軌，共約 {scope.duration_seconds} 秒；預計使用 {formatBytes(scope.estimated_audio_bytes)} 暫存空間。</p>
          </div>
          {scope.counts.no_audio > 0 && <span>{scope.counts.no_audio} 個素材沒有音軌，會清楚標記並略過</span>}
        </div>
      )}

      {latest && active && (
        <div className="analysis-progress" role="status" aria-live="polite">
          <div className="progress-main">
            <span className="small-spinner" aria-hidden="true" />
            <div>
              <strong>{latest.state === 'cancelling' ? '正在保存 checkpoint 並停止…' : '正在辨識人聲並產生繁體中文逐字稿…'}</strong>
              <p>完成 {progress}% · 已處理 {processed.toLocaleString()} / {latest.counts.total.toLocaleString()} 個項目 · {latest.counts.issues} 個需要注意</p>
              {progress === 0 && latest.state !== 'cancelling' && <small>第一次辨識會先載入本機模型；低信心與只有背景聲的結果不會被當成可用口語。</small>}
              <progress value={progress} max={100} aria-label="本機語音辨識進度" />
            </div>
          </div>
          <button
            className="button secondary"
            onClick={() => onCancel(latest)}
            disabled={actionBusy || latest.state === 'cancelling'}
          >
            {latest.state === 'cancelling' ? '停止中…' : '停止分析'}
          </button>
        </div>
      )}

      {latest && interrupted && (
        <div className="analysis-outcome interrupted">
          <span className="outcome-symbol" aria-hidden="true">↻</span>
          <div>
            <strong>上次分析安全中斷</strong>
            <p>{latest.error_code ? analysisErrorCopy[latest.error_code] ?? '已完成的 checkpoint 仍保留。' : '已完成的 checkpoint 仍保留。'}</p>
          </div>
          <button className="button primary" onClick={() => onResume(latest)} disabled={actionBusy}>
            {actionBusy ? '正在接續…' : '接續分析'}
          </button>
        </div>
      )}

      {latest && ['failed', 'cancelled'].includes(latest.state) && (
        <div className={`analysis-outcome ${latest.state === 'failed' ? 'has-error' : ''}`}>
          <span className="outcome-symbol" aria-hidden="true">{latest.state === 'failed' ? '!' : '×'}</span>
          <div>
            <strong>{latest.state === 'failed' ? '這次分析沒有完成' : '分析已取消'}</strong>
            <p>{latest.error_code ? analysisErrorCopy[latest.error_code] ?? '請確認本機分析環境後再試一次。' : '毛片沒有被修改，可以重新開始分析。'}</p>
          </div>
        </div>
      )}

      {latest && processed > 0 && (active || ['failed', 'cancelled', 'interrupted'].includes(latest.state)) && (
        <div className="analysis-live-results">
          <strong>{active ? '先抽查目前結果' : '查看這次已保存的部分結果'}</strong>
          <p>{active
            ? '不必等整批完成；若前幾支逐字稿明顯不對，可以立即停止分析。'
            : '即使分析停止，已完成的項目仍可用來判斷設定是否合適。'}</p>
          <TranscriptResults
            projectId={projectId}
            run={latest}
            refreshToken={processed}
          />
        </div>
      )}

      {current && (
        <>
          <div className="analysis-results">
            <div>
              <strong>{current.counts.issues ? '繁體中文逐字稿已完成，部分素材需要注意' : '繁體中文逐字稿已準備好'}</strong>
              <p>已轉錄 {current.counts.transcribed} 個項目，{current.counts.no_speech} 個沒有人聲，{current.counts.no_audio} 個沒有音軌，{current.counts.issues} 個需要處理。</p>
              <p>
                {current.counts.issues
                  ? '先展開需要處理的素材，再抽查幾段成功逐字稿；其餘結果已保存，不必重新分析。'
                  : '抽查幾段逐字稿即可；結果已保存，稍後建立剪輯計畫時會直接重用。'}
              </p>
              <p>開始新的分析時，Clipen 會移除這份逐字稿與分析暫存；毛片與參考作品不會被刪除或修改。</p>
            </div>
            <span className="result-ready">分析完成</span>
          </div>
          <TranscriptResults key={current.run_id} projectId={projectId} run={current} />
        </>
      )}

      {runtime?.state === 'ready' && !active && (
        <details className="runtime-settings">
          <summary>本機語音辨識設定</summary>
          <div>
            <p>{runtime.message}。移除只會刪除 Clipen 管理的模型與執行環境，不會碰素材或逐字稿。</p>
            <button className="text-button danger" onClick={onRemoveRuntime} disabled={actionBusy}>移除本機語音辨識</button>
          </div>
        </details>
      )}
    </section>
  )
}

function MaterialsWorkspace({
  project,
  csrf,
  onProject,
  onBack,
}: {
  project: Project
  csrf: string
  onProject: (project: Project) => void
  onBack: () => void
}) {
  const [name, setName] = useState(project.name)
  const [brief, setBrief] = useState(project.brief)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [busyRole, setBusyRole] = useState<DirectoryRole | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [inventory, setInventory] = useState<InventorySummary | null>(null)
  const [inventoryPage, setInventoryPage] = useState<InventoryItemsPage | null>(null)
  const [inventoryLoading, setInventoryLoading] = useState(true)
  const [inventoryActionBusy, setInventoryActionBusy] = useState(false)
  const [analysis, setAnalysis] = useState<AnalysisSummary | null>(null)
  const [analysisActionBusy, setAnalysisActionBusy] = useState(false)

  const source = project.directories.find((directory) => directory.role === 'source')
  const reference = project.directories.find((directory) => directory.role === 'reference')

  useEffect(() => {
    let mounted = true
    getInventory(project.project_id)
      .then((result) => { if (mounted) setInventory(result) })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
      .finally(() => { if (mounted) setInventoryLoading(false) })
    return () => { mounted = false }
  }, [project.project_id, project.revision])

  const latestState = inventory?.latest?.state
  useEffect(() => {
    if (!latestState || !['queued', 'running', 'cancelling'].includes(latestState)) return
    let mounted = true
    const timer = window.setInterval(() => {
      getInventory(project.project_id)
        .then((result) => { if (mounted) setInventory(result) })
        .catch(() => { /* A later poll or the global reconnect state can recover. */ })
    }, 650)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [latestState, project.project_id])

  const currentInventoryId = source?.availability === 'available'
    ? inventory?.current?.run_id
    : undefined
  useEffect(() => {
    if (!currentInventoryId) return
    let mounted = true
    getInventoryItems(project.project_id, currentInventoryId)
      .then((result) => { if (mounted) setInventoryPage(result) })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
    return () => { mounted = false }
  }, [currentInventoryId, project.project_id])
  const visibleInventoryPage = inventoryPage?.inventory.run_id === currentInventoryId
    ? inventoryPage
    : null

  useEffect(() => {
    if (!currentInventoryId) return
    let mounted = true
    getAnalysis(project.project_id)
      .then((result) => { if (mounted) setAnalysis(result) })
      .catch((caught) => { if (mounted) setError(errorMessage(caught)) })
    return () => { mounted = false }
  }, [currentInventoryId, project.project_id])

  const visibleAnalysis = analysis && (
    analysis.scope?.inventory_run_id === currentInventoryId
    || analysis.latest?.inventory_run_id === currentInventoryId
    || analysis.current?.inventory_run_id === currentInventoryId
  ) ? analysis : null

  const latestAnalysisState = analysis?.latest?.state
  useEffect(() => {
    if (!latestAnalysisState || !['queued', 'running', 'cancelling'].includes(latestAnalysisState)) return
    let mounted = true
    let refreshing = false
    const refresh = () => {
      if (refreshing) return
      refreshing = true
      getAnalysis(project.project_id)
        .then((result) => { if (mounted) setAnalysis(result) })
        .catch(() => { /* A later poll can recover transient local UI errors. */ })
        .finally(() => { refreshing = false })
    }
    const runId = analysis?.latest?.run_id
    const stream = runId
      ? new EventSource(`/api/projects/${project.project_id}/analyses/${runId}/events`)
      : null
    if (stream) stream.onmessage = refresh
    const fallback = window.setInterval(refresh, 5_000)
    return () => {
      mounted = false
      stream?.close()
      window.clearInterval(fallback)
    }
  }, [analysis?.latest?.run_id, latestAnalysisState, project.project_id])

  const runtimeState = analysis?.runtime.state
  useEffect(() => {
    if (runtimeState !== 'removing') return
    let mounted = true
    let refreshing = false
    const timer = window.setInterval(() => {
      if (refreshing) return
      refreshing = true
      getAnalysis(project.project_id)
        .then((result) => { if (mounted) setAnalysis(result) })
        .catch(() => { /* A later poll can recover a transient local error. */ })
        .finally(() => { refreshing = false })
    }, 700)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [project.project_id, runtimeState])

  async function recoverConflict(caught: unknown) {
    if (caught instanceof ApiError && caught.code === 'revision_conflict') {
      try {
        const latest = await getProject(project.project_id)
        onProject(latest)
        setName(latest.name)
        setBrief(latest.brief)
        setDirty(false)
      } catch {
        // Keep the original conflict message when refreshing also fails.
      }
    }
    setError(errorMessage(caught))
  }

  async function saveDetails(event: FormEvent) {
    event.preventDefault()
    if (!dirty || !name.trim() || saving) return
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const updated = await updateProjectDetails(csrf, project, name, brief)
      setDirty(false)
      onProject(updated)
      setNotice('作品已保存')
    } catch (caught) {
      await recoverConflict(caught)
    } finally {
      setSaving(false)
    }
  }

  async function inspectMedia() {
    if (inventoryActionBusy || !source || source.availability !== 'available') return
    setInventoryActionBusy(true)
    setError(null)
    setNotice(null)
    try {
      let target = project
      if (dirty) {
        if (!name.trim()) return
        target = await updateProjectDetails(csrf, project, name, brief)
        setDirty(false)
        onProject(target)
      }
      const run = await startInventory(csrf, target)
      setInventory((previous) => ({ latest: run, current: previous?.current ?? null }))
      setNotice('素材檢查已開始，你可以留在這裡查看進度')
    } catch (caught) {
      await recoverConflict(caught)
    } finally {
      setInventoryActionBusy(false)
    }
  }

  async function stopInventory(run: InventoryRun) {
    if (inventoryActionBusy) return
    setInventoryActionBusy(true)
    setError(null)
    setNotice(null)
    try {
      const updated = await cancelInventory(csrf, project.project_id, run.run_id)
      setInventory((previous) => ({
        latest: updated,
        current: previous?.current ?? null,
      }))
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setInventoryActionBusy(false)
    }
  }

  async function analyzeMedia() {
    if (
      analysisActionBusy
      || !visibleAnalysis?.scope
      || visibleAnalysis.runtime.state !== 'ready'
    ) return
    setAnalysisActionBusy(true)
    setError(null)
    setNotice(null)
    try {
      const run = await startAnalysis(
        csrf,
        project,
        visibleAnalysis.scope.scope_id,
      )
      setAnalysis((previous) => previous && { ...previous, latest: run, current: null })
      setNotice('新的本機素材分析已開始，舊分析結果已移除；毛片不會被修改')
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setAnalysisActionBusy(false)
    }
  }

  async function stopAnalysis(run: AnalysisRun) {
    if (analysisActionBusy) return
    setAnalysisActionBusy(true)
    setError(null)
    try {
      const updated = await cancelAnalysis(csrf, project.project_id, run.run_id)
      setAnalysis((previous) => previous && { ...previous, latest: updated })
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setAnalysisActionBusy(false)
    }
  }

  async function continueAnalysis(run: AnalysisRun) {
    if (analysisActionBusy) return
    setAnalysisActionBusy(true)
    setError(null)
    try {
      const updated = await resumeAnalysis(csrf, project.project_id, run.run_id)
      setAnalysis((previous) => previous && { ...previous, latest: updated })
      setNotice('已從上次保存的 checkpoint 接續分析')
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setAnalysisActionBusy(false)
    }
  }

  async function removeLocalASR() {
    if (analysisActionBusy) return
    const confirmed = window.confirm(
      '移除 Clipen 管理的本機語音辨識？毛片與已產生的逐字稿不會被刪除。',
    )
    if (!confirmed) return
    setAnalysisActionBusy(true)
    setError(null)
    setAnalysis((previous) => previous && {
      ...previous,
      runtime: {
        ...previous.runtime,
        state: 'removing',
        phase: 'removing',
        message: '正在停止分析並移除本機語音辨識…',
      },
    })
    try {
      const runtime = await removeASR(csrf)
      setAnalysis((previous) => previous && { ...previous, runtime })
      setNotice('本機語音辨識已移除；毛片與分析成果沒有變動，需要時請在 Terminal 執行 clipen install')
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setAnalysisActionBusy(false)
    }
  }

  async function choose(role: DirectoryRole) {
    if (busyRole) return
    setBusyRole(role)
    setError(null)
    setNotice(null)
    try {
      const selection = await selectDirectory(csrf, role)
      if (!selection) return
      const updated = await saveDirectory(
        csrf,
        project,
        role,
        selection.selection_token,
      )
      onProject(updated)
      setNotice(role === 'source' ? '毛片位置已保存' : '參考作品位置已保存')
    } catch (caught) {
      await recoverConflict(caught)
    } finally {
      setBusyRole(null)
    }
  }

  async function remove(role: DirectoryRole) {
    if (busyRole) return
    setBusyRole(role)
    setError(null)
    setNotice(null)
    try {
      onProject(await removeDirectory(csrf, project, role))
      setNotice('資料夾引用已移除，原始檔案沒有變動')
    } catch (caught) {
      await recoverConflict(caught)
    } finally {
      setBusyRole(null)
    }
  }

  return (
    <main className="workspace-main">
      <button className="back-button" onClick={onBack}>← 所有作品</button>
      <div className="workspace-layout">
        <aside className="stage-sidebar" aria-label="作品階段">
          <div className="sidebar-project">
            <span>目前作品</span>
            <strong>{project.name}</strong>
          </div>
          <ol>
            {stages.map((stage, index) => (
              <li
                key={stage}
                className={
                  index === 0
                    ? currentInventoryId ? 'completed' : 'active'
                    : index === 1 && currentInventoryId ? 'active' : 'locked'
                }
              >
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <strong>{stage}</strong>
                  {index === 0 && currentInventoryId && <small>已檢查</small>}
                  {index > 1 && <small>尚未開放</small>}
                </div>
              </li>
            ))}
          </ol>
        </aside>

        <section className="materials-panel" aria-labelledby="materials-title">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">01 · 素材</p>
              <h1 id="materials-title">準備這次剪輯</h1>
              <p>先告訴 Clipen 素材在哪裡、希望剪成什麼樣子。檢查素材後，可分別啟動本機分析；只有明確同意傳送縮圖，才會呼叫 AI。</p>
            </div>
            <div className={`save-state ${dirty ? 'unsaved' : ''}`} aria-live="polite">
              <span aria-hidden="true" />{saving ? '保存中' : dirty ? '尚未保存' : '已保存'}
            </div>
          </div>

          {(error || notice) && (
            <div className={error ? 'inline-message error' : 'inline-message success'} role={error ? 'alert' : 'status'}>
              {error ?? notice}
            </div>
          )}

          <form className="project-form" onSubmit={saveDetails}>
            <label htmlFor="workspace-name">作品名稱</label>
            <input
              id="workspace-name"
              maxLength={120}
              value={name}
              onChange={(event) => { setName(event.target.value); setDirty(true); setNotice(null) }}
            />
            <label htmlFor="workspace-brief">剪輯需求</label>
            <textarea
              id="workspace-brief"
              maxLength={8000}
              rows={5}
              placeholder="例如：剪成 30 秒的手沖教學，步驟清楚、節奏俐落，保留沖水聲。"
              value={brief}
              onChange={(event) => { setBrief(event.target.value); setDirty(true); setNotice(null) }}
            />
            <div className="form-footer">
              <span>{brief.length.toLocaleString()} / 8,000</span>
              <button className="button primary" type="submit" disabled={!dirty || !name.trim() || saving}>
                {saving ? '正在保存…' : '保存作品資料'}
              </button>
            </div>
          </form>

          <div className="directory-section">
            <div className="section-title-row">
              <div><h2>素材位置</h2><p>macOS 會讓你親自選擇資料夾；Clipen 不接受手動輸入路徑。</p></div>
            </div>
            <DirectoryCard
              role="source"
              reference={source}
              busy={busyRole === 'source'}
              onChoose={() => void choose('source')}
              onRemove={() => void remove('source')}
            />
            <DirectoryCard
              role="reference"
              reference={reference}
              busy={busyRole === 'reference'}
              onChoose={() => void choose('reference')}
              onRemove={() => void remove('reference')}
            />
          </div>

          <div className="scope-note">
            <span className="lock-icon" aria-hidden="true" />
            <div><strong>目前只保存位置</strong><p>Clipen 不會複製、搬移、重新命名或修改毛片。只有按下下方的「檢查素材」才會開始讀取媒體資訊。</p></div>
          </div>

          <InventorySection
            summary={inventory}
            page={visibleInventoryPage}
            loading={inventoryLoading}
            sourceReady={source?.availability === 'available'}
            actionBusy={inventoryActionBusy}
            onStart={() => void inspectMedia()}
            onCancel={(run) => void stopInventory(run)}
          />
          {currentInventoryId && <EvidenceSection key={`${project.project_id}:${currentInventoryId}`} projectId={project.project_id} csrf={csrf} inventoryId={currentInventoryId} />}
          {currentInventoryId && (
            <AnalysisSection
              projectId={project.project_id}
              summary={visibleAnalysis}
              loading={!visibleAnalysis}
              actionBusy={analysisActionBusy}
              onStart={() => void analyzeMedia()}
              onCancel={(run) => void stopAnalysis(run)}
              onResume={(run) => void continueAnalysis(run)}
              onRemoveRuntime={() => void removeLocalASR()}
            />
          )}
        </section>
      </div>
    </main>
  )
}

function App() {
  const [application, setApplication] = useState<ApplicationState>({ kind: 'connecting' })
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(projectFromUrl())

  useEffect(() => {
    const controller = new AbortController()
    async function connect() {
      try {
        await connectLocalSession()
        const [status, projects] = await Promise.all([getStatus(), listProjects()])
        if (!controller.signal.aborted) setApplication({ kind: 'ready', status, projects })
      } catch (caught) {
        if (!controller.signal.aborted) {
          setApplication({
            kind: 'failed',
            message: errorMessage(caught),
            serverUnavailable: !(caught instanceof ApiError),
          })
        }
      }
    }
    void connect()
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const updateRoute = () => setSelectedProjectId(projectFromUrl())
    window.addEventListener('popstate', updateRoute)
    return () => window.removeEventListener('popstate', updateRoute)
  }, [])

  const selectedProject = useMemo(
    () => application.kind === 'ready'
      ? application.projects.find((project) => project.project_id === selectedProjectId)
      : undefined,
    [application, selectedProjectId],
  )

  function navigate(project: Project | null) {
    const url = project ? `/?project=${encodeURIComponent(project.project_id)}` : '/'
    window.history.pushState(null, '', url)
    setSelectedProjectId(project?.project_id ?? null)
  }

  function replaceProject(project: Project) {
    setApplication((current) => {
      if (current.kind !== 'ready') return current
      const projects = [project, ...current.projects.filter((item) => item.project_id !== project.project_id)]
      return { ...current, projects }
    })
  }

  const ready = application.kind === 'ready'
  const providerReady = ready && application.status.provider.state === 'ready'

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => navigate(null)} aria-label="Clipen 作品首頁">clipen</button>
        <div className={`connection-badge ${providerReady ? 'is-ready' : ''}`} aria-live="polite">
          <span aria-hidden="true" />
          {application.kind === 'connecting' && '正在連接'}
          {application.kind === 'failed' && '連線中斷'}
          {ready && (providerReady ? 'AI 已連接' : 'AI 需要處理')}
        </div>
      </header>

      {application.kind === 'connecting' && (
        <main className="system-main"><section className="system-card"><div className="spinner" /><div><p className="eyebrow">正在準備</p><h1>連接本機工作台…</h1><p>正在載入你的作品與 Codex 狀態。</p></div></section></main>
      )}

      {application.kind === 'failed' && (
        <main className="system-main"><section className="system-card warning" role="alert"><span className="notice-symbol">!</span><div><p className="eyebrow">無法連線</p><h1>{application.serverUnavailable ? 'Clipen 本機服務未啟動' : '本機工作台拒絕連線'}</h1><p>{application.message}</p>{application.serverUnavailable && <code>clipen codex</code>}</div></section></main>
      )}

      {ready && (
        <>
          <ProviderNotice status={application.status} />
          {selectedProjectId && !selectedProject ? (
            <main className="system-main"><section className="system-card warning"><span className="notice-symbol">!</span><div><p className="eyebrow">找不到作品</p><h1>這個作品不存在或無法讀取</h1><button className="button primary" onClick={() => navigate(null)}>回到作品首頁</button></div></section></main>
          ) : selectedProject ? (
            <MaterialsWorkspace key={selectedProject.project_id} project={selectedProject} csrf={application.status.session.csrf_token} onProject={replaceProject} onBack={() => navigate(null)} />
          ) : (
            <ProjectHome
              projects={application.projects}
              csrf={application.status.session.csrf_token}
              onProjects={(projects) => setApplication({ ...application, projects })}
              onOpen={(project) => navigate(project)}
            />
          )}
        </>
      )}
    </div>
  )
}

export default App
