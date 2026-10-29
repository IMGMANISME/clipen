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
    async function migrate() {
      const submitted: string[] = []
      let offset = 0
      while (true) {
        const page = await getProductionPrompts(projectId, offset)
        if (disposed || startedAt !== version.current) return
        submitted.push(...page.prompts.filter((p) => p.kind === kind && p.source === 'submitted').map((p) => p.text))
        offset += page.prompts.length
        if (offset >= page.total) break
        // An incomplete history cannot establish whether this draft was sent.
        if (!page.prompts.length) return
      }
      const restored = recoverUnsentDraft(legacy!, submitted, fallback)
      setText((current) => {
        if (current !== fallback) return current
        try { localStorage.setItem(key, restored); localStorage.removeItem(legacyKey) } catch { /* Keep it visible in the form. */ }
        return restored
      })
    }
    void migrate().catch(() => { /* Leave the legacy draft intact if any history page fails. */ })
    return () => { disposed = true }
  }, [projectId, kind, key, legacyKey, fallback])
  return [text, update]
}
