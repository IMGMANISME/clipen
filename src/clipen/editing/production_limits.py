"""Session-scoped production bounds. Legacy style proofs keep their own policy."""
from dataclasses import dataclass
from .style_proof import style_proof_stats


@dataclass(frozen=True)
class ProductionLimits:
    duration_seconds: int = 75
    clips: int = 30
    titles: int = 96
    components: int = 48
    effects: int = 256
    delivery_min_seconds: int = 30


def limits_for(tools):
    session=tools._session_value()
    target=session['intent'].get('target_duration_seconds',60) if session else 60
    return ProductionLimits(max(75,round(target*1.1)),180,960,128,1024,round(target*.9)) if target>75 else ProductionLimits()


def valid_production(tools,timeline,*,complete=False):
    return not production_limit_issues(tools,timeline,complete=complete)


def production_limit_issues(tools,timeline,*,complete=False):
    limits=limits_for(tools);stats=style_proof_stats(timeline)
    bounds=(('duration_seconds','片長（秒）',stats.duration_seconds,limits.delivery_min_seconds if complete else .75,limits.duration_seconds),
            ('clips','鏡頭數',stats.clip_count,1,limits.clips),('longest_clip_seconds','單鏡最長秒數',stats.longest_clip_seconds,0,30),
            ('titles','文字數',stats.title_count,0,limits.titles),('effects','效果數',stats.effect_count,0,limits.effects))
    issues=[]
    for field,label,actual,minimum,maximum in bounds:
        if minimum<=actual<=maximum:continue
        below_target=field=='duration_seconds' and complete and actual<minimum
        message=f'{label}為 {actual:g}，本次允許範圍為 {minimum:g}–{maximum:g}。'
        if below_target:message+=' 若使用者已更改片長要求，請以 set_target_duration 保存新目標後重新驗證。'
        issues.append({'code':'production_duration_below_target' if below_target else 'production_limits_exceeded',
                       'field':field,'actual':actual,'minimum':minimum,'maximum':maximum,'message':message})
    return issues
