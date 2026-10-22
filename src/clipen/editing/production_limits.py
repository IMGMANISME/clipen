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
    limits=limits_for(tools);stats=style_proof_stats(timeline)
    return (1<=stats.clip_count<=limits.clips and (.75 if not complete else limits.delivery_min_seconds)<=stats.duration_seconds<=limits.duration_seconds
            and stats.longest_clip_seconds<=30 and stats.title_count<=limits.titles and stats.effect_count<=limits.effects)
