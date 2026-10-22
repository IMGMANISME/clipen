import { createContext, useContext } from 'react'

/** The AI provider this Clipen session was started with (`clipen codex`, `clipen claude`). */
export type ProviderInfo = { name: string; legacyWorkflows: boolean }

export const ProviderContext = createContext<ProviderInfo>({ name: 'Codex', legacyWorkflows: true })

export function useProvider(): ProviderInfo {
  return useContext(ProviderContext)
}
