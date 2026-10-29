import { useEffect, useId, useState } from 'react'
import { getProductionMusic, uploadProductionMusic } from './api'
import type { MusicAsset } from './api'
import { musicChoices } from './musicChoices'

type Props = { projectId: string; csrf: string; value: string; onChange: (value: string) => void; disabled: boolean; defaultLabel?: string }
export function ProductionMusicPicker({ projectId, csrf, value, onChange, disabled, defaultLabel = '讓 AI 尋找配樂' }: Props) {
  const id = useId()
  const [assets, setAssets] = useState<MusicAsset[]>([])
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const choices = musicChoices(assets, value)
  const matching = choices.filter(({ asset }) => asset.asset_id === value || asset.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  useEffect(() => { let disposed = false; void getProductionMusic(projectId).then((rows) => { if (!disposed) setAssets(rows) }).catch((e) => { if (!disposed) setError(String(e)) }); return () => { disposed = true } }, [projectId, value])
  async function upload(file?: File) {
    if (!file || disabled) return
    if (file.size > 128 * 1024 * 1024) { setError('音樂檔上限為 128 MB。'); return }
    setUploading(true); setError('')
    try { const asset = await uploadProductionMusic(csrf, projectId, file); setAssets((rows) => [...rows.filter((a) => a.asset_id !== asset.asset_id), asset]); onChange(asset.asset_id) }
    catch (e) { setError(e instanceof Error ? e.message : '音樂上傳失敗。') }
    finally { setUploading(false) }
  }
  return <fieldset className="production-music-picker" disabled={disabled || uploading}>
    <legend>配樂</legend>
    {choices.length > 5 && <label>搜尋配樂<input type="search" aria-label="搜尋配樂" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="輸入曲名" /></label>}
    <label htmlFor={`${id}-choice`}>使用哪首音樂？</label>
    <select id={`${id}-choice`} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{defaultLabel}</option>
      {matching.map(({ asset, label }) => <option key={asset.asset_id} value={asset.asset_id}>{label}</option>)}
    </select>
    {assets.length > choices.length && <p>相同音源與授權的重複項目已合併顯示。</p>}
    {query.trim() && !matching.some(({ asset }) => asset.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) && <p role="status">沒有符合的曲目；目前選用的配樂仍保留。</p>}
    <label htmlFor={`${id}-file`}>或選擇自己的音樂檔</label>
    <input id={`${id}-file`} type="file" accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,.aiff,.aif" onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = '' }} />
    <p>{uploading ? '正在匯入與檢查音樂…' : '檔案會保存在這個作品的本機資料內。指定後，AI 會使用這首歌安排剪輯與混音。'}</p>
    {error && <p role="alert" className="inline-message error">{error}</p>}
  </fieldset>
}
