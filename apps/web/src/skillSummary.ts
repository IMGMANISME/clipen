import type { SkillBinding } from './api'

/** Short labels for what a custom Skill's clipen.json selects. */
export function bindingSummary(binding: SkillBinding, names: Map<string, string>): string[] {
  return [
    binding.extends && `延伸：${names.get(binding.extends) ?? binding.extends}`,
    binding.visual_style_id && `畫面：${binding.visual_style_id}`,
    binding.font_style_id && `字體：${binding.font_style_id}`,
    binding.components && `元件：${binding.components.join('、')}`,
    binding.default_duration_seconds && `預設 ${Math.round(binding.default_duration_seconds / 60 * 10) / 10} 分鐘`,
  ].filter((value): value is string => Boolean(value))
}
