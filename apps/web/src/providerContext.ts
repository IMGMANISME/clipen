import { createContext, useContext } from 'react'

/** The AI provider this Clipen session was started with (`clipen codex`, `clipen claude`). */
type ProviderInfo = { name: string; id?: string }

export const ProviderContext = createContext<ProviderInfo>({ name: 'Codex' })

export function useProvider(): ProviderInfo {
  return useContext(ProviderContext)
}

const NAMES: Record<string, string> = { codex: 'Codex', claude: 'Claude' }

/** A provider's display name from its id, for runs created with another AI. */
export function providerName(id: string | null | undefined): string {
  return (id && NAMES[id]) || 'AI'
}
