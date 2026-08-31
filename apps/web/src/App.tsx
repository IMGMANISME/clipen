import { useEffect, useState } from 'react'
import './index.css'

type ProviderState =
  | 'ready'
  | 'login_required'
  | 'unsupported'
  | 'confinement_failed'
  | 'unavailable'

type StatusPayload = {
  product: { name: string; workspace: 'local' }
  provider: { name: string; state: ProviderState; message: string }
  session: { csrf_token: string }
}

type Connection =
  | { kind: 'connecting' }
  | { kind: 'connected'; status: StatusPayload }
  | { kind: 'failed'; message: string }

const stages = ['素材', '分析', '計畫', '剪輯', '匯出']

const providerHelp: Record<Exclude<ProviderState, 'ready'>, string> = {
  login_required: '請先在 Terminal 完成 Codex 登入，再重新啟動 Clipen。',
  unsupported: '請更新 Codex CLI，再重新啟動 Clipen。',
  confinement_failed: '安全隔離檢查沒有通過，因此 Clipen 不會把素材交給 AI。',
  unavailable: '請確認這台電腦已安裝 Codex CLI，且可從 Terminal 執行。',
}

function bootstrapToken(): string | null {
  if (!window.location.hash.startsWith('#')) return null
  const token = new URLSearchParams(window.location.hash.slice(1)).get('bootstrap')
  return token && token.length >= 32 && token.length <= 256 ? token : null
}

async function responseMessage(response: Response, fallback: string): Promise<string> {
  try {
    const payload = (await response.json()) as { error?: { message?: unknown } }
    return typeof payload.error?.message === 'string' ? payload.error.message : fallback
  } catch {
    return fallback
  }
}

function isStatusPayload(value: unknown): value is StatusPayload {
  if (!value || typeof value !== 'object') return false
  const payload = value as Partial<StatusPayload>
  const provider = payload.provider
  return Boolean(
    payload.product?.name === 'Clipen' &&
      payload.product.workspace === 'local' &&
      provider &&
      provider.name === 'Codex' &&
      ['ready', 'login_required', 'unsupported', 'confinement_failed', 'unavailable'].includes(
        provider.state ?? '',
      ) &&
      typeof provider.message === 'string' &&
      typeof payload.session?.csrf_token === 'string',
  )
}

function App() {
  const [connection, setConnection] = useState<Connection>({ kind: 'connecting' })

  useEffect(() => {
    const controller = new AbortController()

    async function connect() {
      try {
        const token = bootstrapToken()
        if (token) {
          const response = await fetch('/api/session/bootstrap', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-Clipen-Bootstrap': token },
            signal: controller.signal,
          })
          if (!response.ok) {
            throw new Error(await responseMessage(response, '無法建立本機工作台連線。'))
          }
          window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
        }

        const response = await fetch('/api/status', {
          credentials: 'same-origin',
          cache: 'no-store',
          signal: controller.signal,
        })
        if (!response.ok) {
          throw new Error(await responseMessage(response, '無法讀取 Clipen 狀態。'))
        }
        const payload: unknown = await response.json()
        if (!isStatusPayload(payload)) throw new Error('Clipen 回傳了無法辨識的狀態。')
        setConnection({ kind: 'connected', status: payload })
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setConnection({
          kind: 'failed',
          message: error instanceof Error ? error.message : '無法連接 Clipen。',
        })
      }
    }

    void connect()
    return () => controller.abort()
  }, [])

  const provider = connection.kind === 'connected' ? connection.status.provider : null
  const ready = provider?.state === 'ready'

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Clipen 工作台首頁">clipen</a>
        <div className={`connection-badge ${ready ? 'is-ready' : ''}`} aria-live="polite">
          <span aria-hidden="true" />
          {connection.kind === 'connecting' && '正在連接'}
          {connection.kind === 'failed' && '連線中斷'}
          {provider && (ready ? 'AI 已連接' : '需要處理')}
        </div>
      </header>

      <main>
        <section className="hero" aria-labelledby="hero-title">
          <p className="eyebrow">本機剪輯工作台</p>
          <h1 id="hero-title">先把剪輯想清楚，<br />再交給 AI 執行。</h1>
          <p className="intro">
            加入參考作品與毛片後，Clipen 會整理風格、內容和逐字稿，提出一份由你確認的剪輯計畫。
          </p>
        </section>

        <section className="workflow" aria-label="Clipen 作品流程">
          {stages.map((stage, index) => (
            <div className="stage" key={stage}>
              <span aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
              <strong>{stage}</strong>
            </div>
          ))}
        </section>

        {connection.kind === 'connecting' && (
          <section className="system-card connecting" aria-live="polite">
            <div className="spinner" aria-hidden="true" />
            <div>
              <p className="eyebrow">正在準備</p>
              <h2>連接本機工作台…</h2>
              <p>正在確認這次啟動與 Codex 狀態。</p>
            </div>
          </section>
        )}

        {connection.kind === 'failed' && (
          <section className="system-card warning" role="alert">
            <span className="status-symbol" aria-hidden="true">!</span>
            <div>
              <p className="eyebrow">無法連線</p>
              <h2>請從 Terminal 重新啟動 Clipen</h2>
              <p>{connection.message}</p>
              <p className="terminal-hint"><code>./clipen codex</code></p>
            </div>
          </section>
        )}

        {provider && provider.state !== 'ready' && (
          <section className="system-card warning" role="alert">
            <span className="status-symbol" aria-hidden="true">!</span>
            <div>
              <p className="eyebrow">{provider.name} 需要處理</p>
              <h2>{provider.message}</h2>
              <p>{providerHelp[provider.state]}</p>
            </div>
          </section>
        )}

        {ready && (
          <section className="workspace-card" aria-labelledby="workspace-title">
            <div className="workspace-copy">
              <p className="eyebrow">工作台已就緒</p>
              <h2 id="workspace-title">還沒有作品</h2>
              <p>作品會從選擇毛片與參考作品開始，素材原檔會留在原本的位置，不會被 Clipen 修改。</p>
            </div>
            <div className="local-note">
              <span className="lock-icon" aria-hidden="true" />
              <div>
                <strong>只在這台電腦執行</strong>
                <p>關閉分頁不會中斷 Clipen；回到 Terminal 顯示的網址即可重新開啟。</p>
              </div>
            </div>
          </section>
        )}
      </main>
    </div>
  )
}

export default App
