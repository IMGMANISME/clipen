/** Human explanations describe only known causes; raw codes stay in diagnostics. */
export type ProductionProblem = { title: string; reason: string; next: string }
const problems: Record<string, ProductionProblem> = {
  provider_protocol_error: { title: 'AI 未正常完成製作流程', reason: 'AI 的回傳結果未能完成 Clipen 的交付流程。', next: '先確認保留的製作結果，再決定是否重新製作。' },
  delivery_blocked: { title: '影片還沒通過交付檢查', reason: 'AI 做出了草稿，但下方列出的項目還沒通過 Clipen 的檢查，所以不算完成。', next: '草稿已保留，可以先看影片。到「AI 打磨」說明要怎麼處理尚待完成的項目，例如快剪段落改用配樂、對話段落保留完整句子。' },
  provider_stopped_early: { title: 'AI 提早結束了製作', reason: '{provider} 在完成交付前就結束了這次工作。', next: '草稿已保留，按「繼續這份草稿」接續製作。' },
  provider_usage_limit: { title: '{provider} 額度暫時不足', reason: '這次 AI 工作因用量限制而停止。', next: '草稿已保留。待 {provider} 額度恢復後按「繼續這份草稿」接續製作；額度還沒恢復時會再次停止，草稿不受影響。' },
  provider_authentication_required: { title: '{provider} 需要重新登入', reason: 'AI 連線的登入狀態已失效。', next: '完成 {provider} 登入並重啟 Clipen，再重新製作。' },
  provider_network_error: { title: '與 AI 的連線中斷', reason: '這次工作未能維持與 {provider} 的網路連線。', next: '確認網路恢復，再重新製作。' },
  provider_timed_out: { title: 'AI 工作逾時', reason: '這次工作超過執行時間限制，尚未完成交付。', next: '草稿已保留，按「繼續這份草稿」接續製作。' },
  tool_budget_exhausted: { title: '這次製作尚未完成', reason: 'AI 已達這次工作允許的操作上限，並非影片已經完成。', next: '先查看保留的結果，試著縮小製作範圍後重新製作。' },
  preview_budget_exhausted: { title: '這次影片尚未完成', reason: 'AI 已達這次工作允許的預覽輸出上限。', next: '先查看目前預覽，再以明確的修改需求建立新工作。' },
  provider_permission_error: { title: 'AI 無法執行需要的操作', reason: '{provider} 回報權限問題。', next: '查看詳細資訊與 {provider} 的權限設定，處理後再重新製作。' },
  provider_unavailable: { title: '目前無法使用 {provider}', reason: 'Clipen 無法使用 AI 執行環境。', next: '確認 {provider} 可以啟動，重啟 Clipen 後再試。' },
  provider_launch_failed: { title: 'AI 工作未能啟動', reason: 'Clipen 啟動 AI 執行程序時失敗。', next: '確認 {provider} 可以啟動，重啟 Clipen 後再試。' },
  provider_mismatch: { title: '這份工作屬於另一個 AI', reason: '這個工作是用其他 AI 建立的，Clipen 不會改用目前的 AI 接續。', next: '用建立它的 AI 重新啟動 Clipen 再繼續，或建立新的製作工作。' },
  controller_stopped: { title: 'Clipen 關閉時中斷了製作', reason: '上次關閉 Clipen 時，這個工作尚未完成。', next: '先檢查已保留的草稿；重新製作會建立另一個工作。' },
  controller_restarted: { title: '上次製作尚未完成', reason: 'Clipen 重新啟動時發現未結束的工作。', next: '先檢查已保留的草稿；重新製作會建立另一個工作。' },
  root_unavailable: { title: '讀不到素材磁碟', reason: '製作需要的來源資料夾目前無法存取。', next: '重新連接磁碟，回到素材頁確認位置並完成檢查。' },
  root_changed: { title: '素材位置已改變', reason: '原先授權的資料夾已改變。', next: '回到素材頁重新選擇資料夾並完成檢查。' },
  ffmpeg_unavailable: { title: '找不到影片處理工具', reason: '本機無法啟動 FFmpeg。', next: '確認 FFmpeg 已安裝並可使用，再重新製作。' },
}
export function productionProblem(code?: string | null, provider = 'Codex'): ProductionProblem {
  const problem = problems[code ?? ''] ?? { title: '這次製作尚未完成', reason: '目前資料不足以確認中斷原因。', next: '可以先查看保留的結果，或稍後重新製作。' }
  const named = (text: string) => text.replaceAll('{provider}', provider)
  return { title: named(problem.title), reason: named(problem.reason), next: named(problem.next) }
}

const issues: Record<string, string> = {
  subtitle_coverage_incomplete: '部分對話仍缺字幕，還需要補齊。',
  subtitle_source_range_unresolved: '部分對話的辨識與時間尚未修好，還無法完成字幕。',
  production_music_required: '尚未完成配樂：需要加入有來源與授權資料的音樂。',
  production_transition_required: '尚未完成轉場：剪點之間還缺少轉場安排。',
  production_editing_required: '尚未完成精剪：還需要選材、剪點與段落安排。',
  program_graphics_missing: '尚未完成畫面設計：目前還沒有節目元件。',
  program_component_collision: '畫面元件互相重疊，需要調整位置或出現時間。',
  component_anchor_missing: '畫面元件失去對應鏡頭，需要重新連結。',
  component_anchor_out_of_bounds: '畫面元件超出對應鏡頭的時間，需要調整。',
  program_component_overflow: '文字超出版面，需要縮短或調整位置。',
  text_layout_collision: '字幕或文字互相遮擋，需要重新排版。',
  source_speech_evidence_missing: '部分口語尚未辨識，還無法確認字幕是否完整。',
  subtitle_alignment_required: '字幕時間尚未核對，或修改文字後需要重新確認對齊。',
  program_subtitles_missing: '保留的口語還沒有對應字幕。',
  subtitle_muted_source: '字幕對應的原音已靜音，需要確認聲音與字幕安排。',
  subtitle_source_changed: '口語內容已更正，對應字幕尚未更新。',
  retimed_speech_mapping_unavailable: '變速片段的口語與字幕時間尚無法可靠對應。',
  mix_review_required: '預覽的混音尚未完成檢查。',
  hdr_preview_unsupported: '這段 HDR 素材目前無法使用此預覽流程。',
  program_component_missing_glyph: '這個字型無法顯示部分文字，需要更換文字或字型。',
  text_style_missing: '文字缺少可渲染的樣式，需要修復。',
  audio_out_of_bounds: '音樂或音效超出片段範圍，需要調整時間。',
}
export function productionIssue(code: string): string {
  return issues[code] ?? '有一項聲畫或來源檢查尚未通過；詳細資訊保留了原始檢查內容。'
}
