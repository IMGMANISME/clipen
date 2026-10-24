import { useEffect, useState } from 'react'
import { getProviderAccount } from './api'
import type { ProviderAccount, QuotaWindow } from './api'

type Props = { label: string; ready: boolean; providerReady: boolean; providerName: string }

const EFFORTS: Record<string, string> = { minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高', ultra: '極高' }
const WINDOWS: Record<string, string> = { five_hour: '5 小時額度', weekly: '每週額度', other: '額度' }

function resetLabel(seconds: number | null | undefined): string | null {
  if (!seconds) return null
  const date = new Date(seconds * 1000)
  const soon = date.getTime() - Date.now() < 24 * 3600 * 1000
  const text = soon ? date.toLocaleTimeString('zh-TW', { hour12: false, hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString('zh-TW', { month: 'numeric', day: 'numeric' })
  return `${text} 重置`
}

function remaining(window: QuotaWindow): number { return Math.max(0, 100 - window.used_percent) }

/** Which AI, which model and how much quota is left; nothing here calls a model. */
export function ProviderBadge({ label, ready, providerReady, providerName }: Props) {
  const [account, setAccount] = useState<ProviderAccount | null>(null)
  useEffect(() => {
    if (!providerReady) return
    let disposed = false
    const load = () => void getProviderAccount().then((value) => { if (!disposed) setAccount(value) }).catch(() => {})
    load()
    const timer = window.setInterval(load, 120000)
    window.addEventListener('focus', load)
    return () => { disposed = true; window.clearInterval(timer); window.removeEventListener('focus', load) }
  }, [providerReady])

  const model = account?.model
  const quota = account?.quota
  const primary = quota?.windows[0]
  const low = quota?.limited || (primary !== undefined && remaining(primary) < 20)
  const summary = ready ? (providerReady ? [providerName, model?.name].filter(Boolean).join(' · ') : `${providerName} 需要處理`) : label
  const pill = quota?.limited ? '額度已用完' : primary ? `剩 ${remaining(primary)}%` : null

  if (!ready || !providerReady) {
    return <div className="connection-badge" aria-live="polite"><span aria-hidden="true" />{summary}</div>
  }
  return <details className="provider-badge">
    <summary className={`connection-badge is-ready ${low ? 'is-low' : ''}`} aria-label={`目前使用 ${summary}${pill ? `，${pill}` : ''}，查看模型與額度`}>
      <span aria-hidden="true" />{summary}{pill && <em className="provider-quota-pill">{pill}</em>}
    </summary>
    <div className="provider-popover">
      <dl>
        <div><dt>AI</dt><dd>{providerName}{quota?.plan ? `（${quota.plan.charAt(0).toUpperCase()}${quota.plan.slice(1)} 方案）` : ''}</dd></div>
        <div><dt>模型</dt><dd>{model ? <>{model.name}{model.effort ? `，推理強度${EFFORTS[model.effort] ?? model.effort}` : ''}</> : '第一次製作後顯示'}</dd></div>
        {quota?.source === 'live' && quota.windows.map((window) => <div key={window.kind}>
          <dt>{WINDOWS[window.kind]}</dt>
          <dd><meter min={0} max={100} low={20} high={50} optimum={100} value={remaining(window)} aria-label={`${WINDOWS[window.kind]}剩 ${remaining(window)}%`} />
            <span>剩 {remaining(window)}%{resetLabel(window.resets_at) ? `，${resetLabel(window.resets_at)}` : ''}</span></dd>
        </div>)}
        {quota?.source === 'last_run' && <div><dt>額度</dt><dd>{quota.limited ? '已達上限' : quota.warning ? '快用完了' : '可以使用'}
          {resetLabel(quota.resets_at) ? `，${resetLabel(quota.resets_at)}` : ''}</dd></div>}
        {!quota && <div><dt>額度</dt><dd>{providerName === 'Claude' ? '第一次製作後顯示' : '暫時讀不到'}</dd></div>}
      </dl>
      {quota?.source === 'last_run' && <p>Claude Code 不提供剩餘百分比，這是最近一次製作時回報的狀態。</p>}
      {model && providerName === 'Codex' && <p>Clipen 使用 Codex 帳號的預設模型，不會套用你在 Codex 裡的模型設定。</p>}
    </div>
  </details>
}
