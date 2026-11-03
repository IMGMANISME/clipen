import type { CSSProperties } from 'react'
const paths = {
  film: 'M4 3h16v18H4zM4 8h16M4 16h16M8 3v5m8-5v5M8 16v5m8-5v5',
  play: 'm9 5 11 7-11 7V5Z',
  edit: 'm14 5 5 5M4 20l4-1L20 7a2.1 2.1 0 0 0-3-3L5 16l-1 4Z',
  sparkle: 'm12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7L12 3Zm7-2v4m-2-2h4',
  folder: 'M3 7V5h6l2 2h10v13H3V7Z',
  plus: 'M12 5v14M5 12h14',
  arrow: 'M5 12h14m-6-6 6 6-6 6',
  audio: 'M4 9v6m4-10v14m4-16v18m4-16v14m4-10v6',
  text: 'M4 5h16M12 5v15m-4 0h8',
  layers: 'm12 3 10 5-10 5L2 8l10-5Zm-9 9 9 5 9-5M3 16l9 5 9-5',
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
  check: 'm5 12 4 4L19 6',
  'arrow-left': 'M19 12H5m6-6-6 6 6 6',
}
export type StudioIconName = keyof typeof paths
export function StudioIcon({ name, size = 18, style }: { name: StudioIconName; size?: number; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}><path d={paths[name]} /></svg>
}
