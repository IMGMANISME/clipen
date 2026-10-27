import { useEffect, useState } from 'react'
import { getPowerStatus } from './api'
import type { PowerStatus } from './api'

/** Warns before AI work on battery: closing the lid then sleeps the Mac and pauses production. */
export function PowerNotice() {
  const [power, setPower] = useState<PowerStatus | null>(null)
  useEffect(() => {
    let disposed = false
    const load = () => void getPowerStatus().then((value) => { if (!disposed) setPower(value) }).catch(() => {})
    load()
    const timer = window.setInterval(load, 60000)
    window.addEventListener('focus', load)
    return () => { disposed = true; window.clearInterval(timer); window.removeEventListener('focus', load) }
  }, [])
  if (!power?.on_battery) return null
  return <p className="power-notice" role="note">
    <strong>Mac 目前使用電池{power.percent !== null ? `（${power.percent}%）` : ''}。</strong>
    製作期間闔上螢幕會讓 Mac 睡著，製作也會跟著暫停；建議先接上電源，或保持螢幕打開。
  </p>
}
