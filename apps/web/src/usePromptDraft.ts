import { useEffect, useRef, useState } from 'react'
import { getProductionPrompts } from './api'
import { recoverUnsentDraft } from './reviewDraft'

type Kind = 'production' | 'refinement'
export function usePromptDraft(projectId: string, kind: Kind, fallback = '', runId = ''): [string, (value: string) => void] {
  const legacyKey = `clipen:prompt-draft:${projectId}:${kind}`
  const key = `clipen:prompt-draft:v2:${projectId}:${kind}:${runId}`
  const version = useRef(0)
  const [text, setText] = useState(() => { try { return localStorage.getItem(key) ?? fallback } catch { return fallback } })
  function update(value: string) {
    version.current += 1
    setText(value)
    try { localStorage.setItem(key, value) } catch { /* History still persists in the project database. */ }
  }
  useEffect(() => {
    let disposed = false
    const startedAt = version.current
    try { if (localStorage.getItem(key) !== null) return } catch { /* Use the project history. */ }
    // Migrate an actual unsent draft, never manufacture one from submitted history.
    let legacy: string | null = null
    try { legacy = localStorage.getItem(legacyKey) } catch { return }
    if (legacy === null) return
    void getProductionPrompts(projectId).then(({ prompts }) => {
      if (disposed || startedAt !== version.current) return
      const submitted = prompts.filter((p) => p.kind === kind && p.source === 'submitted').map((p) => p.text)
      const restored = recoverUnsentDraft(legacy!, submitted, fallback)
      setText((current) => {
        if (current !== fallback) return current
        try { localStorage.setItem(key, restored); localStorage.removeItem(legacyKey) } catch { /* Keep it visible in the form. */ }
        return restored
      })
    }).catch(() => { /* Leave the legacy draft intact; history can be retried explicitly. */ })
    return () => { disposed = true }
  }, [projectId, kind, key, legacyKey, fallback])
  return [text, update]
}
