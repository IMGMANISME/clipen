"""Session-scoped production bounds and the timeline counts they are checked against."""
from dataclasses import dataclass
from fractions import Fraction

from clipen.domain import EditTimeline, MediaClip, TrackKind


@dataclass(frozen=True, slots=True)
class TimelineStats:
    duration_seconds: float
    clip_count: int
    longest_clip_seconds: float
    title_count: int
    effect_count: int


def timeline_stats(timeline: EditTimeline) -> TimelineStats:
    frame_rate = Fraction(timeline.frame_rate.numerator, timeline.frame_rate.denominator)
    video = next((track for track in timeline.tracks if track.kind is TrackKind.VIDEO), None)
    clips = () if video is None else video.items
    clip_seconds = [Fraction(item.duration_frames, 1) / frame_rate for item in clips]
    return TimelineStats(
        float(Fraction(timeline.duration_frames, 1) / frame_rate),
        len(clips),
        float(max(clip_seconds, default=Fraction(0, 1))),
        sum(len(track.items) for track in timeline.tracks if track.kind is TrackKind.TITLE),
        sum(len(item.effects) for track in timeline.tracks for item in track.items if isinstance(item, MediaClip)),
    )


@dataclass(frozen=True)
class ProductionLimits:
    duration_seconds: int = 75
    clips: int = 30
    titles: int = 96
    components: int = 48
    effects: int = 256
    delivery_min_seconds: int = 30


def limits_for(tools):
    target=tools._session_value()['intent'].get('target_duration_seconds',60)
    return ProductionLimits(max(75,round(target*1.1)),180,960,128,1024,round(target*.9)) if target>75 else ProductionLimits()


def production_limit_issues(tools,timeline,*,complete=False):
    limits=limits_for(tools);stats=timeline_stats(timeline)
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
