import { createContext, useContext } from 'react'

/** The AI provider this Clipen session was started with (`clipen codex`, `clipen claude`). */
export type ProviderInfo = { name: string }

export const ProviderContext = createContext<ProviderInfo>({ name: 'Codex' })

export function useProvider(): ProviderInfo {
  return useContext(ProviderContext)
}
