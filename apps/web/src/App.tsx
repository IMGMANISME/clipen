import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import {
  ApiError,
  bootstrapSession,
  createProject,
  getProject,
  getStatus,
  listProjects,
  removeDirectory,
  saveDirectory,
  selectDirectory,
  updateProjectDetails,
} from './api'
import type {
  DirectoryAvailability,
  DirectoryReference,
  DirectoryRole,
  Project,
  ProviderState,
  StatusPayload,
} from './api'
import './index.css'

type ApplicationState =
  | { kind: 'connecting' }
  | { kind: 'failed'; message: string }
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

function fragmentToken(): string | null {
  if (!window.location.hash.startsWith('#')) return null
  const token = new URLSearchParams(window.location.hash.slice(1)).get('bootstrap')
  return token && token.length >= 32 && token.length <= 256 ? token : null
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

  const source = project.directories.find((directory) => directory.role === 'source')
  const reference = project.directories.find((directory) => directory.role === 'reference')

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
              <li key={stage} className={index === 0 ? 'active' : 'locked'}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><strong>{stage}</strong>{index > 0 && <small>尚未開放</small>}</div>
              </li>
            ))}
          </ol>
        </aside>

        <section className="materials-panel" aria-labelledby="materials-title">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">01 · 素材</p>
              <h1 id="materials-title">準備這次剪輯</h1>
              <p>先告訴 Clipen 素材在哪裡、希望剪成什麼樣子。這一步不會讀取媒體內容或呼叫 AI。</p>
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
            <div><strong>這一步只保存位置</strong><p>Clipen 不會複製、搬移、重新命名或修改毛片。媒體檢查會在下一步另外取得同意。</p></div>
          </div>
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
        const token = fragmentToken()
        if (token) {
          await bootstrapSession(token)
          window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
        }
        const [status, projects] = await Promise.all([getStatus(), listProjects()])
        if (!controller.signal.aborted) setApplication({ kind: 'ready', status, projects })
      } catch (caught) {
        if (!controller.signal.aborted) setApplication({ kind: 'failed', message: errorMessage(caught) })
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
        <main className="system-main"><section className="system-card warning" role="alert"><span className="notice-symbol">!</span><div><p className="eyebrow">無法連線</p><h1>請從 Terminal 重新啟動 Clipen</h1><p>{application.message}</p><code>./clipen codex</code></div></section></main>
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
