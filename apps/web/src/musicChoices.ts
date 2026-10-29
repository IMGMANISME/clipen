import type { MusicAsset } from './api'

export function musicChoices(assets: MusicAsset[], selectedId: string) {
  const groups = new Map<string, MusicAsset[]>()
  for (const asset of assets) {
    const key = asset.duplicate_group || asset.asset_id
    const group = groups.get(key) ?? []
    group.push(asset); groups.set(key, group)
  }
  const choices = [...groups.values()].map((group) => ({ asset: group.find((a) => a.asset_id === selectedId) ?? group[0], copies: group.length }))
  const names = new Map<string, number>()
  return choices.map(({ asset, copies }) => {
    const base = `${asset.name}（${Math.round(asset.duration_seconds)} 秒）`
    const version = (names.get(base) ?? 0) + 1
    names.set(base, version)
    return { asset, copies, label: `${base}${version > 1 ? ` · 版本 ${version}` : ''}` }
  })
}
